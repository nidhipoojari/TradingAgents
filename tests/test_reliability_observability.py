"""The six reliability/observability findings after the live F1 503 incident, tested offline.

1 worker checkpoints (E2 / tool counts survive termination)   2 usage of failed calls
3 budget accounting reconciled with E3                         4 Gemini SDK retries and timeouts
5 MCP failure reporting                                         6 optional Playwright setup
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
import time
import tomllib
from functools import partial
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult

from tests.test_graph_end_to_end import offline  # noqa: F401
from tests.test_run_budget import _e3, _service
from tradingagents import e1_routing, service
from tradingagents.e3_tracing import E3MetricsCallback
from tradingagents.run_budget import BudgetGuard, RunBudget

ROOT = Path(__file__).resolve().parents[1]
PRICES = {"m": {"input_per_million_usd": 1.0, "output_per_million_usd": 2.0}}


# ------------------------------------------------------------------ 2. usage of failed calls
class _Stream(BaseChatModel):
    """Streams one chunk with provider usage, then fails (or finishes) - as a broken stream would."""

    fail: bool = True
    usage: bool = True
    model_name: str = "m"

    @property
    def _llm_type(self) -> str:
        return "stream-test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise NotImplementedError

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        meta = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150} if self.usage else None
        yield ChatGenerationChunk(message=AIMessageChunk(content="partial", usage_metadata=meta))
        if self.fail:
            error = RuntimeError("503 UNAVAILABLE: stream interrupted")
            error.code = 503
            raise error



def _call(model, callbacks):
    with contextlib.suppress(RuntimeError):          # the failing stream re-raises after the callbacks
        list(model.stream([HumanMessage("hello")], config={"callbacks": callbacks,
                                                             "metadata": {"ls_model_name": "m"}}))


@pytest.mark.unit
def test_a_failed_call_that_reported_usage_keeps_it():
    e3, budget = E3MetricsCallback(), BudgetGuard(RunBudget(10, 1.0), threading.Event(), pricing=PRICES)
    _call(_Stream(), [budget, e3])
    [record] = e3.records
    assert (record["input_tokens"], record["output_tokens"], record["error_code"]) == (120, 30, 503)
    summary = e3.summary(ticker="T", analysis_date="2026-01-09", status="failed", wall_time_seconds=1.0,
                         pricing=PRICES)
    assert summary["per_call"][0]["usage_reported"] is True and summary["failed_calls_with_usage"] == 1
    snap = budget.snapshot()
    assert snap["failed_calls_with_usage"] == 1
    assert snap["reported_usage_cost_usd"] == round((120 * 1 + 30 * 2) / 1e6, 6) == summary["known_usage_cost_usd"]


@pytest.mark.unit
def test_a_failed_call_without_usage_is_unreported_not_zero():
    e3 = E3MetricsCallback()
    _call(_Stream(usage=False), [e3])
    [record] = e3.records
    assert record["input_tokens"] is None and record["output_tokens"] is None     # not 0
    summary = e3.summary(ticker="T", analysis_date="2026-01-09", status="failed", wall_time_seconds=1.0,
                         pricing=PRICES)
    assert summary["per_call"][0]["usage_reported"] is False
    assert summary["failed_calls_without_usage"] == 1 and summary["estimated_cost_usd"] is None


@pytest.mark.unit
def test_a_successful_call_is_recorded_as_before():
    e3 = E3MetricsCallback()
    _call(_Stream(fail=False), [e3])
    [record] = e3.records
    assert record["error"] is None and record["error_code"] is None and record["input_tokens"] == 120


# ------------------------------------------------------------------ 4. Gemini SDK retries and timeouts
@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    from tests._offline import block_network

    block_network(monkeypatch)


@pytest.fixture
def gemini_503(monkeypatch):
    """The real Gemini client and SDK retry logic; only the HTTP request is replaced (no network)."""
    pytest.importorskip("langchain_google_genai")
    import google.genai._api_client as api
    from google.genai import errors

    monkeypatch.setenv("GOOGLE_API_KEY", "offline-test-key-not-real")
    attempts: list[str] = []
    status = {"code": 503, "status": "UNAVAILABLE"}

    def request_once(self, http_request, stream=False):
        attempts.append(re.search(r"models/([^:]+)", http_request.url).group(1))
        error_type = errors.ServerError if status["code"] >= 500 else errors.ClientError
        raise error_type(status["code"], {"error": {"code": status["code"], "status": status["status"],
                                                    "message": "This model is currently experiencing high demand."}},
                         None)

    monkeypatch.setattr(api.BaseApiClient, "_request_once", request_once)
    return attempts, status


def _gemini_config(tmp_path, **overrides):
    config = service.config_for_source("dashboard")
    config.update(llm_provider="google", quick_think_llm="gemini-3.5-flash-lite",
                  quick_fallback_llm="gemini-3.1-flash-lite", results_dir=str(tmp_path), **overrides)
    return config


def _invoke(model):
    started = time.monotonic()
    with pytest.raises(Exception) as caught:
        model.invoke([HumanMessage("hello")])
    return caught.value, time.monotonic() - started


@pytest.mark.unit
def test_a_persistent_503_fails_over_at_once_and_the_backup_is_bounded(tmp_path, gemini_503):
    attempts, _ = gemini_503
    e3 = E3MetricsCallback()
    model = e1_routing.build_model(_gemini_config(tmp_path), "quick", callbacks=[e3])
    with service._observe_sdk_retries() as retries:
        error, seconds = _invoke(model)
    # Primary: E1 asks for 0 retries, which google-genai 3.3 treats as 1 attempt. Backup: the dashboard
    # and MCP bound of 2 attempts, i.e. one retry after about 1-2 s of backoff.
    assert attempts == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.1-flash-lite"]
    assert retries.count == 1                                    # the hidden retry, observed
    assert seconds < 4.0
    assert service.error_category(error) == "provider_unavailable"
    assert [r["error_code"] for r in e3.records] == [503, 503]   # two observable calls, both 503
    assert "switching to gemini-3.1-flash-lite" in (tmp_path / "e1_fallback_log.txt").read_text()


@pytest.mark.unit
def test_a_persistent_429_is_bounded_the_same_way(tmp_path, gemini_503):
    attempts, status = gemini_503
    status.update(code=429, status="RESOURCE_EXHAUSTED")
    error, seconds = _invoke(e1_routing.build_model(_gemini_config(tmp_path), "quick"))
    assert len(attempts) == 3 and seconds < 4.0
    assert service.error_category(error) == "rate_limited"


@pytest.mark.unit
def test_the_sdk_default_explains_the_live_36_seconds(tmp_path, gemini_503, monkeypatch):
    """Without the bound (the CLI keeps Phase 2's default): 6 attempts, ~31-36 s of backoff."""
    import tenacity.nap

    attempts, _ = gemini_503
    waits: list[float] = []
    monkeypatch.setattr(tenacity.nap, "time", type("T", (), {"sleep": staticmethod(waits.append)}))
    config = _gemini_config(tmp_path, llm_backup_max_retries=None, llm_backup_retries=None)
    _invoke(e1_routing.build_model(config, "quick"))
    assert attempts.count("gemini-3.1-flash-lite") == 6 and attempts.count("gemini-3.5-flash-lite") == 1
    assert len(waits) == 5
    for n, wait in enumerate(waits):                     # 1, 2, 4, 8, 16 s, each plus up to 1 s of jitter
        assert 2 ** n <= wait <= 2 ** n + 1
    assert 31 <= sum(waits) <= 36                        # the live backup took 36.1 s


@pytest.mark.unit
def test_dashboard_and_mcp_runs_are_bounded_and_the_cli_is_not():
    for source in ("dashboard", "mcp"):
        config = service.config_for_source(source)
        assert config["llm_backup_retries"] == 1 and config["llm_timeout"] == 30.0
        assert config["llm_backup_max_retries"] is None              # the provider-neutral setting is used
    cli = service.config_for_source("cli")
    assert cli.get("llm_backup_retries") is None and cli["llm_backup_max_retries"] is None
    assert cli["llm_timeout"] is None                                   # Phase 2 unchanged


@pytest.mark.unit
def test_an_explicit_setting_wins_over_the_bound(monkeypatch):
    base = dict(service.DEFAULT_CONFIG, llm_backup_max_retries=4, llm_timeout=15.0)
    config = service.config_for_source("dashboard", base)
    assert (config["llm_backup_max_retries"], config["llm_timeout"]) == (4, 15.0)


@pytest.mark.unit
def test_the_timeout_reaches_the_gemini_client(tmp_path, gemini_503):
    model = e1_routing.build_model(_gemini_config(tmp_path), "quick")
    assert model.runnable.timeout == 30.0 and model.fallbacks[0].timeout == 30.0
    # Gemini counts attempts: the primary's "no retries" is 1 attempt, the backup's "one retry" is 2.
    assert model.runnable.max_retries == 1 and model.fallbacks[0].max_retries == 2


# ------------------------------------------------------------------ 3. budget accounting reconciled with E3
@pytest.mark.unit
def test_budget_reported_usage_equals_e3s_known_usage(tmp_path, monkeypatch, offline):  # noqa: F811
    result = _service(tmp_path, monkeypatch, RunBudget(40, 1.0))
    snap, summary = result.usage["run_budget"], _e3(tmp_path)
    assert snap["reported_usage_cost_usd"] == summary["known_usage_cost_usd"] == summary["estimated_cost_usd"]
    assert snap["unreported_usage_estimate_usd"] == 0 and snap["unpriced_usage_estimate_usd"] == 0
    assert snap["conservative_total_usd"] == snap["budget_cost_usd"] + snap["reserved_in_flight_usd"]


@pytest.mark.unit
def test_with_fallbacks_the_extra_is_the_estimate_for_calls_without_usage(tmp_path, monkeypatch, offline):  # noqa: F811
    with pytest.raises(service.AnalysisBudgetExceeded):
        _service(tmp_path, monkeypatch, RunBudget(8, 1.0), broken=True)
    snap, summary = _e3(tmp_path)["run_budget"], _e3(tmp_path)
    assert snap["reported_usage_cost_usd"] == summary["known_usage_cost_usd"]           # the same tokens, priced alike
    assert snap["unreported_usage_estimate_usd"] > 0 and snap["failed_calls"] == summary["observed_llm_errors"]
    assert snap["budget_cost_usd"] == round(snap["reported_usage_cost_usd"] + snap["unreported_usage_estimate_usd"], 6)
    from tradingagents.run_budget import describe_snapshot

    line = describe_snapshot(snap)
    assert "conservative, not a bill" in line and "estimated for calls that reported no usage" in line


# ------------------------------------------------------------------ 1. worker checkpoints
@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    for name, value in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "offline", "LANGFUSE_SECRET_KEY": "offline",
                        "LANGFUSE_BASE_URL": "http://localhost.invalid", "E3_RESULTS_DIR": str(tmp_path / "e3"),
                        "WORKER_FAKE_MARKER": str(tmp_path / "marker")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("E3_PRICING_JSON", raising=False)
    return tmp_path


@pytest.mark.unit
def test_a_frozen_worker_still_reports_its_last_checkpoint(worker_env):
    from tests.test_worker import _request
    from tradingagents import worker

    with pytest.raises(worker.WorkerUnresponsive) as caught:
        worker.run_in_worker(_request(worker_env, analysts=("market",)), heartbeat_timeout=3.0,
                             setup="tests.worker_fakes:freeze_after_two_calls")
    failure = caught.value.analysis_failure
    view = failure["worker_checkpoint"]
    assert failure["llm_calls_succeeded"] == 2 and failure["tool_calls"] == view["tool_calls_started"] >= 1
    assert failure["e2_cache"]["misses"] >= 1 and failure["e2_cache"]["complete"] is False
    assert view["taken_seconds_before_failure"] >= 0 and "lower bound" in view["basis"]
    assert failure["error_category"] == "worker_failure"
    assert not service.is_busy()


@pytest.mark.unit
def test_a_terminated_cancel_keeps_the_checkpoint(worker_env):
    from tests.test_worker import _request
    from tradingagents import worker

    cancel, seen = threading.Event(), []

    def on_event(event):
        seen.append(event)
        if len(seen) >= 3:                       # news has run its tools; market's call hangs
            cancel.set()

    with pytest.raises(service.AnalysisCancelled) as caught:
        worker.run_in_worker(_request(worker_env), on_event=on_event, cancel=cancel, grace=1.0,
                             setup="tests.worker_fakes:hung_market_call")
    assert caught.value.terminated and caught.value.worker_checkpoint["tool_calls_started"] >= 1
    [summary] = [json.loads(p.read_text()) for p in (worker_env / "e3").glob("*_e3.json")]
    assert summary["status"] == "cancelled" and summary["worker_checkpoint"]["e2_cache"] is not None


@pytest.mark.unit
def test_a_completed_run_is_unchanged_by_checkpoints(worker_env):
    from tests.test_worker import _request
    from tradingagents import worker

    result = worker.run_in_worker(_request(worker_env, analysts=("market",)), setup="tests.worker_fakes:scripted")
    assert "worker_checkpoint" not in result.usage and result.usage["tool_calls"] >= 1
    assert set(result.usage) == {"llm_calls", "tool_calls", "tokens_in", "tokens_out", "tokens_total",
                                 "llm_provider", "quick_think_llm", "deep_think_llm", "elapsed_seconds",
                                 "e2_cache", "run_id", "source"}


# ------------------------------------------------------------------ 5. MCP failure reporting
@pytest.mark.unit
def test_an_mcp_failure_returns_run_id_category_and_partial_metrics(monkeypatch):
    pytest.importorskip("fastmcp")
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from tradingagents import mcp_server

    secret = "AIza" + "Q" * 35
    monkeypatch.setenv("GOOGLE_API_KEY", secret)
    record = {"run_id": "20261123-113910-4266de44", "llm_calls": 4, "llm_calls_succeeded": 2,
              "llm_calls_failed": 2, "tokens_in": 7021, "tokens_out": 1464, "tool_calls": 3,
              "e1_fallback_events": ["2026-11-23 11:39:15 | quick tier | m failed (503) -> switching to b",
                                     "2026-11-23 11:39:19 | quick tier | m failed (503) -> switching to b"],
              "e2_cache": {"hits": 0, "misses": 5, "saved_entries": 5},
              "e3_cost": {"estimated_cost_usd": 0.005766, "basis": "successful_calls", "partial_cost_usd": None,
                          "note": "n"}, "e3_summary_path": "/home/someone/results/e3/x_e3.json",
              "sdk_retries_observed": 5}

    def fails(request, on_event=None, cancel=None):
        error = RuntimeError(f'GoogleAPIError: 503 UNAVAILABLE key={secret} Traceback (most recent call last): '
                             'File "/home/someone/x.py", line 3')
        error.analysis_failure = record
        raise error

    async def go():
        async with Client(mcp_server.create_server(run=fails)) as client:
            await client.call_tool("analyze_stock", {"ticker": "NVDA", "trade_date": "2026-10-23"})

    with pytest.raises(ToolError) as caught:
        asyncio.run(go())
    text = str(caught.value)
    assert text.startswith("analysis failed [provider_unavailable] run 20261123-113910-4266de44: ")
    assert secret not in text and "/home/someone/results" not in text and "e3_summary_path" not in text
    partial_metrics = json.loads(text.split("partial metrics: ", 1)[1])
    assert partial_metrics["llm_calls_succeeded"] == 2 and partial_metrics["e1_fallback_switches"] == 2
    assert partial_metrics["e3_cost"]["basis"] == "successful_calls" and partial_metrics["sdk_retries_observed"] == 5
    assert "not complete" in partial_metrics["note"]


@pytest.mark.unit
def test_an_mcp_failure_through_the_worker_carries_its_record(worker_env):
    pytest.importorskip("fastmcp")
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from tradingagents import mcp_server, worker

    async def go():
        server = mcp_server.create_server(run=partial(worker.run_in_worker,
                                                      setup="tests.worker_fakes:fail_after_two_calls"))
        async with Client(server) as client:
            await client.call_tool("analyze_stock", {"ticker": "NVDA", "trade_date": "2026-01-09",
                                                     "analysts": ["market"]})

    with pytest.raises(ToolError) as caught:
        asyncio.run(go())
    text = str(caught.value)
    assert re.match(r"analysis failed \[provider_unavailable\] run \d{8}-\d{6}-[0-9a-f]{8}: ", text)
    partial_metrics = json.loads(text.split("partial metrics: ", 1)[1])
    # MCP runs use the configured defaults, where E2 is off here, so there are no E2 counts to report.
    assert partial_metrics["llm_calls_succeeded"] == 2 and partial_metrics["tool_calls"] >= 1
    assert "e2_cache" not in partial_metrics and "Traceback" not in text


@pytest.mark.unit
def test_an_mcp_timeout_names_its_run(monkeypatch):
    pytest.importorskip("fastmcp")
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from tradingagents import mcp_server

    def slow(request, on_event=None, cancel=None):
        cancel.wait(5)
        raise service.AnalysisCancelled(request.run_id, "after 0 report(s)")

    async def go():
        async with Client(mcp_server.create_server(run=slow, timeout=0.5)) as client:
            await client.call_tool("analyze_stock", {"ticker": "NVDA", "trade_date": "2026-10-23"})

    with pytest.raises(ToolError, match=r"timed out .* \[timeout\] run \d{8}-\d{6}-[0-9a-f]{8}"):
        asyncio.run(go())


# ------------------------------------------------------------------ 6. optional Playwright setup
@pytest.mark.unit
def test_playwright_is_an_optional_testing_extra_only():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["optional-dependencies"]["e2e"] == ["playwright==1.52.0"]
    assert not any("playwright" in dep for dep in project["dependencies"])
    for extra, deps in project["optional-dependencies"].items():
        if extra != "e2e":
            assert not any("playwright" in dep for dep in deps), extra


@pytest.mark.unit
def test_the_setup_script_is_valid_and_handles_ubuntu_26_04():
    import subprocess

    script = ROOT / "scripts" / "smoke" / "setup_playwright.sh"
    assert script.stat().st_mode & 0o111
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    text = script.read_text()
    assert "PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64" in text and "--user-libs" in text
    assert "PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1" in text


@pytest.mark.unit
def test_the_live_script_says_how_to_get_a_browser_when_none_is_installed(monkeypatch):
    import builtins
    import importlib.util
    import sys

    monkeypatch.syspath_prepend(str(ROOT / "scripts" / "smoke"))
    spec = importlib.util.spec_from_file_location("live_dashboard_t", ROOT / "scripts" / "smoke" / "live_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_dashboard_t"] = module
    spec.loader.exec_module(module)
    real_import = builtins.__import__

    def no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_playwright)
    assert "setup_playwright.sh" in module.check_browser()
    sys.modules.pop("live_dashboard_t", None)

