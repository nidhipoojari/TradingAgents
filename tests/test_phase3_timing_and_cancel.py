"""Phase 3: cooperative cancellation, and E3's timing boundaries (Phase 2 comparable).

Real graph, scripted models, offline vendors. One analyst keeps the model calls
sequential, so "no call started after the cancel" is deterministic.
"""

from __future__ import annotations

import copy
import json
import threading
import time

import pytest
from langchain_core.runnables import Runnable

from tests.test_analysis_service import _fake_langfuse
from tests.test_graph_end_to_end import TRADE_DATE, ScriptedModel, _Client, offline  # noqa: F401
from tradingagents import e1_routing, service
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.e3_tracing import E3MetricsCallback

# Every field a Phase 2 E3 summary had; Phase 3 may add fields but not drop or rename these.
PHASE2_E3_FIELDS = set(E3MetricsCallback().summary(ticker="T", analysis_date="2026-01-01", status="success",
                                                    wall_time_seconds=1.0))


def _config(tmp_path, **extra):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
               memory_log_path=str(tmp_path / "memory" / "trading_memory.md"), **extra)
    return cfg


def _request(tmp_path, **config):
    return service.AnalysisRequest(ticker="NVDA", trade_date=TRADE_DATE, analysts=("market",),
                                   source="dashboard", config=_config(tmp_path, **config))


class CancellingModel(ScriptedModel):
    """Sets the cancel event during call ``cancel_at``; records every call that starts after it."""

    cancel: object = None
    cancel_at: int = 2
    after_cancel: list = []

    def bind_tools(self, tools, **kwargs) -> Runnable:   # annotated: with_fallbacks re-binds it
        return super().bind_tools(tools, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.cancel.is_set():
            self.after_cancel.append(len(self.calls))
        result = super()._generate(messages, stop, run_manager, **kwargs)
        if len(self.calls) == self.cancel_at:
            self.cancel.set()
        return result


def _use(monkeypatch, model, backup=None):
    def create(cfg, tier, **kwargs):
        chosen = backup if (backup is not None and cfg[f"{tier}_think_llm"] == "backup-model") else model
        return _Client(chosen.model_copy(update={"callbacks": kwargs.get("callbacks")}))
    monkeypatch.setattr(e1_routing, "create_tier_client", create)


# ------------------------------------------------------------------ cancellation
@pytest.mark.unit
def test_no_model_call_starts_after_the_cancel(tmp_path, monkeypatch, offline):  # noqa: F811
    cancel = threading.Event()
    model = CancellingModel(cancel=cancel, cancel_at=2, after_cancel=[])
    _use(monkeypatch, model)

    with pytest.raises(service.AnalysisCancelled) as caught:
        service.run_analysis(_request(tmp_path), cancel=cancel)

    assert model.after_cancel == []                 # the guard refused every later call
    assert len(model.calls) == 2                    # the call that cancelled was allowed to finish
    assert caught.value.stage
    assert not service.is_busy()
    # No decision recorded and no usage file: the run did not complete.
    assert not (tmp_path / "memory" / "trading_memory.md").exists()
    assert not (tmp_path / "results" / "NVDA" / TRADE_DATE / "token_usage.json").exists()


@pytest.mark.unit
def test_the_guard_refuses_new_calls_inside_the_graph(tmp_path, monkeypatch, offline):  # noqa: F811
    """Cancelled during the analyst's first model call: its tool calls are refused inside the
    analyst's own subgraph, not only at the next top-level step."""
    cancel = threading.Event()
    model = CancellingModel(cancel=cancel, cancel_at=1, after_cancel=[])
    _use(monkeypatch, model)
    with pytest.raises(service.AnalysisCancelled, match="were refused") as caught:
        service.run_analysis(_request(tmp_path), cancel=cancel)
    assert caught.value.prevented_calls > 0 and model.after_cancel == []


@pytest.mark.unit
def test_a_cancelled_call_is_not_sent_to_the_e1_backup(tmp_path, monkeypatch, offline):  # noqa: F811
    cancel = threading.Event()
    main = CancellingModel(cancel=cancel, cancel_at=1, after_cancel=[])
    backup = CancellingModel(cancel=cancel, cancel_at=10**6, after_cancel=[])
    _use(monkeypatch, main, backup)
    request = _request(tmp_path, quick_think_llm="main-model", deep_think_llm="main-model",
                       quick_fallback_llm="backup-model", deep_fallback_llm="backup-model")

    with pytest.raises(service.AnalysisCancelled):
        service.run_analysis(request, cancel=cancel)

    assert backup.calls == []                        # E1 did not treat the cancel as a model failure
    assert not (tmp_path / "results" / service.FALLBACK_LOG_NAME).exists()


@pytest.mark.unit
def test_a_cancel_before_the_start_builds_nothing(tmp_path):
    cancel = threading.Event()
    cancel.set()
    built = []
    with pytest.raises(service.AnalysisCancelled, match="before it started"):
        service.run_analysis(_request(tmp_path), cancel=cancel,
                             graph_factory=lambda *a, **k: built.append(1))
    assert built == []


@pytest.mark.unit
def test_a_cancel_during_setup_stops_before_the_workflow(tmp_path, monkeypatch, offline):  # noqa: F811
    from tradingagents.graph import trading_graph

    cancel = threading.Event()
    model = ScriptedModel()
    _use(monkeypatch, model)
    real = trading_graph.TradingAgentsGraph.create_run_state

    def slow_lookup(self, *a, **k):
        state = real(self, *a, **k)
        cancel.set()                                 # cancelled while the identity lookup ran
        return state

    monkeypatch.setattr(trading_graph.TradingAgentsGraph, "create_run_state", slow_lookup)
    with pytest.raises(service.AnalysisCancelled, match="before the workflow started"):
        service.run_analysis(_request(tmp_path), cancel=cancel)
    assert model.calls == []


@pytest.mark.unit
def test_the_guard_counts_calls_it_could_not_stop():
    cancel = threading.Event()
    guard = service.CancellationGuard(cancel)
    guard.on_chat_model_start({}, [[]])
    guard.on_chat_model_start({}, [[]])
    guard.on_llm_end(None)
    cancel.set()
    with pytest.raises(service._CancelSignal):
        guard.on_chat_model_start({}, [[]])
    with pytest.raises(service._CancelSignal):
        guard.on_tool_start({}, "")
    assert (guard.in_flight, guard.prevented) == (1, 2)
    message = str(service.AnalysisCancelled("r1", "after 1 report(s)", guard.in_flight, guard.prevented))
    assert "1 model call(s) already running could not be interrupted" in message


@pytest.mark.unit
def test_the_cancel_signal_escapes_generic_exception_handlers():
    """Agents' ``except Exception`` fallbacks and E1's with_fallbacks must not swallow it."""
    assert not issubclass(service._CancelSignal, Exception)


@pytest.mark.unit
def test_a_cancelled_run_is_recorded_as_cancelled_in_e3(tmp_path, monkeypatch, offline):  # noqa: F811
    _fake_langfuse(monkeypatch, tmp_path)
    cancel = threading.Event()
    _use(monkeypatch, CancellingModel(cancel=cancel, cancel_at=2, after_cancel=[]))
    with pytest.raises(service.AnalysisCancelled):
        service.run_analysis(_request(tmp_path), cancel=cancel)
    [path] = (tmp_path / "e3").glob("*_e3.json")
    summary = json.loads(path.read_text())
    assert summary["status"] == "cancelled" and "cancelled" in summary["run_error"]
    assert summary["observed_llm_calls"] == 2       # token accounting covers the calls that ran


# ------------------------------------------------------------------ E3 timing
@pytest.mark.unit
def test_e3_keeps_every_phase2_field_and_adds_the_phases(tmp_path, monkeypatch, offline):  # noqa: F811
    _fake_langfuse(monkeypatch, tmp_path)
    _use(monkeypatch, ScriptedModel())
    result = service.run_analysis(_request(tmp_path))
    summary = json.loads(result.e3_summary_path.read_text())

    assert set(summary) >= PHASE2_E3_FIELDS
    assert summary["schema"] == "tradingagents-e3-summary-v1"
    pre, analysis, post = (summary[k] for k in ("pre_analysis_seconds", "analysis_seconds",
                                                 "post_analysis_seconds"))
    assert analysis > 0 and pre >= 0 and 0 <= post < 1     # no prompts outside the CLI
    assert abs(pre + analysis + post - summary["wall_time_seconds"]) < 0.01
    assert set(summary["timing_boundaries"]) == {"wall_time_seconds", "pre_analysis_seconds",
                                                 "analysis_seconds", "post_analysis_seconds"}


@pytest.mark.unit
def test_cli_e3_wall_time_spans_the_report_prompts_as_in_phase2(tmp_path, monkeypatch, offline):  # noqa: F811
    import cli.display as display
    import cli.main as cli_main
    from cli import run as cli_run
    from cli.models import AnalystType

    hooks = _fake_langfuse(monkeypatch, tmp_path)
    buffer = display.MessageBuffer()
    monkeypatch.setattr(display, "message_buffer", buffer)
    monkeypatch.setattr(cli_run, "message_buffer", buffer)
    monkeypatch.setattr(cli_run, "create_layout", lambda: None)
    monkeypatch.setattr(cli_run, "update_display", lambda *a, **k: None)
    monkeypatch.setattr(cli_run, "Live", type("L", (), {"__init__": lambda s, *a, **k: None,
                                                  "__enter__": lambda s: s, "__exit__": lambda s, *a: False}))
    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags=None: {
        "ticker": "NVDA", "analysis_date": TRADE_DATE, "analysts": [AnalystType.MARKET], "asset_type": "stock"})
    monkeypatch.setattr(cli_run, "_build_run_config", lambda s, c: _config(tmp_path))

    def slow_prompt(*a, **k):   # a person reading the report before answering
        time.sleep(0.3)
        return "N"

    monkeypatch.setattr(cli_main.typer, "prompt", slow_prompt)
    _use(monkeypatch, ScriptedModel())

    cli_run.run_analysis()

    [path] = (tmp_path / "e3").glob("*_e3.json")
    summary = json.loads(path.read_text())
    assert summary["source"] == "cli" and summary["status"] == "success"
    assert summary["post_analysis_seconds"] >= 0.5          # the two prompts, as Phase 2 counted them
    assert summary["wall_time_seconds"] >= summary["analysis_seconds"] + 0.5
    # token_usage.json's elapsed_seconds keeps its Phase 2 span: the analysis only.
    usage = json.loads((tmp_path / "results" / "NVDA" / TRADE_DATE / "token_usage.json").read_text())
    assert usage["elapsed_seconds"] < summary["wall_time_seconds"] - 0.5
    # Saved once, explicitly; the exit hook was dropped.
    assert len(hooks["registered"]) == len(hooks["unregistered"]) == 1


@pytest.mark.unit
def test_phase_split_without_marks_leaves_phases_unset():
    from tradingagents.e3_cli_hook import E3Session

    session = E3Session("NVDA", "2026-01-09", callbacks=[E3MetricsCallback()])
    phases = session._phases(time.perf_counter())
    assert phases["pre_analysis_seconds"] is None and phases["analysis_seconds"] is None
    assert phases["post_analysis_seconds"] is None and phases["timing_boundaries"]


# ------------------------------------------------------------------ in-flight calls and the process lock
class InFlightModel(ScriptedModel):
    """The market analyst's first call blocks until released (an LLM call still in flight);
    the news analyst's call then cancels the run (unless ``cancels`` is False)."""

    cancel: object = None
    release: object = None
    entered: object = None
    cancels: bool = True
    log: list = []

    def bind_tools(self, tools, **kwargs) -> Runnable:
        return super().bind_tools(tools, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        names = {t.name for t in self.tools}
        if self.cancel is not None and self.cancel.is_set() and self.log and self.log[-1] != "in-flight returned":
            self.log.append("started after cancel")
        if "get_stock_data" in names and not self.entered.is_set():
            self.entered.set()
            self.release.wait(20)
            self.log.append("in-flight returned")
        elif self.cancels and "get_news" in names and self.entered.is_set() and not self.cancel.is_set():
            self.cancel.set()
            self.log.append("cancel set")
        return super()._generate(messages, stop, run_manager, **kwargs)


@pytest.mark.unit
def test_no_second_analysis_starts_until_the_in_flight_call_has_returned(tmp_path, monkeypatch, offline):  # noqa: F811
    cancel, release, entered = threading.Event(), threading.Event(), threading.Event()
    model = InFlightModel(cancel=cancel, release=release, entered=entered, log=[])
    _use(monkeypatch, model)
    request = service.AnalysisRequest(ticker="NVDA", trade_date=TRADE_DATE, analysts=("market", "news"),
                                      config=_config(tmp_path))
    outcome = {}

    def first():
        try:
            service.run_analysis(request, cancel=cancel)
        except service.AnalysisCancelled as exc:
            outcome["cancelled"] = exc

    worker = threading.Thread(target=first)
    worker.start()
    assert cancel.wait(10)                       # cancelled while the market call is still running
    time.sleep(0.3)
    # The first run still holds the process: nothing else may start yet.
    assert worker.is_alive() and service.is_busy()
    with pytest.raises(service.AnalysisBusyError):
        service.run_analysis(request)
    assert "cancelled" not in outcome            # not reported as done while its call runs

    release.set()
    worker.join(10)
    cancelled = outcome["cancelled"]
    assert model.log == ["cancel set", "in-flight returned"]     # nothing started after the cancel
    # The blocked call, and the cancelling call itself when the cancel is noticed before it returns.
    assert cancelled.in_flight_model_calls in (1, 2)
    assert cancelled.released_after_seconds >= 0.25   # noticed a few ms after cancel.set()
    assert "waited for them to return before releasing" in str(cancelled)
    assert not service.is_busy()
    # Released for real: the next analysis runs normally.
    _use(monkeypatch, ScriptedModel())
    assert service.run_analysis(request).rating == "Overweight"


@pytest.mark.unit
def test_an_mcp_timeout_answers_at_once_but_holds_the_server_until_the_call_returns(tmp_path, monkeypatch, offline):  # noqa: F811
    fastmcp = pytest.importorskip("fastmcp")
    import asyncio

    from fastmcp.exceptions import ToolError

    from tradingagents import mcp_server

    monkeypatch.setattr(service, "DEFAULT_CONFIG", _config(tmp_path))
    release, entered = threading.Event(), threading.Event()
    model = InFlightModel(release=release, entered=entered, cancels=False, log=[])
    _use(monkeypatch, model)
    args = {"ticker": "NVDA", "trade_date": TRADE_DATE, "analysts": ["market", "news"]}

    async def scenario():
        async with fastmcp.Client(mcp_server.create_server(timeout=0.5)) as client:
            started = time.monotonic()
            with pytest.raises(ToolError, match="timed out"):
                await client.call_tool("analyze_stock", args)
            answered_in = time.monotonic() - started
            assert service.is_busy()             # the abandoned run still holds the server
            with pytest.raises(ToolError, match="another analysis is running"):
                await client.call_tool("analyze_stock", args)
            release.set()
            for _ in range(100):
                if not service.is_busy():
                    break
                await asyncio.sleep(0.05)
            return answered_in

    answered_in = asyncio.run(scenario())
    assert answered_in < 3                       # the client was not kept waiting for the drain
    assert not service.is_busy()
    assert "started after cancel" not in model.log
