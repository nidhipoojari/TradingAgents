"""Failed runs keep what they recorded: the shared service, the worker process and the dashboard.

Modelled on the live F1 incident of 2026-11-23: two Market Analyst calls succeed, then the primary
model answers 503, E1 switches to the backup, and the backup answers 503 too. Offline throughout.
"""

from __future__ import annotations

import copy
import json
import multiprocessing as mp
import time

import pytest

from tests.test_analysis_service import _fake_langfuse
from tests.test_e1_e2_e3_audit import IN, OUT, Named
from tests.test_graph_end_to_end import TRADE_DATE, _Client, offline  # noqa: F401
from tradingagents import e1_routing, service
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.run_budget import RunBudget

UNAVAILABLE = "503 UNAVAILABLE: This model is currently experiencing high demand."


class Flaky(Named):
    """Succeeds ``ok_calls`` times (counted across copies), then answers 503 like the live incident."""

    ok_calls: int = 2
    counter: dict = {}
    message: str = UNAVAILABLE

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.counter["n"] = self.counter.get("n", 0) + 1
        if self.counter["n"] > self.ok_calls:
            raise RuntimeError(self.message)
        return super()._generate(messages, stop, run_manager, **kwargs)


def _setup(tmp_path, monkeypatch, *, main, backup=None, budget=None, analysts=("market",)):
    tmp_path.mkdir(parents=True, exist_ok=True)
    _fake_langfuse(monkeypatch, tmp_path)
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({n: {"input_per_million_usd": 1.0, "output_per_million_usd": 2.0}
                                   for n in ("main-m", "backup-m")}))
    monkeypatch.setenv("E3_PRICING_JSON", str(pricing))
    models = {"main-m": main, "backup-m": backup or main}
    monkeypatch.setattr(e1_routing, "create_tier_client", lambda cfg, tier, **k: _Client(
        models[cfg[f"{tier}_think_llm"]].model_copy(update={"callbacks": k.get("callbacks")})))
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
                  memory_log_path=str(tmp_path / "memory.md"), quick_think_llm="main-m", deep_think_llm="main-m",
                  quick_fallback_llm="backup-m" if backup else None, deep_fallback_llm="backup-m" if backup else None,
                  e2_cache_enabled=True)
    return service.AnalysisRequest(ticker="NVDA", trade_date=TRADE_DATE, config=config, analysts=analysts,
                                   source="dashboard", budget=budget)


def _usage(tmp_path):
    return json.loads((tmp_path / "results" / "NVDA" / TRADE_DATE / "token_usage.json").read_text())


def _e3(tmp_path):
    [summary] = [json.loads(p.read_text()) for p in (tmp_path / "e3").glob("*_e3.json")]
    return summary


class Unavailable(Named):
    """A backup model that is overloaded too."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError(UNAVAILABLE)


DASHBOARD_LIMITS = RunBudget(15, 0.04)


def _incident(tmp_path, monkeypatch, budget=DASHBOARD_LIMITS):
    request = _setup(tmp_path, monkeypatch, main=Flaky(model_name="main-m", counter={}),
                     backup=Unavailable(model_name="backup-m"), budget=budget)
    with pytest.raises(RuntimeError, match="503 UNAVAILABLE") as caught:
        service.run_analysis(request)
    return caught.value


# ------------------------------------------------------------------ the shared service
@pytest.mark.unit
def test_a_503_after_successful_calls_keeps_everything_the_run_recorded(tmp_path, monkeypatch, offline):  # noqa: F811
    exc = _incident(tmp_path, monkeypatch)
    usage, summary = _usage(tmp_path), _e3(tmp_path)
    assert exc.analysis_failure == usage and exc.usage_path.name == "token_usage.json"
    assert usage["status"] == "failed" and "503 UNAVAILABLE" in usage["error"]
    assert usage["run_id"] == summary["run_id"] and usage["source"] == "dashboard"
    # Two Market Analyst calls succeeded; the Bull Researcher's primary and its backup failed.
    assert (usage["llm_calls"], usage["llm_calls_succeeded"], usage["llm_calls_failed"]) == (4, 2, 2)
    assert (usage["tokens_in"], usage["tokens_out"]) == (2 * IN, 2 * OUT)       # only what was reported
    assert usage["e3_cost"]["basis"] == "successful_calls"
    assert usage["e3_cost"]["estimated_cost_usd"] == round(2 * (IN * 1.0 + OUT * 2.0) / 1e6, 6)
    assert "2 failed call(s)" in usage["e3_cost"]["note"]
    assert set(usage["per_agent"]) == {"Market Analyst", "Bull Researcher"} and usage["timing"]
    assert usage["langfuse_flush"] == summary["langfuse_flush"]
    assert summary["status"] == "failed" and "503" in summary["run_error"]


@pytest.mark.unit
def test_the_primary_and_backup_failures_are_both_recorded(tmp_path, monkeypatch, offline):  # noqa: F811
    _incident(tmp_path, monkeypatch)
    events = _usage(tmp_path)["e1_fallback_events"]
    assert len(events) == 2
    assert "main-m failed" in events[0] and "switching to backup-m" in events[0]
    assert "BACKUP backup-m also failed" in events[1]
    assert _e3(tmp_path)["observed_llm_errors"] == 2


@pytest.mark.unit
def test_e2_counters_survive_a_failure(tmp_path, monkeypatch, offline):  # noqa: F811
    _incident(tmp_path, monkeypatch)
    e2 = _usage(tmp_path)["e2_cache"]
    assert e2["misses"] > 0 and e2["hits"] == 0                     # a fresh cache: all misses
    assert 0 < e2["saved_entries"] <= e2["misses"]                   # past-dated answers were saved


@pytest.mark.unit
def test_the_budget_accounting_survives_a_failure(tmp_path, monkeypatch, offline):  # noqa: F811
    _incident(tmp_path, monkeypatch)
    budget, summary = _usage(tmp_path)["run_budget"], _e3(tmp_path)
    assert budget["llm_calls_started"] == 4 and budget["failed_calls"] == 2
    assert budget["exhausted"] is False and budget["llm_calls_in_flight"] == 0
    assert budget["max_llm_calls"] == 15 and budget["max_cost_usd"] == 0.04
    assert summary["run_budget"] == budget


@pytest.mark.unit
def test_credentials_in_a_provider_error_are_not_saved(tmp_path, monkeypatch, offline):  # noqa: F811
    secret = "AIza" + "S" * 35
    monkeypatch.setenv("GOOGLE_API_KEY", secret)
    request = _setup(tmp_path, monkeypatch, main=Flaky(model_name="main-m", counter={}, ok_calls=0,
                                                       message=f"403 bad key {secret} for request"))
    with pytest.raises(RuntimeError):
        service.run_analysis(request)
    saved = (tmp_path / "results" / "NVDA" / TRADE_DATE / "token_usage.json").read_text()
    assert secret not in saved and "[redacted]" in saved
    assert secret not in json.dumps(_e3(tmp_path))


@pytest.mark.unit
def test_a_failure_before_any_model_call_reports_zero_and_invents_nothing(tmp_path, monkeypatch, offline):  # noqa: F811
    request = _setup(tmp_path, monkeypatch, main=Flaky(model_name="main-m", counter={}, ok_calls=0))
    with pytest.raises(RuntimeError):
        service.run_analysis(request)
    usage = _usage(tmp_path)
    assert usage["llm_calls_succeeded"] == 0 and usage["tokens_in"] == usage["tokens_out"] == 0
    assert usage["e3_cost"]["estimated_cost_usd"] is None and usage["e3_cost"]["basis"] == "incomplete"


@pytest.mark.unit
def test_successful_runs_are_unchanged(tmp_path, monkeypatch, offline):  # noqa: F811
    request = _setup(tmp_path, monkeypatch, main=Named(model_name="main-m"))
    result = service.run_analysis(request.__class__(**{**request.__dict__, "budget": None}))
    assert set(result.usage) == {"llm_calls", "tool_calls", "tokens_in", "tokens_out", "tokens_total",
                                 "llm_provider", "quick_think_llm", "deep_think_llm", "elapsed_seconds",
                                 "e2_cache", "run_id", "source"}
    assert "status" not in _e3(tmp_path) or _e3(tmp_path)["status"] == "success"


@pytest.mark.unit
def test_cancel_and_budget_stops_keep_their_behaviour(tmp_path, monkeypatch, offline):  # noqa: F811
    request = _setup(tmp_path, monkeypatch, main=Named(model_name="main-m"), budget=RunBudget(2, None))
    with pytest.raises(service.AnalysisBudgetExceeded):
        service.run_analysis(request)
    assert not (tmp_path / "results" / "NVDA" / TRADE_DATE / "token_usage.json").exists()
    assert _e3(tmp_path)["status"] == "budget_exceeded"


# ------------------------------------------------------------------ the worker process
@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    for name, value in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "offline", "LANGFUSE_SECRET_KEY": "offline",
                        "LANGFUSE_BASE_URL": "http://localhost.invalid", "E3_RESULTS_DIR": str(tmp_path / "e3"),
                        "WORKER_FAKE_MARKER": str(tmp_path / "marker")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("E3_PRICING_JSON", raising=False)
    return tmp_path


def _no_stray_workers():
    from tradingagents import worker

    spares = {w.process.pid for p in worker._POOLS.values() if (w := p._ready)}
    return not [c for c in mp.active_children() if c.name == "tradingagents-analysis" and c.is_alive()
                and c.pid not in spares]


@pytest.mark.unit
def test_a_provider_failure_crosses_the_worker_with_its_record(worker_env):
    from tests.test_worker import _request
    from tradingagents import worker

    with pytest.raises(RuntimeError, match="503 UNAVAILABLE") as caught:
        worker.run_in_worker(_request(worker_env, analysts=("market",)), setup="tests.worker_fakes:fail_after_two_calls")
    failure = caught.value.analysis_failure
    assert failure["status"] == "failed" and failure["llm_calls_succeeded"] == 2 and failure["llm_calls_failed"] >= 1
    assert caught.value.usage_path.is_file() and json.loads(caught.value.usage_path.read_text()) == failure
    assert caught.value.e3_summary_path.is_file()
    assert not service.is_busy()
    for _ in range(100):                                     # the finished child exits in the background
        if _no_stray_workers():
            break
        time.sleep(0.05)
    assert _no_stray_workers()


@pytest.mark.unit
def test_a_worker_that_dies_still_leaves_a_record(worker_env):
    from tests.test_worker import _request
    from tradingagents import worker

    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"exited unexpectedly \(exit code 3\)") as caught:
        worker.run_in_worker(_request(worker_env, analysts=("market",)), setup="tests.worker_fakes:die_after_two_calls")
    assert time.monotonic() - started < 25           # reported at once, not after the 30 s heartbeat limit
    failure = caught.value.analysis_failure
    assert failure["status"] == "failed" and failure["llm_calls_succeeded"] == 2
    assert failure["llm_calls_in_flight"] == 1
    # E2 and tool counts survive from the worker's last checkpoint, labelled as a lower bound.
    view = failure["worker_checkpoint"]
    assert view["basis"].startswith("last checkpoint") and view["taken_seconds_before_failure"] >= 0
    assert failure["tool_calls"] == view["tool_calls_started"] >= 1
    assert failure["e2_cache"]["misses"] >= 1 and failure["e2_cache"]["complete"] is False
    assert failure["e2_cache"]["saved_entries"] == failure["e2_cache"]["misses"]
    assert failure["tokens_in"] == 200 and failure["tokens_out"] == 40               # the two reported calls
    [summary] = [json.loads(p.read_text()) for p in (worker_env / "e3").glob("*_e3.json")]
    assert summary["status"] == "failed" and summary["terminated"] is True
    assert not service.is_busy()


# ------------------------------------------------------------------ the dashboard
@pytest.mark.unit
def test_the_dashboard_keeps_and_shows_a_failed_runs_record():
    from tradingagents.dashboard import app, jobs

    record = {"llm_calls": 4, "llm_calls_succeeded": 2, "llm_calls_failed": 2, "tokens_in": 7021,
              "tokens_out": 1464, "e1_fallback_events": ["a -> switching to b", "BACKUP b also failed"],
              "e2_cache": {"hits": 0, "misses": 5, "saved_entries": 5},
              "e3_cost": {"estimated_cost_usd": 0.005766, "basis": "successful_calls",
                          "partial_cost_usd": None, "note": "estimate covers every successful LLM call"},
              "run_budget": None}

    def fails(request, on_event=None, cancel=None):
        error = RuntimeError(f"GoogleAPIError: {UNAVAILABLE}")
        error.analysis_failure = record
        raise error

    job = jobs.start_job(service.AnalysisRequest(ticker="NVDA", trade_date=TRADE_DATE, source="dashboard"), run=fails)
    for _ in range(100):
        if not job.running:
            break
        time.sleep(0.02)
    assert job.status == "failed" and job.failure == record
    assert job.error.startswith("GoogleAPIError: 503 UNAVAILABLE")      # the original type, not "RuntimeError: ..."
    lines = app.failure_lines(job.run_id, job.failure)
    assert lines[0] == f"Run ID {job.run_id}."
    text = "\n".join(lines)
    assert "4 LLM call(s) (2 succeeded, 2 failed); 7,021 input and 1,464 output tokens" in text
    # One call moved to the backup; the second line is the backup's own failure, not a switch.
    assert "E1 backup-model switches:** 1 (backup failed 1x)" in text
    assert "5 misses, 5 saved" in text and "$0.005766" in text
    assert app.failure_lines("R", None) == ["Run ID R.", "No usage was recorded before the failure."]


@pytest.mark.unit
def test_the_dashboard_page_shows_a_failed_run(tmp_path, monkeypatch, offline):  # noqa: F811
    pytest.importorskip("streamlit")
    import datetime
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from tradingagents.dashboard import trade
    from tradingagents.execution import alpaca_paper as ap

    for name in (*ap.KEY_ENVS, *ap.SECRET_ENVS):
        monkeypatch.delenv(name, raising=False)
    request = _setup(tmp_path, monkeypatch, main=Flaky(model_name="main-m", counter={}))
    monkeypatch.setattr(service, "DEFAULT_CONFIG", dict(request.config))
    monkeypatch.setenv("E3_RESULTS_DIR", str(tmp_path / "e3"))
    page = AppTest.from_file(str(Path(trade.__file__).with_name("app.py")), default_timeout=30).run()
    next(t for t in page.text_input if t.label == "Ticker").input("NVDA")
    page.date_input[0].set_value(datetime.date.fromisoformat(TRADE_DATE))
    page.multiselect[0].set_value(["market"])
    next(b for b in page.button if b.label == "Run analysis").click()
    page.run()
    deadline = time.time() + 60
    while time.time() < deadline and not page.error:
        time.sleep(0.2)
        page.run()
    assert not page.exception
    assert any("The analysis failed (provider_unavailable): RuntimeError: 503 UNAVAILABLE" in e.value
               for e in page.error)
    shown = " ".join(m.value for m in page.markdown)
    assert "Run ID " in shown and "Before it failed:** 3 LLM call(s) (2 succeeded, 1 failed)" in shown
