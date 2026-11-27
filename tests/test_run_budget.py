"""Per-run LLM limits (tradingagents/run_budget.py): the guard itself, the shared service on the real
graph, the worker process and the dashboard. Offline: scripted models, no network."""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.runnables import Runnable

from tests.test_analysis_service import _fake_langfuse
from tests.test_e1_e2_e3_audit import IN, OUT, Named
from tests.test_graph_end_to_end import TRADE_DATE, ScriptedModel, _Client, offline  # noqa: F401
from tradingagents import e1_routing, run_budget, service
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.run_budget import BudgetGuard, RunBudget

PRICES = {"cheap": {"input_per_million_usd": 1.0, "output_per_million_usd": 2.0},
          "dear": {"input_per_million_usd": 10.0, "output_per_million_usd": 20.0}}


# ------------------------------------------------------------------ the guard, call by call
def _guard(calls=None, cost=None, pricing=PRICES):
    return BudgetGuard(RunBudget(calls, cost), threading.Event(), pricing=pricing)


def _start(guard, model="cheap", chars=3000):
    run_id = uuid.uuid4()
    guard.on_chat_model_start({}, [[HumanMessage("x" * chars)]], run_id=run_id,
                              metadata={"ls_model_name": model})
    return run_id


def _end(guard, run_id, tokens_in=None, tokens_out=None):
    if tokens_in is None:
        response = LLMResult(generations=[[ChatGeneration(message=AIMessage("text"))]])
    else:
        response = LLMResult(generations=[[]],
                             llm_output={"token_usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}})
    guard.on_llm_end(response, run_id=run_id)


def _reservation(price, chars=3000):
    return (chars / run_budget.CHARS_PER_TOKEN * price["input_per_million_usd"]
            + run_budget.OUTPUT_RESERVE_TOKENS * price["output_per_million_usd"]) / 1e6


@pytest.mark.unit
def test_calls_within_the_limits_are_charged_their_reported_usage():
    guard = _guard(calls=5, cost=1.0)
    for _ in range(3):
        _end(guard, _start(guard), 1000, 100)
    snap = guard.snapshot()
    assert snap["llm_calls_started"] == 3 and not snap["exhausted"] and not guard.cancel.is_set()
    assert snap["budget_cost_usd"] == round(3 * (1000 * 1.0 + 100 * 2.0) / 1e6, 6)
    assert snap["llm_calls_in_flight"] == 0 and snap["reserved_in_flight_usd"] == 0


@pytest.mark.unit
def test_the_call_limit_refuses_the_next_call_and_stops_the_run():
    from tradingagents.service import _CancelSignal

    guard = _guard(calls=2)
    for _ in range(2):
        _end(guard, _start(guard), 10, 10)
    with pytest.raises(_CancelSignal, match="before a model call"):
        _start(guard)
    assert guard.cancel.is_set() and guard.snapshot()["reason"] == "call limit reached: 2 of 2 LLM calls used"
    with pytest.raises(_CancelSignal):                 # every later call is refused too
        _start(guard)
    snap = guard.snapshot()
    assert snap["llm_calls_started"] == 2 and snap["llm_calls_refused"] == 2


@pytest.mark.unit
def test_the_cost_limit_counts_the_reservations_of_calls_still_running():
    from tradingagents.service import _CancelSignal

    one = _reservation(PRICES["cheap"])                # 1,000 input + 2,000 output tokens = $0.005
    guard = _guard(cost=2.5 * one)
    first, second = _start(guard), _start(guard)       # both running: 2 reservations held
    with pytest.raises(_CancelSignal):
        _start(guard)                                  # a third would pass the ceiling
    assert guard.snapshot()["reason"].startswith("cost limit reached")
    assert guard.snapshot()["reserved_in_flight_usd"] == round(2 * one, 6)
    _end(guard, first, 1000, 10)
    _end(guard, second, 1000, 10)
    snap = guard.snapshot()
    assert snap["budget_cost_usd"] == round(2 * (1000 + 20) / 1e6, 6) and snap["reserved_in_flight_usd"] == 0


@pytest.mark.unit
def test_actual_usage_replaces_the_reservation_so_cheap_calls_keep_running():
    from tradingagents.service import _CancelSignal

    one = _reservation(PRICES["cheap"])                # $0.005 reserved per call
    guard = _guard(cost=1.5 * one)                     # $0.0075: reservations alone would allow 1 call
    admitted = 0
    with pytest.raises(_CancelSignal):
        while True:                                    # one at a time; each actually costs $0.00102
            _end(guard, _start(guard), 1000, 10)
            admitted += 1
    # Admitted while charged + the next reservation <= $0.0075: 0, 0.00102 and 0.00204 charged -> 3 calls.
    assert admitted == 3 and guard.snapshot()["budget_cost_usd"] == round(3 * 0.00102, 6)
    assert guard.snapshot()["reason"].startswith("cost limit reached")


@pytest.mark.unit
def test_an_unpriced_model_is_charged_at_the_highest_known_price():
    guard = _guard(cost=1.0)
    _end(guard, _start(guard, model="mystery-model"), 1000, 100)
    snap = guard.snapshot()
    assert snap["unpriced_calls"] == 1
    assert snap["budget_cost_usd"] == round((1000 * 10.0 + 100 * 20.0) / 1e6, 6)   # the "dear" price


@pytest.mark.unit
def test_a_call_without_usage_keeps_its_whole_reservation():
    guard = _guard(cost=1.0)
    _end(guard, _start(guard))                         # the provider reported no tokens
    snap = guard.snapshot()
    assert snap["calls_without_usage"] == 1 and snap["budget_cost_usd"] == round(_reservation(PRICES["cheap"]), 6)


@pytest.mark.unit
def test_an_e1_fallback_counts_the_failed_primary_and_the_backup():
    guard = _guard(calls=10, cost=1.0)
    primary = _start(guard, model="dear")
    guard.on_llm_error(RuntimeError("429 RESOURCE_EXHAUSTED"), run_id=primary)
    _end(guard, _start(guard, model="cheap"), 1000, 100)
    snap = guard.snapshot()
    assert snap["llm_calls_started"] == 2 and snap["failed_calls"] == 1
    # The failed call is charged its input estimate (1,000 tokens at the dear price); the backup its usage.
    assert snap["budget_cost_usd"] == round((1000 * 10.0 + 1000 * 1.0 + 100 * 2.0) / 1e6, 6)


@pytest.mark.unit
def test_concurrent_calls_cannot_slip_past_the_call_limit():
    from tradingagents.service import _CancelSignal

    guard = _guard(calls=5)
    barrier, admitted, refused = threading.Barrier(20), [], []

    def call():
        barrier.wait()
        try:
            admitted.append(_start(guard))
        except _CancelSignal:
            refused.append(1)

    threads = [threading.Thread(target=call) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(admitted) == 5 and len(refused) == 15 and guard.snapshot()["llm_calls_started"] == 5


@pytest.mark.unit
def test_a_cancel_by_the_person_is_left_to_the_cancel_guard_and_not_counted():
    guard = _guard(calls=1)
    guard.cancel.set()
    _start(guard)                                      # no refusal from the budget, no count
    snap = guard.snapshot()
    assert snap["llm_calls_started"] == 0 and snap["llm_calls_refused"] == 0 and not snap["exhausted"]


@pytest.mark.unit
def test_a_cost_limit_without_prices_refuses_to_start():
    with pytest.raises(ValueError, match="needs model prices"):
        _guard(cost=0.04, pricing={})
    assert _guard(calls=3, pricing={}).snapshot()["llm_calls_started"] == 0     # a call limit alone is fine


@pytest.mark.unit
def test_the_dashboard_limits_default_to_15_calls_and_4_cents(monkeypatch):
    monkeypatch.delenv(run_budget.CALLS_ENV, raising=False)
    monkeypatch.delenv(run_budget.COST_ENV, raising=False)
    assert run_budget.dashboard_budget_from_env() == RunBudget(15, 0.04)
    monkeypatch.setenv(run_budget.CALLS_ENV, "25")
    monkeypatch.setenv(run_budget.COST_ENV, "0.10")
    assert run_budget.dashboard_budget_from_env() == RunBudget(25, 0.10)
    monkeypatch.setenv(run_budget.COST_ENV, "off")
    assert run_budget.dashboard_budget_from_env() == RunBudget(25, None)
    monkeypatch.setenv(run_budget.CALLS_ENV, "0")
    assert run_budget.dashboard_budget_from_env() is None
    monkeypatch.setenv(run_budget.CALLS_ENV, "fifteen")
    with pytest.raises(ValueError, match="not a number"):
        run_budget.dashboard_budget_from_env()
    with pytest.raises(ValueError):
        RunBudget(0, None)
    with pytest.raises(ValueError):
        RunBudget(None, -1.0)


@pytest.mark.unit
def test_the_status_line_for_people():
    guard = _guard(calls=2, cost=1.0)
    _end(guard, _start(guard), 10, 10)
    line = run_budget.describe_snapshot(guard.snapshot())
    assert line.startswith("Per-run limits: 1 of 2 LLM calls; budget accounting $0.000030 of $1.00")
    assert "(conservative, not a bill: $0.000030 from reported tokens)" in line
    assert run_budget.describe_snapshot(None) == "Per-run limits: off"


# ------------------------------------------------------------------ the shared service, real graph
class _NoUsage(ScriptedModel):
    model_name: str = "quiet-m"

    def bind_tools(self, tools, **kwargs) -> Runnable:
        return super().bind_tools(tools, **kwargs)


def _service(tmp_path, monkeypatch, budget, *, broken=False, analysts=service.ANALYST_ORDER, model_cls=Named,
             price=(1.0, 2.0), cancel=None, on_event=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    _fake_langfuse(monkeypatch, tmp_path)
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({n: {"input_per_million_usd": price[0], "output_per_million_usd": price[1]}
                                   for n in ("main-m", "backup-m", "quiet-m")}))
    monkeypatch.setenv("E3_PRICING_JSON", str(pricing))
    if model_cls is Named:
        models = {"main-m": Named(model_name="main-m", broken=broken), "backup-m": Named(model_name="backup-m")}
    else:
        models = {"main-m": model_cls()}
    monkeypatch.setattr(e1_routing, "create_tier_client", lambda cfg, tier, **k: _Client(
        models[cfg[f"{tier}_think_llm"]].model_copy(update={"callbacks": k.get("callbacks")})))
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
                  memory_log_path=str(tmp_path / "memory.md"), quick_think_llm="main-m", deep_think_llm="main-m")
    if broken:
        config.update(quick_fallback_llm="backup-m", deep_fallback_llm="backup-m")
    request = service.AnalysisRequest(ticker="NVDA", trade_date=TRADE_DATE, config=config, analysts=analysts,
                                      source="dashboard", budget=budget)
    return service.run_analysis(request, cancel=cancel, on_event=on_event)


def _e3(tmp_path):
    [summary] = [json.loads(p.read_text()) for p in (tmp_path / "e3").glob("*_e3.json")]
    return summary


@pytest.mark.unit
def test_a_run_within_its_limits_completes_and_reports_its_use(tmp_path, monkeypatch, offline):  # noqa: F811
    result = _service(tmp_path, monkeypatch, RunBudget(40, 1.0))
    snap = result.usage["run_budget"]
    assert snap["llm_calls_started"] == result.usage["llm_calls"] == 15 and not snap["exhausted"]
    assert snap["budget_cost_usd"] == round(15 * (IN * 1.0 + OUT * 2.0) / 1e6, 6)
    summary = _e3(tmp_path)
    assert summary["status"] == "success" and summary["run_budget"] == snap
    assert json.loads(result.usage_path.read_text())["run_budget"] == snap


@pytest.mark.unit
def test_the_call_limit_stops_the_run_as_a_budget_stop(tmp_path, monkeypatch, offline):  # noqa: F811
    with pytest.raises(service.AnalysisBudgetExceeded) as caught:
        _service(tmp_path, monkeypatch, RunBudget(5, None))
    exc = caught.value
    assert isinstance(exc, service.AnalysisCancelled)          # the stop path releases everything as before
    assert "stopped by its per-run limit (call limit reached: 5 of 5" in str(exc)
    assert exc.budget["llm_calls_started"] == 5 and exc.budget["llm_calls_refused"] >= 1
    summary = _e3(tmp_path)
    assert summary["status"] == "budget_exceeded" and "call limit" in summary["run_error"]
    assert summary["observed_llm_calls"] == 5                  # refused calls never started
    assert summary["run_budget"]["exhausted"] is True


@pytest.mark.unit
def test_the_cost_limit_stops_the_run_before_the_estimate_passes_it(tmp_path, monkeypatch, offline):  # noqa: F811
    # Input free, output $100 per million: a reservation is exactly 2,000 x $100/M = $0.20, whatever the
    # prompt, and each call actually costs 20 x $100/M = $0.002. A market-only run is sequential, so a
    # call is admitted while 0.002 x (calls so far) + 0.20 <= 0.207: exactly 4 calls, then the stop.
    limit = 0.207
    with pytest.raises(service.AnalysisBudgetExceeded, match="cost limit reached") as caught:
        _service(tmp_path, monkeypatch, RunBudget(None, limit), analysts=("market",), price=(0.0, 100.0))
    snap = caught.value.budget
    assert snap["llm_calls_started"] == 4 and snap["budget_cost_usd"] == round(4 * 0.002, 6)
    assert snap["budget_cost_usd"] <= limit and snap["llm_calls_in_flight"] == 0
    summary = _e3(tmp_path)
    assert summary["status"] == "budget_exceeded" and summary["observed_llm_calls"] == 4


@pytest.mark.unit
def test_an_e1_fallback_spends_the_limit_on_both_calls(tmp_path, monkeypatch, offline):  # noqa: F811
    with pytest.raises(service.AnalysisBudgetExceeded) as caught:
        _service(tmp_path, monkeypatch, RunBudget(6, 1.0), broken=True)
    snap, summary = caught.value.budget, _e3(tmp_path)
    assert snap["llm_calls_started"] == summary["observed_llm_calls"] == 6
    assert snap["failed_calls"] == summary["observed_llm_errors"] >= 3      # primaries that failed, then backups


@pytest.mark.unit
def test_calls_without_usage_are_charged_their_reservations(tmp_path, monkeypatch, offline):  # noqa: F811
    result = _service(tmp_path, monkeypatch, RunBudget(40, 5.0), model_cls=_NoUsage, analysts=("market",))
    snap = result.usage["run_budget"]
    assert snap["calls_without_usage"] == snap["llm_calls_started"] == result.usage["llm_calls"]
    assert snap["budget_cost_usd"] >= snap["llm_calls_started"] * run_budget.OUTPUT_RESERVE_TOKENS * 2.0 / 1e6


@pytest.mark.unit
def test_a_cancel_by_the_person_is_not_reported_as_a_budget_stop(tmp_path, monkeypatch, offline):  # noqa: F811
    cancel = threading.Event()

    def on_event(event):
        if event.kind == "step":
            cancel.set()

    with pytest.raises(service.AnalysisCancelled) as caught:
        _service(tmp_path, monkeypatch, RunBudget(40, 1.0), cancel=cancel, on_event=on_event)
    assert not isinstance(caught.value, service.AnalysisBudgetExceeded)
    assert _e3(tmp_path)["status"] == "cancelled"


@pytest.mark.unit
def test_concurrent_analysts_share_one_atomic_limit(tmp_path, monkeypatch, offline):  # noqa: F811
    with pytest.raises(service.AnalysisBudgetExceeded):
        _service(tmp_path, monkeypatch, RunBudget(3, None))         # four analysts start in parallel
    summary = _e3(tmp_path)
    assert summary["observed_llm_calls"] == summary["run_budget"]["llm_calls_started"] == 3


@pytest.mark.unit
def test_without_a_budget_nothing_changes(tmp_path, monkeypatch, offline):  # noqa: F811
    result = _service(tmp_path, monkeypatch, None)
    assert "run_budget" not in result.usage and "run_budget" not in _e3(tmp_path)
    assert result.usage["llm_calls"] == 15
    # The CLI builds requests without one; the MCP server always sets its call limit (tests/test_mcp_budget.py).
    from tradingagents import mcp_server

    assert mcp_server.build_request("NVDA", TRADE_DATE).budget.max_llm_calls == 24
    assert service.AnalysisRequest(ticker="NVDA", trade_date=TRADE_DATE, source="cli").budget is None


# ------------------------------------------------------------------ the worker process
@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    """As tests/test_worker.py's e3_env: E3 on with a stand-in Langfuse, no pricing (call limits only)."""
    for name, value in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "offline", "LANGFUSE_SECRET_KEY": "offline",
                        "LANGFUSE_BASE_URL": "http://localhost.invalid", "E3_RESULTS_DIR": str(tmp_path / "e3"),
                        "WORKER_FAKE_MARKER": str(tmp_path / "marker")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("E3_PRICING_JSON", raising=False)
    return tmp_path


@pytest.mark.unit
def test_a_budget_stop_crosses_the_worker_process_as_a_budget_stop(worker_env):
    from tests.test_worker import _request
    from tradingagents import worker

    with pytest.raises(service.AnalysisBudgetExceeded) as caught:
        worker.run_in_worker(_request(worker_env, budget=RunBudget(3, None)), setup="tests.worker_fakes:scripted")
    assert caught.value.budget["llm_calls_started"] == 3 and not getattr(caught.value, "terminated", True)
    [summary] = [json.loads(p.read_text()) for p in (worker_env / "e3").glob("*_e3.json")]
    assert summary["status"] == "budget_exceeded" and summary["observed_llm_calls"] == 3
    assert not service.is_busy()


@pytest.mark.unit
def test_a_budget_stop_with_a_hung_call_terminates_the_worker(worker_env):
    from tests.test_worker import _request
    from tradingagents import worker

    started = time.monotonic()
    with pytest.raises(service.AnalysisBudgetExceeded) as caught:   # market's call hangs; news uses the rest
        worker.run_in_worker(_request(worker_env, budget=RunBudget(2, None)), grace=1.0,
                             setup="tests.worker_fakes:hung_market_call")
    assert caught.value.terminated and "stopped by its per-run limit" in str(caught.value)
    assert time.monotonic() - started < 60 and not service.is_busy()
    [summary] = [json.loads(p.read_text()) for p in (worker_env / "e3").glob("*_e3.json")]
    assert summary["status"] == "budget_exceeded"


# ------------------------------------------------------------------ the dashboard
@pytest.mark.unit
def test_the_dashboard_job_reports_a_budget_stop_apart_from_a_cancel():
    from tradingagents.dashboard import jobs

    def stops(request, on_event=None, cancel=None):
        raise service.AnalysisBudgetExceeded(request.run_id, "before a model call", 0, 1,
                                             budget={"reason": "call limit reached: 15 of 15 LLM calls used"})

    def cancels(request, on_event=None, cancel=None):
        raise service.AnalysisCancelled(request.run_id, "after 2 report(s)")

    request = service.AnalysisRequest(ticker="NVDA", trade_date=TRADE_DATE, source="dashboard",
                                      budget=RunBudget(15, 0.04))
    for run, status in ((stops, "budget_exceeded"), (cancels, "cancelled")):
        job = jobs.start_job(request, run=run)
        for _ in range(100):
            if not job.running:
                break
            time.sleep(0.02)
        assert job.status == status
    assert "per-run limit" not in job.error


@pytest.mark.unit
def test_the_dashboard_page_shows_the_limits_and_a_budget_stop(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    import datetime
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from tradingagents.dashboard import trade
    from tradingagents.execution import alpaca_paper as ap

    for name in (*ap.KEY_ENVS, *ap.SECRET_ENVS):
        monkeypatch.delenv(name, raising=False)
    _fake_langfuse(monkeypatch, tmp_path)
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({"main-m": {"input_per_million_usd": 1.0, "output_per_million_usd": 2.0}}))
    monkeypatch.setenv("E3_PRICING_JSON", str(pricing))
    monkeypatch.setenv("E3_RESULTS_DIR", str(tmp_path / "e3"))
    monkeypatch.setenv(run_budget.CALLS_ENV, "4")
    monkeypatch.setenv(run_budget.COST_ENV, "0.04")
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
                  memory_log_path=str(tmp_path / "memory" / "m.md"), quick_think_llm="main-m", deep_think_llm="main-m",
                  quick_fallback_llm=None, deep_fallback_llm=None)
    monkeypatch.setattr(service, "DEFAULT_CONFIG", config)
    model = Named(model_name="main-m")
    monkeypatch.setattr(e1_routing, "create_tier_client", lambda cfg, tier, **k: _Client(
        model.model_copy(update={"callbacks": k.get("callbacks")})))
    from tests.test_graph_end_to_end import router  # offline vendors, as the `offline` fixture does
    for method, vendors in router.VENDOR_METHODS.items():
        for vendor in vendors:
            monkeypatch.setitem(vendors, vendor, lambda *a, _m=method, **k: f"{_m} data")

    page = AppTest.from_file(str(Path(trade.__file__).with_name("app.py")), default_timeout=30).run()
    assert any("LLM-call limit:** 4 (set by TRADINGAGENTS_DASHBOARD_MAX_LLM_CALLS)" in m.value
               for m in page.sidebar.markdown)
    assert [n.value for n in page.sidebar.number_input if n.label.startswith("Estimated-cost ceiling")] == [0.04]
    next(t for t in page.text_input if t.label == "Ticker").input("NVDA")
    page.date_input[0].set_value(datetime.date.fromisoformat(TRADE_DATE))
    next(b for b in page.button if b.label == "Run analysis").click()
    page.run()
    deadline = time.time() + 60
    while time.time() < deadline and not page.error:
        time.sleep(0.2)
        page.run()
    assert not page.exception
    assert any("Stopped by the per-run limit, not by you" in e.value for e in page.error)
    shown = " ".join(m.value for m in page.markdown)
    assert "Per-run limits: 4 of 4 LLM calls" in shown and "Stopped: call limit reached" in shown
    assert "E3 estimated cost:" in shown
