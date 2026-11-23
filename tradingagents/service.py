"""Phase 3: one analysis service for the CLI, the dashboard (F1) and the MCP server (F2).

Every interface runs an analysis the same way:

    result = run_analysis(AnalysisRequest(ticker="NVDA", trade_date="2026-10-23",
                                          source="dashboard"), on_event=show)

The service does what the CLI's run loop used to do, without any display:
build the graph (E1 routing and backup models are built inside it), attach the
per-run counters and E3 tracing, stream the run with checkpoint resume, record
the decision in the memory log, and write ``token_usage.json``. Interfaces only
turn the events into a view.

Shared and separate state
- E2 market-data cache and E3 tracing are the same pipelines for every source.
- The memory log is the configured one, except for MCP runs, which default to a
  log of their own (see ``config_for_source``) so analyses requested by other
  AI assistants do not feed lessons into the CLI's and dashboard's decisions.
- Each run gets a unique ``run_id`` and a ``source`` label. Both go into
  token_usage.json, the E3 summary and the Langfuse trace metadata. The run
  writes no other files than before.

One run at a time per process: the dataflows config, the E2 hit counters and the
E3 exit hook are process-wide, so concurrent runs would mix their numbers. A
second request while one is running gets ``AnalysisBusyError`` instead of
waiting minutes in a queue.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler

from tradingagents import e2_cache
from tradingagents.agents.rating import is_review, run_rating
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.run_stats import StatsCallbackHandler

logger = logging.getLogger(__name__)

SOURCES = frozenset({"cli", "dashboard", "mcp", "api"})
ANALYST_ORDER = ("market", "social", "news", "fundamentals")
ANALYST_REPORTS = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}
# The decision steps after the analysts, in the order they file.
DECISION_STEPS = (
    ("investment_plan", "Research debate"),
    ("trader_investment_plan", "Trader"),
    ("final_trade_decision", "Risk debate and Portfolio Manager"),
)
MCP_MEMORY_LOG_ENV = "TRADINGAGENTS_MCP_MEMORY_LOG_PATH"
MCP_MEMORY_LOG_NAME = "mcp_trading_memory.md"
FALLBACK_LOG_NAME = "e1_fallback_log.txt"

_RUN_LOCK = threading.Lock()


class AnalysisBusyError(RuntimeError):
    """Another analysis is running in this process."""


class AnalysisCancelled(RuntimeError):
    """The caller asked the run to stop (a timeout or a cancel button).

    ``stage`` is where the cancel was noticed. ``in_flight_model_calls`` counts
    model calls that had already started then: no provider SDK here can abort a
    request mid-flight, so the run waits for them to return (they are billed;
    their results are discarded) before it releases the process, and only then is
    this raised. ``released_after_seconds`` is how long that wait took. No new
    agent step, model call or tool call starts after the cancel is noticed.
    """

    def __init__(self, run_id: str, stage: str, in_flight_model_calls: int = 0,
                 prevented_calls: int = 0):
        self.run_id, self.stage = run_id, stage
        self.in_flight_model_calls, self.prevented_calls = in_flight_model_calls, prevented_calls
        self.detected_at = time.monotonic()
        self.released_after_seconds: float | None = None
        super().__init__(self._message())

    def _message(self) -> str:
        refused = f"; {self.prevented_calls} new model/tool call(s) were refused" if self.prevented_calls else ""
        waited = ""
        if self.in_flight_model_calls:
            waited = (f"; {self.in_flight_model_calls} model call(s) already running could not be interrupted, "
                      "so the run waited for them to return before releasing")
            if self.released_after_seconds is not None:
                waited += f" ({self.released_after_seconds:.1f}s)"
        return f"analysis {self.run_id} was cancelled {self.stage}{refused}{waited}"

    def mark_released(self) -> None:
        """Called once the graph has unwound and nothing of the run is still executing."""
        self.released_after_seconds = round(time.monotonic() - self.detected_at, 3)
        self.args = (self._message(),)


class _CancelSignal(BaseException):
    """Raised inside the graph to stop it. A BaseException on purpose: the agents'
    ``except Exception`` fallbacks, and E1's with_fallbacks, must not catch it and
    carry on (E1 would otherwise send the cancelled call to the backup model)."""


class CancellationGuard(BaseCallbackHandler):
    """Refuses to start a model or tool call once the run is cancelled.

    LangChain calls it before every model and tool call in the graph, including
    those inside parallel analysts and the memory step, so cancelling stops new
    work within one call of being asked, not only between top-level steps. It
    also counts the model calls in flight, so a cancelled run can say how many
    it could not stop.
    """

    raise_error = True      # a refusal must stop the call, not be logged and ignored
    run_inline = True

    def __init__(self, cancel: threading.Event):
        super().__init__()
        self.cancel = cancel
        self._lock = threading.Lock()
        self.in_flight = 0
        self.prevented = 0

    def _gate(self, what: str) -> None:
        if self.cancel.is_set():
            with self._lock:
                self.prevented += 1
            raise _CancelSignal(f"before a {what}")

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self._gate("model call")
        with self._lock:
            self.in_flight += 1

    def on_llm_start(self, serialized, prompts, **kwargs):
        self._gate("model call")
        with self._lock:
            self.in_flight += 1

    def _ended(self) -> None:
        with self._lock:
            self.in_flight = max(self.in_flight - 1, 0)

    def on_llm_end(self, response, **kwargs):
        self._ended()

    def on_llm_error(self, error, **kwargs):
        self._ended()

    def on_tool_start(self, serialized, input_str, **kwargs):
        self._gate("tool call")


def new_run_id() -> str:
    """Unique, sortable and short enough to sit inside an Alpaca client_order_id."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"


def config_for_source(source: str, base: Mapping[str, Any] | None = None) -> dict:
    """The run config for an interface: the configured defaults, with MCP runs on a log of their own.

    ``TRADINGAGENTS_MCP_MEMORY_LOG_PATH`` names the MCP log; unset, it sits beside
    the main log as ``mcp_trading_memory.md``.
    """
    _check_source(source)
    config = copy.deepcopy(dict(base if base is not None else DEFAULT_CONFIG))
    if source == "mcp":
        main_log = config.get("memory_log_path")
        config["memory_log_path"] = os.environ.get(MCP_MEMORY_LOG_ENV) or (
            str(Path(main_log).with_name(MCP_MEMORY_LOG_NAME)) if main_log else None)
    return config


def run_directory(config: Mapping[str, Any], ticker: str, trade_date: str) -> Path:
    """Where a run writes token_usage.json, with the ticker validated as a path component."""
    return Path(config["results_dir"]) / safe_ticker_component(ticker) / trade_date


def _check_source(source: str) -> None:
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}; one of {', '.join(sorted(SOURCES))}")


@dataclass(frozen=True)
class AnalysisRequest:
    ticker: str
    trade_date: str
    source: str = "api"
    asset_type: str = "stock"
    analysts: tuple[str, ...] = ANALYST_ORDER
    # A complete run config; None uses config_for_source(source).
    config: Mapping[str, Any] | None = None
    portfolio: Any = None          # tradingagents.portfolio.PortfolioContext
    run_id: str | None = None

    def __post_init__(self):
        _check_source(self.source)
        safe_ticker_component(self.ticker)
        if self.asset_type not in ("stock", "crypto"):
            raise ValueError(f"asset_type must be stock or crypto, got {self.asset_type!r}")
        chosen = set(self.analysts)
        unknown = chosen - set(ANALYST_ORDER)
        if unknown or not chosen:
            raise ValueError(f"analysts must be a non-empty subset of {', '.join(ANALYST_ORDER)}")
        # The graph's node order is fixed; the selection is a set.
        object.__setattr__(self, "analysts", tuple(a for a in ANALYST_ORDER if a in chosen))


@dataclass(frozen=True)
class AnalysisEvent:
    """One thing that happened during a run.

    ``kind`` is ``"checkpoint"`` (a checkpointed run started; ``resuming`` says
    whether it continues a saved one) or ``"step"`` (``messages`` from an agent
    and, after a top-level step or a filed analyst report, the ``state`` delta).
    ``progress`` is ``(done, total, label)`` over the run's reports.
    """

    run_id: str
    kind: str
    messages: tuple = ()
    state: dict | None = None
    resuming: bool = False
    progress: tuple[int, int, str] = (0, 0, "")


@dataclass
class AnalysisResult:
    run_id: str
    source: str
    ticker: str
    trade_date: str
    asset_type: str
    analysts: tuple[str, ...]
    rating: str
    final_state: dict
    usage: dict
    results_dir: Path
    usage_path: Path
    e3_summary_path: Path | None = None
    fallback_events: list[str] = field(default_factory=list)
    graph: Any = field(default=None, repr=False)   # for the CLI's report saving
    e3: Any = field(default=None, repr=False)      # the E3 session, when its save was deferred

    @property
    def is_review(self) -> bool:
        return is_review(self.rating)

    @property
    def decision_text(self) -> str:
        return self.final_state.get("final_trade_decision") or ""


def progress_of(state: Mapping[str, Any], analysts) -> tuple[int, int, str]:
    """How far a run is: reports filed, out of how many, and what is next."""
    steps = [(ANALYST_REPORTS[a], "Analysts") for a in ANALYST_ORDER if a in analysts]
    steps += list(DECISION_STEPS)
    done = sum(1 for key, _ in steps if state.get(key))
    upcoming = next((label for key, label in steps if not state.get(key)), "Done")
    return done, len(steps), upcoming


def run_analysis(
    request: AnalysisRequest,
    *,
    on_event: Callable[[AnalysisEvent], None] | None = None,
    cancel: threading.Event | None = None,
    stats_handler: StatsCallbackHandler | None = None,
    extra_callbacks=(),
    graph_factory=None,
    wait: bool = False,
    defer_e3_save: bool = False,
) -> AnalysisResult:
    """Run one analysis and return its decision, usage and file locations.

    ``on_event`` receives every step. ``cancel`` stops the run: it is checked
    before each stage (graph construction, the market-data identity lookup, the
    workflow), between workflow steps, and before every model and tool call
    inside the graph; a model call already in flight is not interrupted (see
    AnalysisCancelled). ``stats_handler`` lets a caller watch the counters live.
    ``graph_factory`` defaults to TradingAgentsGraph. ``defer_e3_save`` leaves
    the E3 summary for the caller to save (``result.e3.save()``) after its own
    post-analysis work, as the CLI does after its report prompts. Raises
    AnalysisBusyError when another run holds the process, unless ``wait``.
    """
    if not _RUN_LOCK.acquire(blocking=wait):
        raise AnalysisBusyError("another analysis is running in this process; try again when it finishes")
    try:
        return _run_locked(request, on_event, cancel, stats_handler, extra_callbacks, graph_factory,
                           defer_e3_save)
    finally:
        _RUN_LOCK.release()


def is_busy() -> bool:
    return _RUN_LOCK.locked()


def _run_locked(request, on_event, cancel, stats_handler, extra_callbacks, graph_factory, defer_e3_save):
    # Imported here: the graph pulls in every LLM SDK, which a caller that only
    # builds requests (the MCP server listing its tools) need not pay for.
    from tradingagents.e3_cli_hook import E3Session
    from tradingagents.graph.trading_graph import _validate_trade_date

    if graph_factory is None:
        from tradingagents.graph.trading_graph import TradingAgentsGraph as graph_factory

    ticker, asset_type, portfolio = request.ticker, request.asset_type, request.portfolio
    trade_date = _validate_trade_date(request.trade_date)
    run_id = request.run_id or new_run_id()
    config = (copy.deepcopy(dict(request.config)) if request.config is not None
              else config_for_source(request.source))
    guard = CancellationGuard(cancel) if cancel is not None else None

    def check(stage: str) -> None:
        if cancel is not None and cancel.is_set():
            raise AnalysisCancelled(run_id, stage, guard.in_flight if guard else 0,
                                    guard.prevented if guard else 0)

    check("before it started")
    stats = stats_handler or StatsCallbackHandler()
    e3 = E3Session.from_env(ticker=ticker, analysis_date=trade_date, run_id=run_id,
                            source=request.source)
    # The guard first: a refused call must not be counted as started by the others.
    callbacks = [*([guard] if guard else []), stats, *e3.callbacks, *extra_callbacks]
    emit = on_event or (lambda event: None)

    try:
        try:
            check("before the graph was built")
            e3.mark_analysis_started()
            graph = graph_factory(list(request.analysts), config=config, debug=False, callbacks=callbacks)
            started = time.time()
            e2_before = e2_cache.stats_snapshot()
            fallback_log = Path(config["results_dir"]) / FALLBACK_LOG_NAME
            fallback_mark = _size(fallback_log)

            check("before the instrument lookup")
            init_state = graph.create_run_state(ticker, trade_date, asset_type, portfolio)
            args = graph.propagator.get_graph_args(callbacks=callbacks)
            graph_config = args.setdefault("config", {})
            # E3: one Langfuse session per run, tagged with the interface it came from.
            graph_config.setdefault("metadata", {}).update({
                "tradingagents_run_id": run_id,
                "tradingagents_source": request.source,
                "langfuse_session_id": run_id,
                "langfuse_tags": [f"source:{request.source}"],
            })

            check("before the workflow started")
            # Recompile with a checkpointer when enabled (#1249); torn down in the finally.
            checkpoint_tid = graph.begin_checkpoint(ticker, trade_date, asset_type, portfolio)
            final_state: dict = {}
            try:
                if checkpoint_tid is not None:
                    graph_config.setdefault("configurable", {})["thread_id"] = checkpoint_tid
                    emit(AnalysisEvent(run_id, "checkpoint",
                                       resuming=bool(getattr(graph, "_resuming", False))))
                # On resume, None continues the saved run instead of re-appending the state.
                for messages, chunk in graph.stream_run(graph.checkpoint_input(init_state), **args):
                    check(f"after {progress_of(final_state, request.analysts)[0]} report(s)")
                    # Streamed chunks are per-node deltas; merged, they are the final state.
                    if chunk is not None:
                        final_state.update(chunk)
                    emit(AnalysisEvent(run_id, "step", tuple(messages), chunk,
                                       progress=progress_of(final_state, request.analysts)))

                check("before the decision was recorded")
                # Clean run: log the decision, then drop the checkpoint. A failure
                # above skips both, keeping the checkpoint for a resume.
                graph.record_decision(ticker, trade_date, final_state)
                graph.clear_checkpoint_on_success(ticker, trade_date, asset_type, portfolio)
            finally:
                graph.end_checkpoint()
        except _CancelSignal as signal:
            raise AnalysisCancelled(run_id, str(signal), guard.in_flight if guard else 0,
                                    guard.prevented if guard else 0) from None
        except AnalysisCancelled:
            raise
        except BaseException as exc:
            # A step that failed because the run was cancelled under it is a cancellation.
            if cancel is not None and cancel.is_set():
                raise AnalysisCancelled(run_id, "during a step", guard.in_flight if guard else 0,
                                        guard.prevented if guard else 0) from exc
            raise
    except AnalysisCancelled as cancelled:
        # The graph has unwound: LangGraph waited for every running task, so nothing
        # of this run executes past here and the lock can be released.
        cancelled.mark_released()
        e3.mark_analysis_finished()
        e3.mark_cancelled(str(cancelled))
        e3.save()
        raise
    except BaseException as exc:
        e3.mark_analysis_finished()
        e3.mark_failed(exc)
        e3.save()
        raise
    e3.mark_analysis_finished()

    rating = run_rating(final_state)
    usage = {
        **stats.get_stats(),
        "llm_provider": config.get("llm_provider"),
        "quick_think_llm": config.get("quick_think_llm"),
        "deep_think_llm": config.get("deep_think_llm"),
        "elapsed_seconds": round(time.time() - started, 1),
    }
    usage["tokens_total"] = usage["tokens_in"] + usage["tokens_out"]
    # E2: this run's own hits and misses, not the process's running total.
    if config.get("e2_cache_enabled", False) or e2_cache.is_enabled():
        usage["e2_cache"] = _e2_delta(e2_before, e2_cache.stats_snapshot())
    usage["run_id"] = run_id
    usage["source"] = request.source

    results_dir = run_directory(config, ticker, trade_date)
    results_dir.mkdir(parents=True, exist_ok=True)
    usage_path = results_dir / "token_usage.json"
    usage_path.write_text(json.dumps(usage, indent=2), encoding="utf-8")

    e3.mark_completed(decision=final_state.get("final_trade_decision"))
    if not defer_e3_save:
        e3.save()

    return AnalysisResult(
        run_id=run_id, source=request.source, ticker=ticker, trade_date=trade_date,
        asset_type=asset_type, analysts=request.analysts, rating=rating,
        final_state=final_state, usage=usage, results_dir=results_dir, usage_path=usage_path,
        e3_summary_path=e3.saved_path, fallback_events=_new_lines(fallback_log, fallback_mark),
        graph=graph, e3=e3,
    )


def _e2_delta(before: dict, after: dict) -> dict:
    counts = {key: after[key] - before.get(key, 0) for key in ("hits", "misses", "skipped")}
    looked_up = counts["hits"] + counts["misses"]
    counts["hit_rate_pct"] = round(100 * counts["hits"] / looked_up, 1) if looked_up else None
    return counts


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _new_lines(path: Path, offset: int) -> list[str]:
    """E1: the backup-model switches this run appended to the fallback log."""
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]
