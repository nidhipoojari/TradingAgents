"""Deadlines (whole analysis and model step, child- and parent-enforced), provider-aware retries,
and the full-analysis budget configuration. Offline: scripted models; the provider SDKs run for real
with only their HTTP layer replaced."""

from __future__ import annotations

import json
import multiprocessing as mp
import time

import pytest
from langchain_core.messages import HumanMessage

from tests.test_e1_e2_e3_audit import Named
from tests.test_failed_run_reporting import _setup
from tests.test_graph_end_to_end import TRADE_DATE, offline  # noqa: F401
from tests.test_run_budget import _e3, _service
from tradingagents import e1_routing, run_budget, service
from tradingagents.run_budget import RunBudget
from tradingagents.run_deadline import RunDeadline


class Slow(Named):
    """Answers after ``delay`` seconds; ``fail`` makes it raise after the delay."""

    delay: float = 0.0
    fail: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("503 UNAVAILABLE: slow and overloaded")
        return super()._generate(messages, stop, run_manager, **kwargs)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    from tests._offline import block_network

    block_network(monkeypatch)


def _usage(tmp_path):
    return json.loads((tmp_path / "results" / "NVDA" / TRADE_DATE / "token_usage.json").read_text())


# ------------------------------------------------------------------ deadlines in the service (child side)
@pytest.mark.unit
def test_a_backup_that_would_start_after_its_step_deadline_is_refused(tmp_path, monkeypatch, offline):  # noqa: F811
    request = _setup(tmp_path, monkeypatch, main=Slow(model_name="main-m", delay=0.6, fail=True),
                     backup=Named(model_name="backup-m"))
    request = request.__class__(**{**request.__dict__, "deadline": RunDeadline(analysis_seconds=60, step_seconds=0.5)})
    with pytest.raises(service.AnalysisDeadlineExceeded) as caught:
        service.run_analysis(request)
    exc = caught.value
    assert exc.deadline["which"] == "step" and "primary, fallback and retries together" in exc.deadline["reason"]
    assert service.error_category(exc) == "timeout" and not isinstance(exc, service.AnalysisBudgetExceeded)
    summary, usage = _e3(tmp_path), _usage(tmp_path)
    assert summary["status"] == "deadline_exceeded" and summary["run_deadline"]["which"] == "step"
    assert usage["status"] == "deadline_exceeded" and usage["run_deadline"]["which"] == "step"
    assert usage["llm_calls_failed"] == 1 and "backup-m" not in json.dumps(summary["per_call"])   # never started


@pytest.mark.unit
def test_the_analysis_deadline_stops_a_run_mid_way_and_keeps_its_record(tmp_path, monkeypatch, offline):  # noqa: F811
    request = _setup(tmp_path, monkeypatch, main=Slow(model_name="main-m", delay=0.25), budget=RunBudget(15, 0.04))
    request = request.__class__(**{**request.__dict__, "deadline": RunDeadline(analysis_seconds=1.0, step_seconds=30)})
    started = time.monotonic()
    with pytest.raises(service.AnalysisDeadlineExceeded) as caught:
        service.run_analysis(request)
    assert time.monotonic() - started < 3.0
    assert caught.value.deadline["which"] == "analysis"
    usage = _usage(tmp_path)
    assert usage["status"] == "deadline_exceeded" and 1 <= usage["llm_calls_succeeded"] < 10
    assert usage["e2_cache"]["misses"] >= 1 and usage["run_budget"]["llm_calls_started"] == usage["llm_calls"]
    assert usage["e3_cost"]["basis"] in ("all_calls", "successful_calls")


@pytest.mark.unit
def test_a_completed_run_reports_its_time_against_the_target(tmp_path, monkeypatch, offline):  # noqa: F811
    request = _setup(tmp_path, monkeypatch, main=Named(model_name="main-m"))
    deadline = RunDeadline(analysis_seconds=180, step_seconds=60, target_seconds=41)
    result = service.run_analysis(request.__class__(**{**request.__dict__, "deadline": deadline}))
    timing = result.usage["run_deadline"]
    assert timing["target_seconds"] == 41 and timing["target_met"] is True and timing["elapsed_seconds"] < 41


@pytest.mark.unit
def test_a_run_whose_deadline_passed_before_it_started_never_runs(tmp_path, monkeypatch, offline):  # noqa: F811
    request = _setup(tmp_path, monkeypatch, main=Named(model_name="main-m"))
    late = RunDeadline(analysis_seconds=5, started_at=time.time() - 10)
    with pytest.raises(service.AnalysisDeadlineExceeded, match="before it started"):
        service.run_analysis(request.__class__(**{**request.__dict__, "deadline": late}))
    assert not (tmp_path / "e3").exists() or not list((tmp_path / "e3").glob("*_e3.json"))


# ------------------------------------------------------------------ deadlines in the worker (parent side)
@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    for name, value in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "offline", "LANGFUSE_SECRET_KEY": "offline",
                        "LANGFUSE_BASE_URL": "http://localhost.invalid", "E3_RESULTS_DIR": str(tmp_path / "e3"),
                        "WORKER_FAKE_MARKER": str(tmp_path / "marker")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("E3_PRICING_JSON", raising=False)
    return tmp_path


def _children_gone(timeout=10.0) -> bool:
    from tradingagents import worker

    end = time.time() + timeout
    while time.time() < end:
        spares = {w.process.pid for p in worker._POOLS.values() if (w := p._ready)}
        if not [c for c in mp.active_children() if c.name == "tradingagents-analysis" and c.is_alive()
                and c.pid not in spares]:
            return True
        time.sleep(0.1)
    return False


def _worker_request(tmp_path, deadline, analysts=("market",), provider=None):
    from dataclasses import replace

    from tests.test_worker import _request

    request = _request(tmp_path, analysts=analysts, deadline=deadline)
    # The suite clears TRADINGAGENTS_*, so its provider is the built-in default (openai), whose client module a
    # fresh worker imports in its first 0.3-1 s (retry_policy.policy_for). Timing-sensitive tests use the
    # configured live provider (google), as the dashboard and MCP server do, so that cost is not in the window.
    return replace(request, config={**request.config, "llm_provider": provider}) if provider else request


def _hung_call_stopped_by_the_parent(worker_env, setup):
    """Run until the parent ends the hung call; (exception, seconds from the hung call's start to the stop)."""
    from tradingagents import worker

    deadline = RunDeadline(analysis_seconds=120, step_seconds=2.0, grace_seconds=1.0)
    calls = []
    with pytest.raises(service.AnalysisDeadlineExceeded) as caught:
        worker.run_in_worker(_worker_request(worker_env, deadline), setup=setup, record_calls=calls)
    stopped_at = time.monotonic()
    [hung] = [c for c in calls if c["end"] is None]
    return caught.value, stopped_at - hung["received_mono"]   # monotonic: a wall-clock step cannot skew it


# The step allowance is timed from when the parent heard of the hung call (the clock the step deadline uses), not from the
# request: worker start-up is not part of a model step, and a cold start takes 1-2 s idle but 8-15 s on a
# loaded machine (measured), which made a request-to-stop bound fail intermittently. 2 s step + the
# parent's 2 s slack + 1 s grace, with 3 s for polling, termination and records under load.
STEP_STOP_BOUND = 2.0 + 2.0 + 1.0 + 3.0


@pytest.mark.unit
def test_a_hung_call_is_ended_by_the_parent_at_the_step_deadline(worker_env):
    exc, after_call_start = _hung_call_stopped_by_the_parent(worker_env, "tests.worker_fakes:hung_market_call")
    assert exc.terminated and exc.deadline["which"] == "step" and exc.deadline["enforced_by"] == "parent"
    assert 2.0 + 2.0 <= after_call_start < STEP_STOP_BOUND      # never before the step allowance, never long after
    assert exc.released_after_seconds < 1.0 + 1.0               # the grace, then terminated
    failure = exc.analysis_failure
    assert failure["status"] == "deadline_exceeded" and failure["error_category"] == "timeout"
    assert failure["llm_calls_in_flight"] == 1
    [summary] = [json.loads(p.read_text()) for p in (worker_env / "e3").glob("*_e3.json")]
    assert summary["status"] == "deadline_exceeded" and summary["terminated"] is True
    assert not service.is_busy() and _children_gone()


@pytest.mark.unit
def test_a_slow_worker_start_does_not_use_up_the_step_deadline(worker_env):
    """Regression: the step deadline counts from the model call, so a slow start neither stops the run before
    its first call nor shortens that call's allowance (the cause of the intermittent failure above)."""
    from tests.worker_fakes import SLOW_STARTUP_SECONDS
    from tradingagents import worker

    requested = time.monotonic()
    exc, after_call_start = _hung_call_stopped_by_the_parent(
        worker_env, "tests.worker_fakes:slow_start_hung_market_call")
    assert exc.terminated and exc.deadline["which"] == "step" and exc.deadline["enforced_by"] == "parent"
    assert 2.0 + 2.0 <= after_call_start < STEP_STOP_BOUND
    assert time.monotonic() - requested >= SLOW_STARTUP_SECONDS + 2.0 + 2.0   # the start-up came on top
    assert exc.analysis_failure["llm_calls_in_flight"] == 1
    assert not service.is_busy() and _children_gone()
    worker.pool_for("tests.worker_fakes:slow_start_hung_market_call").close()   # no 4 s spare left behind


@pytest.mark.unit
def test_a_frozen_worker_is_stopped_by_the_deadline_before_the_watchdog(worker_env):
    from tradingagents import worker

    # A warm worker first: the deadline counts from the request, and under load a cold start can use up
    # 4 s before the run begins (which correctly starts nothing, but this test is about a frozen run).
    worker.prewarm(setup="tests.worker_fakes:freeze_after_two_calls")
    pool = worker.pool_for("tests.worker_fakes:freeze_after_two_calls")
    for _ in range(300):
        if pool._ready is not None:
            break
        time.sleep(0.05)
    assert pool._ready is not None and pool._ready.wait_ready(60)
    deadline = RunDeadline(analysis_seconds=4.0, step_seconds=60, grace_seconds=1.0)
    started = time.monotonic()
    with pytest.raises(service.AnalysisDeadlineExceeded) as caught:
        worker.run_in_worker(_worker_request(worker_env, deadline), setup="tests.worker_fakes:freeze_after_two_calls")
    assert time.monotonic() - started < 4.0 + 1.0 + 6.0          # well before the 30 s heartbeat limit
    failure = caught.value.analysis_failure
    assert caught.value.deadline["which"] == "analysis"
    assert failure["llm_calls_succeeded"] == 2 and failure["worker_checkpoint"]["tool_calls_started"] >= 1
    assert failure["e2_cache"]["complete"] is False
    assert not service.is_busy() and _children_gone()


@pytest.mark.unit
def test_the_deadline_reached_while_calls_run_stops_the_worker_cooperatively(worker_env):
    from tradingagents import worker

    # A warm spare first: the deadline counts from the request, and a cold start (~2.5 s) would use it up
    # before the run began (that case starts nothing and is tested below).
    worker.prewarm(setup="tests.worker_fakes:slow_calls")
    pool = worker.pool_for("tests.worker_fakes:slow_calls")
    for _ in range(300):                       # the spare is created in the background
        if pool._ready is not None:
            break
        time.sleep(0.05)
    assert pool._ready is not None and pool._ready.wait_ready(60)   # (measured from the spare's own start)
    deadline = RunDeadline(analysis_seconds=2.0, step_seconds=60, grace_seconds=5.0)
    with pytest.raises(service.AnalysisDeadlineExceeded) as caught:
        worker.run_in_worker(_worker_request(worker_env, deadline, analysts=("market", "news"), provider="google"),
                             setup="tests.worker_fakes:slow_calls")
    exc = caught.value
    assert exc.deadline["which"] == "analysis" and not getattr(exc, "terminated", False)
    assert exc.analysis_failure["status"] == "deadline_exceeded" and exc.analysis_failure["llm_calls_succeeded"] >= 1
    assert exc.analysis_failure["e2_cache"]["misses"] >= 1                    # what ran is kept
    assert not service.is_busy() and _children_gone()


@pytest.mark.unit
def test_a_deadline_that_passes_before_a_worker_is_ready_starts_nothing(worker_env):
    from tradingagents import worker

    late = RunDeadline(analysis_seconds=1, started_at=time.time() - 5)
    with pytest.raises(service.AnalysisDeadlineExceeded, match="before a worker was ready"):
        worker.run_in_worker(_worker_request(worker_env, late), setup="tests.worker_fakes:scripted")
    assert not service.is_busy()


# ------------------------------------------------------------------ provider-aware retries (real SDKs)
def _openai_like(monkeypatch, status):
    """The openai and anthropic SDKs send through httpx2 (not httpx): answer every request with ``status``."""
    import httpx2

    seen = []

    def send(self, request, **kwargs):
        seen.append(str(request.url))
        return httpx2.Response(status, json={"error": {"message": "overloaded", "type": "server_error"}},
                               request=request)

    monkeypatch.setattr(httpx2.Client, "send", send)
    return seen


@pytest.mark.unit
@pytest.mark.parametrize("provider, key, primary, backup", [
    ("openai", "OPENAI_API_KEY", "gpt-4.1-mini", "gpt-4.1-nano"),
    ("anthropic", "ANTHROPIC_API_KEY", "claude-haiku-4-5", "claude-sonnet-4-5"),
])
@pytest.mark.parametrize("status", [503, 429])
def test_openai_and_anthropic_retries_are_bounded_and_counted(tmp_path, monkeypatch, provider, key, primary,
                                                             backup, status):
    pytest.importorskip({"openai": "langchain_openai", "anthropic": "langchain_anthropic"}[provider])
    monkeypatch.setenv(key, "offline-test-key-not-real")
    seen = _openai_like(monkeypatch, status)
    config = service.config_for_source("dashboard")
    config.update(llm_provider=provider, quick_think_llm=primary, quick_fallback_llm=backup,
                  results_dir=str(tmp_path))
    model = e1_routing.build_model(config, "quick")
    # These SDKs count additional retries: the primary's "no retries" is 0, the backup's "one retry" is 1.
    assert model.runnable.max_retries == 0 and model.fallbacks[0].max_retries == 1
    started = time.monotonic()
    with service._observe_sdk_retries() as retries:
        retries.describe_for(config)
        with pytest.raises(Exception) as caught:
            model.invoke([HumanMessage("hello")])
    assert len(seen) == 3                                # primary 1 attempt + backup 2 attempts
    assert retries.report() == 1                         # the backup's one retry, observed
    assert time.monotonic() - started < 4.0              # 0.5-1 s of SDK backoff (8 s at most)
    assert service.error_category(caught.value) == ("rate_limited" if status == 429 else "provider_unavailable")


@pytest.mark.unit
def test_an_unobservable_provider_reports_retries_as_unavailable():
    counter = service._SdkRetryCounter()
    counter.describe_for({"llm_provider": "google", "deep_think_provider": "bedrock"})
    counter.add()
    assert counter.report() is None and counter.observability == "unavailable for bedrock"
    observed = service._SdkRetryCounter()
    observed.describe_for({"llm_provider": "google"})
    observed.add()
    assert observed.report() == 1 and observed.observability == "observed"


@pytest.mark.unit
def test_the_cli_keeps_its_raw_retry_setting():
    config = dict(service.config_for_source("cli"), llm_provider="openai", quick_think_llm="a",
                  quick_fallback_llm="b", llm_backup_max_retries=5, results_dir="/tmp/x")
    assert e1_routing.build_model(config, "quick").fallbacks[0].max_retries == 5


# ------------------------------------------------------------------ full-analysis budget configuration
@pytest.mark.unit
def test_the_call_limit_follows_the_analysts_and_depth(monkeypatch):
    monkeypatch.delenv(run_budget.CALLS_ENV, raising=False)
    monkeypatch.delenv(run_budget.COST_ENV, raising=False)
    assert run_budget.dashboard_budget(1, 1, 1) == RunBudget(15, 0.04)      # the validated market-only run
    assert run_budget.dashboard_budget(4, 1, 1) == RunBudget(24, 0.04)      # all four analysts
    assert run_budget.dashboard_budget(4, 3, 3).max_llm_calls == 39
    monkeypatch.setenv(run_budget.CALLS_ENV, "20")
    assert run_budget.dashboard_budget(4, 1, 1).max_llm_calls == 20        # an explicit setting wins


@pytest.mark.unit
def test_raising_the_cost_ceiling_needs_approval_and_is_never_automatic(monkeypatch):
    monkeypatch.delenv(run_budget.COST_ENV, raising=False)
    assert run_budget.dashboard_budget(4, 3, 3).max_cost_usd == 0.04       # more analysts: same ceiling
    with pytest.raises(ValueError, match="needs your explicit approval"):
        run_budget.dashboard_budget(1, 1, 1, cost_ceiling=0.10)
    assert run_budget.dashboard_budget(1, 1, 1, cost_ceiling=0.10, approved=True).max_cost_usd == 0.10
    assert run_budget.dashboard_budget(1, 1, 1, cost_ceiling=0.02).max_cost_usd == 0.02   # lower: fine
    monkeypatch.setenv(run_budget.COST_ENV, "off")
    assert run_budget.dashboard_budget(1, 1, 1).max_cost_usd is None       # only when explicitly switched off


@pytest.mark.unit
def test_a_full_four_analyst_run_completes_within_its_derived_limit(tmp_path, monkeypatch, offline):  # noqa: F811
    monkeypatch.delenv(run_budget.CALLS_ENV, raising=False)
    result = _service(tmp_path, monkeypatch, run_budget.dashboard_budget(4, 1, 1))
    snap = result.usage["run_budget"]
    assert result.usage["llm_calls"] == 15 and snap["max_llm_calls"] == 24 and not snap["exhausted"]


@pytest.mark.unit
def test_a_full_run_with_every_primary_failing_is_a_budget_stop_not_a_failure(tmp_path, monkeypatch, offline):  # noqa: F811
    monkeypatch.delenv(run_budget.CALLS_ENV, raising=False)
    with pytest.raises(service.AnalysisBudgetExceeded) as caught:
        _service(tmp_path, monkeypatch, run_budget.dashboard_budget(4, 1, 1), broken=True)
    assert caught.value.budget["llm_calls_started"] == 24 and service.error_category(caught.value) == "budget_exceeded"
    assert _e3(tmp_path)["status"] == "budget_exceeded"


@pytest.mark.unit
def test_the_page_refuses_a_raised_ceiling_without_approval(tmp_path, monkeypatch):
    pytest.importorskip("streamlit")
    import datetime
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from tests.test_analysis_service import _fake_langfuse
    from tradingagents.dashboard import trade
    from tradingagents.execution import alpaca_paper as ap

    for name in (*ap.KEY_ENVS, *ap.SECRET_ENVS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(run_budget.CALLS_ENV, raising=False)
    monkeypatch.delenv(run_budget.COST_ENV, raising=False)
    _fake_langfuse(monkeypatch, tmp_path)
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({"m": {"input_per_million_usd": 1.0, "output_per_million_usd": 2.0}}))
    monkeypatch.setenv("E3_PRICING_JSON", str(pricing))
    page = AppTest.from_file(str(Path(trade.__file__).with_name("app.py")), default_timeout=30).run()
    shown = " ".join(m.value for m in page.sidebar.markdown)
    assert "deadline 180 s, 60 s per model step, target 41 s" in shown
    assert "| 1 | 15 | 30 | 45 |" in shown and "| 4 | 24 | 39 | 54 |" in shown
    next(n for n in page.sidebar.number_input if n.label.startswith("Estimated-cost ceiling")).set_value(0.10).run()
    assert [c.label for c in page.sidebar.checkbox if "approve raising the ceiling" in c.label]
    next(t for t in page.text_input if t.label == "Ticker").input("NVDA")
    page.date_input[0].set_value(datetime.date.fromisoformat(TRADE_DATE))
    next(b for b in page.button if b.label == "Run analysis").click()
    page.run()
    assert any("needs your explicit approval" in e.value for e in page.error)
    assert not any(m.label == "Rating" for m in page.metric)                  # nothing started


@pytest.mark.unit
def test_a_cancel_before_the_deadline_stays_a_cancel(worker_env):
    """The first reason to stop wins: a person's cancel is not relabelled when the deadline passes
    while the hung worker is being stopped."""
    import threading

    from tradingagents import worker

    worker.prewarm(setup="tests.worker_fakes:hung_market_call")      # so the cancel lands mid-call
    pool = worker.pool_for("tests.worker_fakes:hung_market_call")
    for _ in range(300):
        if pool._ready is not None:
            break
        time.sleep(0.05)
    assert pool._ready is not None and pool._ready.wait_ready(60)
    from tests.test_worker import _wait

    cancel = threading.Event()
    marker = worker_env / "marker"
    cancelled_after = {}
    # Cancel once the hung call has begun (the fake writes "blocked" first), not after a fixed second: a fresh
    # worker's first 0.3-1 s varies, and a cancel that lands before the run starts is a different case.
    # The deadline (3.5 s) then passes during the 4 s grace, while the hung worker is being stopped.
    deadline = RunDeadline(analysis_seconds=3.5, step_seconds=60, grace_seconds=1.0)

    def cancel_when_blocked():
        _wait(lambda: marker.exists() and "blocked" in marker.read_text(), 30)
        cancelled_after["s"] = deadline.elapsed()
        cancel.set()

    threading.Thread(target=cancel_when_blocked, daemon=True).start()
    with pytest.raises(service.AnalysisCancelled) as caught:
        worker.run_in_worker(_worker_request(worker_env, deadline, provider="google"), cancel=cancel, grace=4.0,
                             setup="tests.worker_fakes:hung_market_call")
    assert cancelled_after.get("s", 99) < deadline.analysis_seconds, (
        f"precondition not met: the hung call began only {cancelled_after.get('s')} s after the request, after the "
        "deadline; this machine was too slow for this test's timing, which is not a deadline defect")
    assert not isinstance(caught.value, service.AnalysisDeadlineExceeded)
    assert service.error_category(caught.value) == "cancelled" and caught.value.terminated
    assert not service.is_busy() and _children_gone()


# ------------------------------------------------------------------ outcomes kept apart
@pytest.mark.unit
def test_an_sdk_retry_that_recovers_is_a_success_not_a_failure(tmp_path, monkeypatch):
    """The backup's first attempt gets a 503, its one SDK retry succeeds: the step completes, the hidden
    retry is counted, and nothing is reported as a provider failure or a deadline stop."""
    pytest.importorskip("langchain_openai")
    import httpx2

    from tradingagents.e3_tracing import E3MetricsCallback

    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key-not-real")
    answers = iter([503, 503, 200])            # primary: 503 (no retries) -> backup: 503, then 200

    def send(self, request, **kwargs):
        status = next(answers)
        if status != 200:
            return httpx2.Response(status, json={"error": {"message": "overloaded"}}, request=request)
        # langchain-openai 1.8 calls the Responses API (/v1/responses), so answer in that format.
        return httpx2.Response(200, request=request, json={
            "id": "resp_1", "object": "response", "created_at": 0, "status": "completed", "model": "gpt-4.1-nano",
            "output": [{"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}})

    monkeypatch.setattr(httpx2.Client, "send", send)
    config = service.config_for_source("dashboard")
    config.update(llm_provider="openai", quick_think_llm="gpt-4.1-mini", quick_fallback_llm="gpt-4.1-nano",
                  results_dir=str(tmp_path))
    e3 = E3MetricsCallback()
    with service._observe_sdk_retries() as retries:
        retries.describe_for(config)
        answer = e1_routing.build_model(config, "quick", callbacks=[e3]).invoke([HumanMessage("hello")])
    assert answer.content == "ok"
    assert retries.report() == 1                                  # one hidden retry, observed
    primary, backup = e3.records
    assert primary["error_code"] == 503 and backup["error"] is None   # a fallback, then a success
    assert (backup["input_tokens"], backup["output_tokens"]) == (12, 3)
