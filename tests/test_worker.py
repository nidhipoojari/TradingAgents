"""The analysis worker process: a cancelled run releases its slot even when a model call hangs.

Each test runs a real spawned child with offline fakes installed inside it
(tests/worker_fakes.py). No network, no live models.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path

import pytest

from tradingagents import service, worker
from tradingagents.default_config import DEFAULT_CONFIG

DATE = "2026-01-09"


@pytest.fixture(autouse=True)
def fresh_pools():
    """Workers are spawned with the environment of their test; none is reused across tests."""
    for pool in worker._POOLS.values():
        pool.close()
    worker._POOLS.clear()
    yield
    for pool in worker._POOLS.values():
        pool.close()
    worker._POOLS.clear()


@pytest.fixture
def e3_env(tmp_path, monkeypatch):
    for name, value in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "offline", "LANGFUSE_SECRET_KEY": "offline",
                        "LANGFUSE_BASE_URL": "http://localhost.invalid", "E3_RESULTS_DIR": str(tmp_path / "e3"),
                        "WORKER_FAKE_MARKER": str(tmp_path / "marker")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("E3_PRICING_JSON", raising=False)
    return tmp_path


def _request(tmp_path, analysts=("market", "news"), source="dashboard", **change):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
                  memory_log_path=str(tmp_path / "memory.md"), e2_cache_enabled=True)
    return service.AnalysisRequest(ticker="NVDA", trade_date=change.pop("trade_date", DATE), source=source,
                                   analysts=analysts, config=config, **change)


def _summaries(tmp_path):
    return [json.loads(p.read_text()) for p in sorted((tmp_path / "e3").glob("*_e3.json"))]


@pytest.mark.unit
def test_a_worker_run_matches_the_in_process_service(e3_env):
    events = []
    result = worker.run_in_worker(_request(e3_env), on_event=events.append, setup="tests.worker_fakes:scripted")

    assert result.rating == "Overweight" and result.source == "dashboard"
    assert result.graph is None and "messages" not in result.final_state
    assert result.usage["e2_cache"]["misses"] > 0                          # E2 inside the child
    [summary] = _summaries(e3_env)                                          # E3 saved by the child
    assert summary["status"] == "success" and summary["run_id"] == result.run_id
    assert summary["source"] == "dashboard" and "unattributed" not in summary["per_agent"]
    assert summary["observed_llm_calls"] == result.usage["llm_calls"]
    assert any(e.kind == "step" for e in events) and events[-1].progress[0] == events[-1].progress[1]
    assert all(isinstance(m, worker.PortableMessage) for e in events for m in e.messages)
    assert not service.is_busy()


@pytest.mark.unit
def test_a_hung_model_call_is_cut_off_and_the_slot_released(e3_env):
    marker = e3_env / "marker"
    cancel = threading.Event()
    cancelled_at = {}

    def cancel_once_blocked():
        while not marker.exists():
            time.sleep(0.02)
        cancelled_at["t"] = time.monotonic()
        cancel.set()

    threading.Thread(target=cancel_once_blocked, daemon=True).start()
    outcome = {}

    def run():
        try:
            worker.run_in_worker(_request(e3_env), cancel=cancel, grace=1.0, setup="tests.worker_fakes:hung_market_call")
        except service.AnalysisCancelled as exc:
            outcome["exc"], outcome["t"] = exc, time.monotonic()

    runner = threading.Thread(target=run)
    runner.start()
    assert _wait(lambda: "t" in cancelled_at, 60)
    # While the hung call holds the slot, nothing else may start.
    assert service.is_busy()
    with pytest.raises(service.AnalysisBusyError):
        worker.run_in_worker(_request(e3_env), setup="tests.worker_fakes:scripted")
    runner.join(30)

    exc = outcome["exc"]
    released_after = outcome["t"] - cancelled_at["t"]
    assert exc.terminated is True and 1.0 <= released_after < 6.0
    assert exc.in_flight_model_calls >= 1 and "may still bill" in str(exc)
    assert not service.is_busy()
    summary = json.loads(Path(exc.e3_summary_path).read_text())             # written by the parent
    assert summary["status"] == "cancelled" and summary["terminated"] is True
    assert summary["run_id"] == exc.run_id and summary["source"] == "dashboard"
    # On cancel the child flushed Langfuse at once: completed calls' traces went out before termination.
    assert summary["langfuse_flush"] == "ok" and summary["langfuse_flushed"] is True
    assert summary["trace_complete"] is False and summary["incomplete_calls"] >= 1
    assert "may still bill" in summary["billing_note"] and "not estimated" in summary["billing_note"]
    incomplete = [c for c in summary["per_call"] if not c["complete"]]
    assert incomplete and all(c["input_tokens"] is None and c["output_tokens"] is None for c in incomplete)
    assert all(c["input_tokens"] == 100 for c in summary["per_call"] if c["complete"])   # reported, not estimated
    assert {"agent": "Market Analyst", "model": "gemini-test"} in summary["in_flight_at_termination"]
    assert summary["observed_llm_calls"] >= 1
    # Released for real: the next analysis runs at once.
    assert worker.run_in_worker(_request(e3_env), setup="tests.worker_fakes:scripted").rating == "Overweight"


@pytest.mark.unit
def test_a_cooperative_cancel_needs_no_termination(e3_env):
    cancel = threading.Event()

    def on_event(event):
        if event.kind == "step":
            cancel.set()

    started = time.monotonic()
    with pytest.raises(service.AnalysisCancelled) as caught:
        worker.run_in_worker(_request(e3_env), on_event=on_event, cancel=cancel, grace=20.0,
                             setup="tests.worker_fakes:slow_calls")
    assert caught.value.terminated is False
    assert time.monotonic() - started < 20
    [summary] = _summaries(e3_env)                                          # the child's own E3 summary
    assert summary["status"] == "cancelled" and "terminated" not in summary
    assert not service.is_busy()


@pytest.mark.unit
def test_a_worker_error_comes_back_as_the_right_error(e3_env):
    with pytest.raises(ValueError, match="future"):
        worker.run_in_worker(_request(e3_env, trade_date="2999-01-01"), setup="tests.worker_fakes:scripted")
    assert not service.is_busy()


@pytest.mark.unit
def test_the_suite_and_the_cli_run_in_process():
    assert worker.worker_mode() == "inline"
    assert worker.analysis_runner() is service.run_analysis


def _wait(condition, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.unit
def test_a_warm_worker_uses_the_environment_of_the_request_not_of_its_start(tmp_path, monkeypatch):
    """The pool spawns the next worker early; settings changed since must still apply."""
    monkeypatch.setenv("E3_TRACE_ENABLED", "")
    worker.run_in_worker(_request(tmp_path / "a"), setup="tests.worker_fakes:scripted")   # leaves a warm worker
    assert _wait(lambda: worker._POOLS["tests.worker_fakes:scripted"]._ready is not None, 30)
    for name, value in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "offline", "LANGFUSE_SECRET_KEY": "offline",
                        "LANGFUSE_BASE_URL": "http://localhost.invalid", "E3_RESULTS_DIR": str(tmp_path / "e3")}.items():
        monkeypatch.setenv(name, value)
    result = worker.run_in_worker(_request(tmp_path / "b"), setup="tests.worker_fakes:scripted")
    [summary] = _summaries(tmp_path)                                           # E3 on, as set after the spawn
    assert summary["run_id"] == result.run_id


@pytest.mark.unit
def test_the_mcp_tool_runs_in_a_worker_and_a_timeout_frees_the_server(e3_env, monkeypatch):
    fastmcp = pytest.importorskip("fastmcp")
    import asyncio
    from functools import partial

    from fastmcp.exceptions import ToolError

    from tradingagents import mcp_server

    defaults = copy.deepcopy(DEFAULT_CONFIG)
    defaults.update(results_dir=str(e3_env / "results"), data_cache_dir=str(e3_env / "cache"),
                    memory_log_path=str(e3_env / "memory" / "trading_memory.md"), e2_cache_enabled=True)
    monkeypatch.setattr(service, "DEFAULT_CONFIG", defaults)
    args = {"ticker": "NVDA", "trade_date": DATE, "analysts": ["market", "news"]}

    async def scenario():
        ok = mcp_server.create_server(run=partial(worker.run_in_worker, setup="tests.worker_fakes:scripted"))
        async with fastmcp.Client(ok) as client:
            answer = (await client.call_tool("analyze_stock", args)).data
        hung = mcp_server.create_server(timeout=1.0, run=partial(worker.run_in_worker, grace=1.0,
                                                                 setup="tests.worker_fakes:hung_market_call"))
        async with fastmcp.Client(hung) as client:
            started = time.monotonic()
            with pytest.raises(ToolError, match="timed out"):
                await client.call_tool("analyze_stock", args)
            answered_in = time.monotonic() - started
            for _ in range(200):
                if not service.is_busy():
                    break
                await asyncio.sleep(0.05)
            freed_in = time.monotonic() - started
        return answer, answered_in, freed_in

    answer, answered_in, freed_in = asyncio.run(scenario())
    assert answer["source"] == "mcp" and answer["rating"] == "Overweight"
    assert answer["usage"]["e2_cache"]["misses"] > 0
    summaries = {s["run_id"]: s for s in _summaries(e3_env)}
    assert summaries[answer["run_id"]]["source"] == "mcp"
    assert answered_in < 8 and freed_in < 12 and not service.is_busy()
    # The MCP memory log, written by the child, is the MCP one.
    assert (e3_env / "memory" / service.MCP_MEMORY_LOG_NAME).exists()
    assert not (e3_env / "memory" / "trading_memory.md").exists()


@pytest.mark.unit
def test_a_dashboard_job_runs_in_a_worker(e3_env):
    from functools import partial

    from tradingagents.dashboard import jobs

    job = jobs.start_job(_request(e3_env), run=partial(worker.run_in_worker, setup="tests.worker_fakes:scripted"))
    assert _wait(lambda: not job.running, 60)
    assert job.status == "done" and job.result.source == "dashboard"
    assert any(line.startswith(("AI:", "Tool call")) for line in job.log())
    [summary] = _summaries(e3_env)
    assert summary["source"] == "dashboard" and summary["run_id"] == job.run_id


# ------------------------------------------------------------------ E3 when the worker is terminated
@pytest.mark.unit
def test_the_parents_call_records_match_e3s_own(e3_env):
    """Termination accounting rests on the same per-call numbers E3 records itself."""
    calls = []
    result = worker.run_in_worker(_request(e3_env), setup="tests.worker_fakes:scripted", record_calls=calls)
    [summary] = _summaries(e3_env)
    own = sorted((c["agent"], c["model"], c["input_tokens"], c["output_tokens"]) for c in summary["per_call"])
    parents = sorted((c["agent"], c["model"], c["input_tokens"], c["output_tokens"]) for c in calls)
    assert parents == own and len(own) == result.usage["llm_calls"]
    assert all(c["end"] is not None for c in calls)


@pytest.mark.unit
def test_a_slow_langfuse_cannot_stall_shutdown(e3_env):
    marker = e3_env / "marker"
    cancel = threading.Event()
    threading.Thread(target=lambda: (_wait(marker.exists, 60), cancel.set()), daemon=True).start()
    started = time.monotonic()
    with pytest.raises(service.AnalysisCancelled) as caught:
        worker.run_in_worker(_request(e3_env), cancel=cancel, grace=1.0,
                             setup="tests.worker_fakes:slow_langfuse_hung_call")
    assert caught.value.terminated and time.monotonic() - started < 30
    summary = json.loads(Path(caught.value.e3_summary_path).read_text())
    assert summary["langfuse_flush"] in ("timed_out", "not attempted") and summary["langfuse_flushed"] is False


@pytest.mark.unit
def test_e3_save_bounds_its_langfuse_flush(tmp_path, monkeypatch):
    from tradingagents.e3_cli_hook import E3Session
    from tradingagents.e3_tracing import E3MetricsCallback

    class SlowClient:
        def flush(self):
            time.sleep(600)

    monkeypatch.setenv("E3_RESULTS_DIR", str(tmp_path))
    monkeypatch.setenv("E3_FLUSH_TIMEOUT_SECONDS", "0.3")
    session = E3Session("NVDA", DATE, callbacks=[E3MetricsCallback()], client=SlowClient(), run_id="r1")
    started = time.monotonic()
    session.save()
    assert time.monotonic() - started < 3
    assert json.loads(session.saved_path.read_text())["langfuse_flush"] == "timed_out"


# ------------------------------------------------------------------ start-up, watchdog, spare
@pytest.mark.unit
def test_a_worker_that_never_becomes_ready_is_replaced_then_reported(e3_env, caplog):
    started = time.monotonic()
    with caplog.at_level("WARNING", logger="tradingagents.worker"), pytest.raises(worker.WorkerStartupError,
                                                                                  match="not ready after"):
        worker.run_in_worker(_request(e3_env), ready_timeout=1.5, setup="tests.worker_fakes:hang_at_startup")
    assert time.monotonic() - started < 15
    assert worker._POOLS["tests.worker_fakes:hang_at_startup"].replacements == 2
    assert sum("failed to start" in r.message for r in caplog.records) == 2          # diagnostics, per worker
    assert not service.is_busy()


@pytest.mark.unit
def test_a_worker_that_hangs_once_at_startup_is_recovered(e3_env, caplog):
    with caplog.at_level("WARNING", logger="tradingagents.worker"):
        result = worker.run_in_worker(_request(e3_env), ready_timeout=8.0, setup="tests.worker_fakes:hang_first_startup")
    assert result.rating == "Overweight"
    assert worker._POOLS["tests.worker_fakes:hang_first_startup"].replacements == 1
    assert any("not ready after" in r.message for r in caplog.records)


@pytest.mark.unit
def test_a_startup_crash_is_diagnosed(e3_env, caplog):
    with caplog.at_level("WARNING", logger="tradingagents.worker"), pytest.raises(
            worker.WorkerStartupError, match="simulated import failure"):
        worker.run_in_worker(_request(e3_env), ready_timeout=10, setup="tests.worker_fakes:crash_at_startup")
    assert not service.is_busy()


@pytest.mark.unit
def test_the_watchdog_terminates_a_frozen_worker(e3_env):
    started = time.monotonic()
    with pytest.raises(worker.WorkerUnresponsive, match="no heartbeat") as caught:
        worker.run_in_worker(_request(e3_env), heartbeat_timeout=2.0, setup="tests.worker_fakes:freeze_during_call")
    assert time.monotonic() - started < 30 and not service.is_busy()
    summary = json.loads(Path(caught.value.e3_summary_path).read_text())
    assert summary["status"] == "failed" and summary["terminated"] is True and summary["incomplete_calls"] >= 1


@pytest.mark.unit
def test_a_dead_spare_is_replaced_on_use(e3_env, caplog):
    pool = worker.pool_for("tests.worker_fakes:scripted")
    pool.replenish()
    assert _wait(lambda: pool._ready is not None, 30)
    pool._ready.process.kill()
    pool._ready.process.join(5)
    with caplog.at_level("WARNING", logger="tradingagents.worker"):
        result = worker.run_in_worker(_request(e3_env), setup="tests.worker_fakes:scripted")
    assert result.rating == "Overweight" and pool.replacements == 1
    assert any("died while waiting as a spare" in r.message for r in caplog.records)


@pytest.mark.unit
def test_starting_a_worker_builds_no_model_and_calls_no_api(e3_env):
    marker = e3_env / "marker"
    pool = worker.pool_for("tests.worker_fakes:record_startup_side_effects")
    spare = pool.take(ready_timeout=60)
    assert spare.ready and spare.startup_seconds > 0
    time.sleep(0.5)
    assert not marker.exists()                      # no model client, no Langfuse client before a request
    spare.stop()
    worker.run_in_worker(_request(e3_env), setup="tests.worker_fakes:record_startup_side_effects")
    assert "model client" in marker.read_text()     # built only once the request arrived


@pytest.mark.unit
def test_a_finished_child_exits_in_the_background(e3_env):
    """The slot is released once the result is in; the child's teardown does not delay the caller,
    and the finished child does not linger."""
    import multiprocessing as mp

    before = {c.pid for c in mp.active_children()}
    started = time.monotonic()
    result = worker.run_in_worker(_request(e3_env), setup="tests.worker_fakes:scripted")
    assert result.rating == "Overweight" and not service.is_busy()
    pool = worker._POOLS["tests.worker_fakes:scripted"]

    def only_the_spare_left():
        spare = pool._ready.process if pool._ready is not None else None
        return all(c is spare for c in mp.active_children()
                   if c.name == "tradingagents-analysis" and c.pid not in before)

    assert _wait(only_the_spare_left, 30)
    assert time.monotonic() - started < 60


@pytest.mark.unit
def test_a_pool_closed_while_its_spare_starts_keeps_no_worker(e3_env):
    import multiprocessing as mp

    pool = worker.WorkerPool("tests.worker_fakes:scripted")
    pool.replenish()
    pool.close()                                   # before the spare has finished starting
    time.sleep(6)
    assert pool._ready is None
    assert not [c for c in mp.active_children() if c.name == "tradingagents-analysis" and c.is_alive()
                and c.pid not in {w.process.pid for p in worker._POOLS.values() if (w := p._ready)}]


@pytest.mark.unit
def test_the_scripted_worker_model_binds_tools_with_an_e1_backup():
    """E1 with a backup model calls with_fallbacks(...).bind_tools(...), which evaluates the
    ``-> Runnable`` annotation; on Python 3.14 it must resolve in worker_fakes' globals."""
    from langchain_core.tools import tool

    from tests import worker_fakes

    @tool
    def get_stock_data(symbol: str) -> str:
        """Prices for a symbol."""
        return "prices"

    main, backup = worker_fakes._scripted(), worker_fakes._scripted()
    bound = main.with_fallbacks([backup]).bind_tools([get_stock_data])
    assert {t.name for t in bound.runnable.tools} == {"get_stock_data"}
