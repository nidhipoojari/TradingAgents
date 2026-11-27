"""F1: explicit rejection of a trade proposal, and the approval-decision audit log. Offline (fake Alpaca)."""

from __future__ import annotations

import datetime
import json
import threading
import time
from pathlib import Path

import pytest

from tests.test_alpaca_paper import FakeData, FakeTrading, _no_alpaca_sdk  # noqa: F401
from tests.test_dashboard_logic import LIMITS, TESTER, TODAY, _broker, _result
from tests.test_graph_end_to_end import offline  # noqa: F401
from tests.test_phase3_integration import TRADE_DATE, phase2_on  # noqa: F401
from tradingagents.dashboard import trade
from tradingagents.execution import alpaca_paper as ap
from tradingagents.execution.decision_log import LOG_NAME, DecisionLog
from tradingagents.execution.identity import _issue
from tradingagents.execution.order_policy import OrderRejected


@pytest.fixture(autouse=True)
def _fresh_sign_in():
    """A fresh signed-in approver for every test. A password confirmation lasts 5 minutes of wall-clock
    time (identity.STEP_UP_SECONDS), so one issued once at collection expires in a run that reaches this
    test late, or after a system clock step (+185.7 s on 2026-11-25 failed 4 tests in test_trade_rejection).
    Set directly, not with monkeypatch, so a test's monkeypatch.undo() cannot bring back a stale one."""
    global TESTER
    TESTER = _issue("tester", "password")


def _setup(tmp_path):
    broker = _broker(tmp_path)
    result = _result("Buy")
    panel = trade.load_panel(result, broker, LIMITS, today=TODAY)
    return broker, result, panel, DecisionLog.for_results_dir(tmp_path)


def _submit(panel, result, broker, decisions, qty=2, **kw):
    args = {"confirmed": True, "identity": TESTER, "today": TODAY, "order_type": "limit", "limit_price": 100.0,
            "decisions": decisions, **kw}
    return trade.submit(panel, result, broker, qty, **args)


@pytest.mark.unit
def test_a_rejection_is_recorded_persistently_and_never_reaches_alpaca(tmp_path):
    broker, result, panel, decisions = _setup(tmp_path)
    broker.trading.submit_order = lambda **k: pytest.fail("a rejection must never send an order")
    entry, created = trade.reject(panel, result, decisions, identity=TESTER, qty=2, limit_price=100.0)
    assert created
    [saved] = DecisionLog.for_results_dir(tmp_path).entries()              # read back from disk
    assert saved == entry
    assert {k: saved[k] for k in ("event", "category", "run_id", "proposal_id", "ticker", "side", "qty",
                                  "limit_price", "user", "auth_method")} == {
        "event": "rejected", "category": "user_rejected", "run_id": result.run_id,
        "proposal_id": f"ta-{result.run_id}-buy", "ticker": "NVDA", "side": "buy", "qty": 2, "limit_price": 100.0,
        "user": "tester", "auth_method": "password"}
    assert datetime.datetime.fromisoformat(saved["recorded_at"]).tzinfo is not None
    assert ap.OrderLedger.for_results_dir(tmp_path).entries() == []          # not an order: ledger untouched


@pytest.mark.unit
def test_repeated_clicks_and_reruns_record_one_rejection(tmp_path):
    broker, result, panel, decisions = _setup(tmp_path)
    assert trade.reject(panel, result, decisions, identity=TESTER)[1] is True
    assert trade.reject(panel, result, decisions, identity=TESTER)[1] is False
    threads = [threading.Thread(target=trade.reject, args=(panel, result, decisions), kwargs={"identity": TESTER})
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [e["event"] for e in decisions.entries()] == ["rejected"]


@pytest.mark.unit
def test_a_rejected_proposal_cannot_be_submitted_until_reconsidered_and_approved_again(tmp_path):
    broker, result, panel, decisions = _setup(tmp_path)
    trade.reject(panel, result, decisions, identity=TESTER)
    with pytest.raises(OrderRejected, match="rejected; reconsider it"):
        _submit(panel, result, broker, decisions)                          # even with a tick and a sign-in
    assert broker.trading.submitted == []
    assert decisions.entries()[-1]["category"] == "proposal_rejected"

    assert trade.reconsider(panel, result, decisions, identity=TESTER)["event"] == "reconsidered"
    assert decisions.rejection(panel.client_order_id) is None and decisions.reconsiderations(panel.client_order_id) == 1
    with pytest.raises(OrderRejected, match="approval"):
        _submit(panel, result, broker, decisions, confirmed=False)       # reopening is not an approval
    receipt = _submit(panel, result, broker, decisions)                    # a new, deliberate approval
    assert receipt.order_id and len(broker.trading.submitted) == 1
    assert trade.reconsider(panel, result, decisions, identity=TESTER) is None   # nothing to reopen now


@pytest.mark.unit
def test_a_successful_order_is_recorded_as_before_plus_its_approval_decision(tmp_path):
    """Phase 3: the ledger is unchanged (approved -> submitted); the approval is also a decision, named by the
    order's ledger lines. A repeat submission of the placed order records no second approval."""
    broker, result, panel, decisions = _setup(tmp_path)
    receipt = _submit(panel, result, broker, decisions)
    assert receipt.order_id and not receipt.duplicate
    ledger = ap.OrderLedger.for_results_dir(tmp_path).entries()
    assert [e["event"] for e in ledger] == ["approved", "submitted"]
    [approval] = decisions.entries()
    assert (approval["event"], approval["category"], approval["user"]) == ("approved", "user_approved", "tester")
    assert {e["approval_decision_id"] for e in ledger} == {approval["decision_id"]}
    assert _submit(panel, result, broker, decisions).duplicate              # one order per run still holds
    assert len(broker.trading.submitted) == 1 and len(decisions.entries()) == 1


@pytest.mark.unit
def test_each_kind_of_failure_is_recorded_with_its_category(tmp_path, monkeypatch):
    broker, result, panel, decisions = _setup(tmp_path)
    with pytest.raises(OrderRejected):
        _submit(panel, result, broker, decisions, confirmed=False)                     # no tick
    with pytest.raises(OrderRejected):
        _submit(panel, result, broker, decisions, stale_acknowledged=False, today="2026-12-01")   # stale, unacknowledged
    with pytest.raises(OrderRejected):
        _submit(panel, result, broker, decisions, qty=20)                               # $2,000 over the cap
    with pytest.raises(OrderRejected):
        _submit(panel, result, broker, decisions, limit_price=100.005)                  # not whole cents
    monkeypatch.setattr(broker, "latest_price", lambda ticker: None)                    # broker: no fresh price
    with pytest.raises(OrderRejected, match="No current price"):
        _submit(panel, result, broker, decisions)
    monkeypatch.undo()
    broker.trading.blocked = True                                                       # broker: account blocked
    with pytest.raises(ap.BrokerError, match="blocked"):
        _submit(panel, result, broker, decisions)
    assert [e["category"] for e in decisions.entries()] == [
        "missing_approval", "missing_approval", "invalid_order", "invalid_order", "broker_validation",
        "broker_validation"]
    assert broker.trading.submitted == [] and ap.OrderLedger.for_results_dir(tmp_path).entries() == []


@pytest.mark.unit
def test_an_alpaca_refusal_is_recorded_in_both_logs_without_secrets(tmp_path, monkeypatch):
    secret = "PKSECRET" + "x" * 24
    monkeypatch.setenv("ALPACA_SECRET_KEY", secret)
    broker, result, panel, decisions = _setup(tmp_path)

    def refuse(order_data):
        raise RuntimeError(f"403 forbidden for key {secret}")

    broker.trading.submit_order = refuse
    with pytest.raises(ap.SubmissionFailed):
        _submit(panel, result, broker, decisions)
    approval, failure = decisions.entries()        # approved (recorded before sending), then Alpaca's refusal
    assert approval["event"] == "approved" and failure["approval_decision_id"] == approval["decision_id"]
    assert failure["category"] == "alpaca_api_error" and "[redacted]" in failure["problems"][0]
    assert [e["event"] for e in ap.OrderLedger.for_results_dir(tmp_path).entries()] == ["approved", "failed"]
    assert secret not in (tmp_path / LOG_NAME).read_text()
    assert issubclass(ap.SubmissionFailed, ap.BrokerError)                 # existing handlers still catch it


@pytest.mark.unit
def test_a_wrong_password_is_recorded_without_the_password(tmp_path):
    broker, result, panel, decisions = _setup(tmp_path)
    trade.record_failure(decisions, "password_failed", panel, result, qty=2, limit_price=100.0, identity=TESTER,
                         problems=["The password is not right."])
    [entry] = decisions.entries()
    assert (entry["category"], entry["user"], entry["event"]) == ("password_failed", "tester", "submission_failed")
    with pytest.raises(ValueError, match="unknown category"):
        trade.record_failure(decisions, "something_else", panel, result)


# ------------------------------------------------------------------ through the page
def test_the_page_rejects_reconsiders_and_audits(phase2_on, monkeypatch):  # noqa: F811
    pytest.importorskip("streamlit")
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    from tradingagents.execution import identity as auth

    results_root = phase2_on              # scripted models, results under tmp_path (test_phase3_integration)
    users = results_root / "users.json"
    password = "correct horse battery"
    users.write_text(json.dumps({"alice": auth.hash_password(password)}))
    monkeypatch.setenv(auth.USERS_FILE_ENV, str(users))
    monkeypatch.setenv(auth.AUTH_DB_ENV, str(results_root / "auth.sqlite"))
    trading = FakeTrading()
    monkeypatch.setattr(ap.AlpacaPaperBroker, "from_env",
                        classmethod(lambda cls, ledger=None: cls(trading, FakeData(price=100.0), ledger=ledger)))
    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: type("R", (), {"symbol": symbol})())
    monkeypatch.setenv("TRADINGAGENTS_DASHBOARD_MAX_LLM_CALLS", "off")
    monkeypatch.setenv("TRADINGAGENTS_DASHBOARD_MAX_COST_USD", "off")
    st.cache_resource.clear()

    page = AppTest.from_file(str(Path(trade.__file__).with_name("app.py")), default_timeout=30).run()
    next(t for t in page.sidebar.text_input if t.label == "Username").input("alice")
    next(t for t in page.sidebar.text_input if t.label == "Password").input(password)
    next(b for b in page.sidebar.button if b.label == "Sign in").click()
    page.run()
    next(t for t in page.text_input if t.label == "Ticker").input("NVDA")
    page.date_input[0].set_value(datetime.date.fromisoformat(TRADE_DATE))
    page.multiselect[0].set_value(["market"])
    next(b for b in page.button if b.label == "Run analysis").click()
    page.run()
    deadline = time.time() + 60
    while time.time() < deadline and not any(m.label == "Rating" for m in page.metric):
        time.sleep(0.2)
        page.run()

    def button(label):
        return next((b for b in page.button if b.label == label), None)

    def approve(typed=password):
        next(n for n in page.number_input if n.label.startswith("Quantity")).set_value(2)
        page.run()
        next(c for c in page.checkbox if c.label.startswith("The analysis is for")).check()
        next(c for c in page.checkbox if c.label.startswith("I approve a paper limit order")).check()
        next(t for t in page.text_input if t.label.startswith("Your password, to approve")).input(typed)
        page.run()

    approve()
    assert not button("Submit paper order").disabled
    button("Reject trade proposal").click()
    page.run()
    page.run()                                                         # reruns record nothing more
    assert not page.exception
    assert any("Trade proposal rejected by alice" in i.value for i in page.info)
    assert button("Submit paper order") is None and button("Reject trade proposal") is None
    log = DecisionLog.for_results_dir(_results_dir())
    assert [e["event"] for e in log.entries()] == ["rejected"] and log.entries()[0]["user"] == "alice"

    button("Reconsider this proposal").click()
    page.run()
    tick = next(c for c in page.checkbox if c.label.startswith("I approve a paper limit order"))
    assert not tick.value and button("Submit paper order").disabled     # nothing carried over: start again
    assert next(n for n in page.number_input if n.label.startswith("Quantity")).value == 0
    approve("wrong password!!")
    button("Submit paper order").click()
    page.run()
    assert trading.submitted == []
    approve()
    button("Submit paper order").click()
    page.run()
    assert not page.exception and len(trading.submitted) == 1
    events = [(e["event"], e["category"]) for e in log.entries()]
    assert events == [("rejected", "user_rejected"), ("reconsidered", "user_rejected"),
                      ("submission_failed", "password_failed"), ("approved", "user_approved")]
    text = log.path.read_text()
    assert password not in text and "wrong password!!" not in text
    assert [e["event"] for e in ap.OrderLedger.for_results_dir(_results_dir()).entries()] == ["approved", "submitted"]


def _results_dir():
    from tradingagents import service

    return service.config_for_source("dashboard")["results_dir"]
