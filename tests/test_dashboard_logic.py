"""F1 dashboard logic without Streamlit: background analyses and the paper-trade panel."""

import threading
import time
from pathlib import Path

import pytest

from tests.test_alpaca_paper import FakeData, FakeTrading, _no_alpaca_sdk  # noqa: F401
from tradingagents import service
from tradingagents.dashboard import jobs, trade
from tradingagents.execution import alpaca_paper as ap
from tradingagents.execution.identity import _issue
from tradingagents.execution.order_policy import OrderLimits, OrderRejected

TODAY = "2026-11-21"
LIMITS = OrderLimits(max_notional=1000.0)

TESTER = _issue("tester", "password")   # a signed-in approver


def _result(rating="Buy", ticker="NVDA", trade_date=TODAY, asset_type="stock", run_id="20261121-101500-ab12cd34"):
    return service.AnalysisResult(
        run_id=run_id, source="dashboard", ticker=ticker, trade_date=trade_date, asset_type=asset_type,
        analysts=("market",), rating=rating, final_state={"final_trade_decision": f"**Rating**: {rating}"},
        usage={}, results_dir=Path("."), usage_path=Path("token_usage.json"))


def _broker(tmp_path, **trading):
    return ap.AlpacaPaperBroker(FakeTrading(**trading), FakeData(price=100.0),
                                ledger=ap.OrderLedger.for_results_dir(tmp_path))


def _wait(job, timeout=5.0):
    end = time.time() + timeout
    while job.running and time.time() < end:
        time.sleep(0.01)
    assert not job.running


# ------------------------------------------------------------------ jobs
REQUEST = service.AnalysisRequest(ticker="NVDA", trade_date="2026-01-09", source="dashboard",
                                  analysts=("market", "news"))


@pytest.mark.unit
def test_a_job_runs_in_the_background_and_keeps_its_progress():
    release = threading.Event()

    def run(request, on_event, cancel):
        on_event(service.AnalysisEvent(request.run_id, "step", progress=(1, 5, "Analysts")))
        release.wait(5)
        return _result(run_id=request.run_id)

    job = jobs.start_job(REQUEST, run=run)
    time.sleep(0.05)
    assert job.running and job.snapshot()["progress"] == (1, 5, "Analysts")
    release.set()
    _wait(job)
    assert job.status == "done" and job.result.run_id == job.run_id
    assert job.progress == (5, 5, "Done")   # 2 analysts + 3 decision steps


@pytest.mark.unit
def test_a_failed_job_says_why():
    def run(request, on_event, cancel):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    job = jobs.start_job(REQUEST, run=run)
    _wait(job)
    assert job.status == "failed" and "429" in job.error


@pytest.mark.unit
def test_a_job_can_be_cancelled():
    def run(request, on_event, cancel):
        while not cancel.wait(0.01):
            pass
        raise service.AnalysisCancelled("r", "on request")

    job = jobs.start_job(REQUEST, run=run)
    job.cancel.set()
    _wait(job)
    assert job.status == "cancelled"


@pytest.mark.unit
def test_a_second_job_is_refused_while_one_runs():
    assert service._RUN_LOCK.acquire()
    try:
        with pytest.raises(service.AnalysisBusyError):
            jobs.start_job(REQUEST, run=lambda *a, **k: None)
    finally:
        service._RUN_LOCK.release()


@pytest.mark.unit
def test_the_activity_log_is_bounded():
    class Message:
        content = "x" * 1000
        tool_calls = [{"name": "get_stock_data"}]

    job = jobs.AnalysisJob(request=REQUEST, run_id="r")
    for _ in range(jobs.MAX_LOG_LINES):
        job._event(service.AnalysisEvent("r", "step", messages=(Message(),), progress=(0, 5, "Analysts")))
    lines = job.log()
    assert len(lines) == jobs.MAX_LOG_LINES
    assert all(len(line) < 500 for line in lines)


# ------------------------------------------------------------------ paper-trade panel
@pytest.mark.unit
def test_hold_shows_no_order(tmp_path):
    panel = trade.load_panel(_result("Hold"), _broker(tmp_path, held=10), LIMITS, today=TODAY)
    assert not panel.can_trade and panel.client_order_id is None
    assert not trade.evaluate(panel, 1, confirmed=True, order_type="limit", limit_price=100.0).can_submit


@pytest.mark.unit
def test_a_sell_without_a_position_is_disabled_with_a_reason(tmp_path):
    panel = trade.load_panel(_result("Sell"), _broker(tmp_path, held=0), LIMITS, today=TODAY)
    check = trade.evaluate(panel, 1, confirmed=True, order_type="limit", limit_price=100.0)
    assert not check.can_submit and "short" in check.problems[0].lower()


@pytest.mark.unit
def test_nothing_is_submittable_until_the_person_sizes_and_approves_it(tmp_path):
    panel = trade.load_panel(_result("Buy"), _broker(tmp_path), LIMITS, today=TODAY)
    assert panel.can_trade
    assert not trade.evaluate(panel, 0, confirmed=True, order_type="limit", limit_price=100.0).can_submit       # no quantity yet
    assert not trade.evaluate(panel, 2, confirmed=False, order_type="limit", limit_price=100.0).can_submit      # not approved
    assert not trade.evaluate(panel, 20, confirmed=True, order_type="limit", limit_price=100.0).can_submit      # $2,000 over the cap
    no_sign_in = trade.evaluate(panel, 2, confirmed=True, order_type="limit", limit_price=100.0)
    assert not no_sign_in.can_submit and "Sign in" in no_sign_in.problems[0]
    check = trade.evaluate(panel, 2, confirmed=True, identity=TESTER, order_type="limit", limit_price=100.0)
    assert check.can_submit and check.notional == 200.0                  # 2 x $100 limit


@pytest.mark.unit
def test_one_order_per_run(tmp_path):
    broker = _broker(tmp_path)
    result = _result("Buy")
    panel = trade.load_panel(result, broker, LIMITS, today=TODAY)

    receipt = trade.submit(panel, result, broker, 2, confirmed=True, identity=TESTER, today=TODAY, order_type="limit", limit_price=100.0)

    assert receipt.order_id and len(broker.trading.submitted) == 1
    reloaded = trade.load_panel(result, broker, LIMITS, today=TODAY)   # the page after a rerun
    assert reloaded.already_submitted is not None and not reloaded.can_trade
    assert not trade.evaluate(reloaded, 2, confirmed=True, order_type="limit", limit_price=100.0).can_submit
    # Even a direct second submit sends nothing new.
    assert trade.submit(panel, result, broker, 2, confirmed=True, identity=TESTER, today=TODAY, order_type="limit", limit_price=100.0).duplicate
    assert len(broker.trading.submitted) == 1


@pytest.mark.unit
def test_submit_refuses_an_unapproved_order(tmp_path):
    broker = _broker(tmp_path)
    result = _result("Buy")
    panel = trade.load_panel(result, broker, LIMITS, today=TODAY)
    with pytest.raises(OrderRejected, match="approval"):
        trade.submit(panel, result, broker, 2, confirmed=False, identity=TESTER, today=TODAY, order_type="limit", limit_price=100.0)
    assert broker.trading.submitted == []


@pytest.mark.unit
def test_an_old_analysis_needs_acknowledging(tmp_path):
    broker = _broker(tmp_path)
    result = _result("Buy", trade_date="2026-10-01")
    panel = trade.load_panel(result, broker, LIMITS, today=TODAY)
    assert panel.stale
    assert not trade.evaluate(panel, 1, confirmed=True, order_type="limit", limit_price=100.0).can_submit
    assert trade.evaluate(panel, 1, confirmed=True, stale_acknowledged=True, identity=TESTER, order_type="limit", limit_price=100.0).can_submit


@pytest.mark.unit
def test_crypto_is_not_paper_traded(tmp_path):
    broker = _broker(tmp_path)
    broker.trading.get_open_position = lambda s: pytest.fail("no position lookup for a crypto pair")
    panel = trade.load_panel(_result("Buy", ticker="BTC-USD", asset_type="crypto"), broker, LIMITS, today=TODAY)
    assert not panel.can_trade


@pytest.mark.unit
def test_a_review_rating_places_nothing(tmp_path):
    panel = trade.load_panel(_result("REVIEW"), _broker(tmp_path), LIMITS, today=TODAY)
    assert not panel.can_trade and "review" in panel.proposal.reason.lower()


# ------------------------------------------------------------------ the page itself
@pytest.mark.unit
def test_the_page_renders_without_an_alpaca_account(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    for name in (*ap.KEY_ENVS, *ap.SECRET_ENVS):
        monkeypatch.delenv(name, raising=False)
    app_path = Path(trade.__file__).with_name("app.py")
    page = AppTest.from_file(str(app_path), default_timeout=30).run()
    assert not page.exception
    assert "Run analysis" in [b.label for b in page.button]
    assert [t.label for t in page.main.text_input] == ["Ticker"]
    # Sign-in is offered for approving orders; viewing needs none.
    assert [t.label for t in page.sidebar.text_input] == ["Username", "Password"]
    assert "Sign in" in [b.label for b in page.sidebar.button]
    # No paper account configured: the page says so instead of failing.
    assert any("ALPACA_API_KEY" in w.value for w in page.sidebar.warning)
