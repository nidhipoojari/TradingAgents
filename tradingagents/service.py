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

import contextlib
import copy
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler

from tradingagents import e2_cache
from tradingagents.agents.rating import is_review, run_rating
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.run_budget import BudgetGuard, RunBudget
from tradingagents.run_deadline import DeadlineGuard, RunDeadline
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


class AnalysisBudgetExceeded(AnalysisCancelled):
    """The run's per-run LLM limit (calls or estimated cost) stopped it: not a cancel by a person
    and not a model failure. ``budget`` is the limit's final accounting (run_budget.BudgetGuard)."""

    def __init__(self, run_id: str, stage: str, in_flight_model_calls: int = 0,
                 prevented_calls: int = 0, budget: dict | None = None):
        self.budget = budget or {}
        super().__init__(run_id, stage, in_flight_model_calls, prevented_calls)

    def _message(self) -> str:
        cancelled = super()._message()
        reason = self.budget.get("reason") or "per-run limit reached"
        return cancelled.replace(f"analysis {self.run_id} was cancelled",
                                 f"analysis {self.run_id} was stopped by its per-run limit ({reason})", 1)


class AnalysisDeadlineExceeded(AnalysisCancelled):
    """The run's deadline stopped it (the whole analysis, or one model step): not a cancel by a person,
    not the budget and not a provider error. ``deadline`` is the deadline's accounting."""

    def __init__(self, run_id: str, stage: str, in_flight_model_calls: int = 0,
                 prevented_calls: int = 0, deadline: dict | None = None):
        self.deadline = deadline or {}
        super().__init__(run_id, stage, in_flight_model_calls, prevented_calls)

    def _message(self) -> str:
        cancelled = super()._message()
        reason = self.deadline.get("reason") or "deadline reached"
        return cancelled.replace(f"analysis {self.run_id} was cancelled",
                                 f"analysis {self.run_id} was stopped by its deadline ({reason})", 1)


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


class _FailedCallCounter(BaseCallbackHandler):
    """Counts model calls that ended in an error (for a failed run's record; adds nothing to a success)."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self.failed = 0

    def on_llm_error(self, error, **kwargs):
        with self._lock:
            self.failed += 1


_SECRET_PATTERNS = (r"AIza[0-9A-Za-z_\-]{30,}", r"\b(?:sk|pk)-lf-[0-9A-Za-z\-]{8,}", r"Bearer\s+[A-Za-z0-9._\-]{12,}",
                    r"(?i)\b(?:api[_-]?key|key|token|secret)=[^\s&'\"]{8,}")


def error_category(error: BaseException) -> str:
    """A short, stable label for why a run stopped, for people and for MCP clients."""
    from tradingagents.e3_tracing import error_code

    if isinstance(error, AnalysisBudgetExceeded):
        return "budget_exceeded"
    if isinstance(error, AnalysisDeadlineExceeded):
        return "timeout"
    if isinstance(error, AnalysisCancelled):
        return "cancelled"
    if isinstance(error, AnalysisBusyError):
        return "busy"
    name = type(error).__name__
    if (name in ("WorkerUnresponsive", "WorkerStartupError") or "worker exited unexpectedly" in str(error)
            or "worker sent no heartbeat" in str(error)):
        return "worker_failure"
    import re

    from tradingagents.e1_routing import FallbackConfigError
    from tradingagents.llm_clients.provider_errors import classify

    if isinstance(error, FallbackConfigError):
        return "configuration"
    code = error_code(error)
    text = str(error)[:400]
    overload = classify(error)
    if overload is not None and overload.kind == "quota_exhausted":
        return "quota_exhausted"            # a daily/monthly/billing quota: retrying the model will not help
    # Whole numbers only: a run ID's hex digits must never read as a status code.
    if code == 429 or re.search(r"\b429\b|RESOURCE_EXHAUSTED", text) or "RateLimit" in name:
        return "rate_limited"
    if code in (500, 502, 503) or re.search(r"\b50[023]\b|\bUNAVAILABLE\b", text):
        return "provider_unavailable"
    if code in (408, 504) or isinstance(error, TimeoutError) or "Timeout" in name or "DEADLINE_EXCEEDED" in text:
        return "timeout"
    if code == 404 or re.search(r"\bNOT_FOUND\b|model.{0,40}not (be )?found|is not found for API version", text,
                                re.IGNORECASE):
        return "model_not_found"
    if code in (401, 403) or re.search(r"\b(UNAUTHENTICATED|PERMISSION_DENIED)\b|API key not valid", text):
        return "auth"
    if isinstance(error, ValueError):
        return "invalid_request"
    if code is not None and 400 <= code < 500:
        return "provider_rejected"
    return "error"


def failure_hint(error: BaseException) -> str | None:
    """What a person can do about this failure (quota, rate limit, 5xx, unknown model, credentials)."""
    from tradingagents.llm_clients.provider_errors import hint_for

    return hint_for(error_category(error), error)


def describe_error(exc: BaseException) -> str:
    """The error as a person should read it. The worker re-raises a child's error as
    RuntimeError("<original type>: <message>"); keep that original type instead of prefixing another."""
    import re

    text = str(exc)
    if type(exc) is RuntimeError and re.match(r"^[A-Za-z_][\w.]*(Error|Exception|Exit): ", text):
        return text
    return f"{type(exc).__name__}: {text}"


def sanitize_error(error: BaseException | str, limit: int = 500) -> str:
    """One line, bounded, with any credential-looking value removed: safe to save and to show."""
    import re

    text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    for name, value in os.environ.items():
        if value and len(value) >= 8 and any(k in name for k in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
            text = text.replace(value, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        text = re.sub(pattern, "[redacted]", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def new_run_id() -> str:
    """Unique, sortable and short enough to sit inside an Alpaca client_order_id."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"


def e3_cost(summary: Mapping[str, Any] | None) -> dict[str, Any]:
    """A run's E3 cost as the dashboard and MCP report it; a partial sum is never the estimate.

    ``basis`` says what ``estimated_cost_usd`` covers:

    - ``all_calls``: every LLM call was priced and reported its tokens (E3 ``estimated_cost_usd``).
    - ``successful_calls``: every successful call was priced and reported its tokens; only failed
      calls that reported no tokens (an E1 switch to the backup) are left out
      (E3 ``estimated_cost_usd_successful_calls``).
    - ``incomplete``: some successful call is unpriced or reported no tokens. ``estimated_cost_usd``
      is None; ``partial_cost_usd`` is the sum over calls that could be priced, a lower bound only.
    - ``unavailable``: no E3 summary (tracing off, or the summary could not be read).
    """
    if not summary:
        return {"estimated_cost_usd": None, "basis": "unavailable", "partial_cost_usd": None,
                "note": "no E3 summary for this run (tracing off or the summary could not be read)"}
    strict = summary.get("estimated_cost_usd")
    if strict is not None:
        return {"estimated_cost_usd": strict, "basis": "all_calls", "partial_cost_usd": None,
                "note": "estimate covers every LLM call"}
    successful = summary.get("estimated_cost_usd_successful_calls")
    if successful is not None:
        failed = summary.get("failed_calls_without_usage") or 0
        return {"estimated_cost_usd": successful, "basis": "successful_calls", "partial_cost_usd": None,
                "note": f"estimate covers every successful LLM call; {failed} failed call(s) reported "
                        "no tokens and are not included (a failed call can still be billed)"}
    partial = summary.get("known_usage_cost_usd")
    unpriced = summary.get("unpriced_llm_calls") or 0
    return {"estimated_cost_usd": None, "basis": "incomplete", "partial_cost_usd": partial,
            "note": "no complete estimate: "
                    + (f"{unpriced} call(s) used a model with no price in E3_PRICING_JSON or reported no tokens"
                       if unpriced else "some calls reported no token usage")
                    + (f"; the priced calls alone come to ${partial:.6f} (a lower bound, not the cost)"
                       if partial is not None else "")}


# Interfaces a person or an assistant waits on get bounded model calls unless configured otherwise
# (an explicit TRADINGAGENTS_LLM_BACKUP_MAX_RETRIES / TRADINGAGENTS_LLM_TIMEOUT always wins). The CLI keeps
# Phase 2's settings. The backup gets ONE retry after its first request, translated per provider by
# llm_clients/retry_policy (Gemini counts attempts, OpenAI/Anthropic count retries); the primary gets none.
# The 30 s request timeout sits above the slowest successful call in the saved evidence (23.1 s; p99 16.4 s).
BOUNDED_SOURCES = frozenset({"dashboard", "mcp"})
BOUNDED_BACKUP_RETRIES = 1
BOUNDED_TIMEOUT_SECONDS = 30.0
# E1 circuit breaker (e1_routing.CircuitBreaker): after an overload error from the main model, its calls
# go straight to the backup for this long. TRADINGAGENTS_E1_BREAKER_SECONDS=<n> or off; CLI: never.
BREAKER_ENV = "TRADINGAGENTS_E1_BREAKER_SECONDS"
BOUNDED_BREAKER_SECONDS = 60.0     # > one interactive run (25-40 s): one overload error per run, not two


class _SdkRetryCounter:
    """Counts provider-SDK retries, which happen inside one model call where no callback sees them.

    Each known SDK logs every retry at INFO before it sleeps (llm_clients/retry_policy.KNOWN): google-genai
    "Retrying ...", openai and anthropic "Retrying request ...". ``report()`` is the count, or None when a
    provider used by the run does not expose its retries (then ``observability`` says which).
    """

    def __init__(self):
        self.count = 0
        self.observability = "observed"
        self._lock = threading.Lock()

    def add(self):
        with self._lock:
            self.count += 1

    def report(self) -> int | None:
        return self.count if self.observability == "observed" else None

    def describe_for(self, config: Mapping[str, Any]) -> None:
        from tradingagents.llm_clients.retry_policy import UNKNOWN, policy_for, tier_provider

        unseen = sorted({tier_provider(dict(config), tier) for tier in ("quick", "deep")
                         if policy_for(tier_provider(dict(config), tier)) is UNKNOWN})
        self.observability = "observed" if not unseen else f"unavailable for {', '.join(unseen)}"


class _RetryLogHandler(logging.Handler):
    def __init__(self, prefix: str, counter: _SdkRetryCounter):
        super().__init__(level=logging.INFO)
        self.prefix, self.counter = prefix, counter

    def emit(self, record):
        if str(record.msg).startswith(self.prefix):
            self.counter.add()


class _observe_sdk_retries:
    """Installs a retry-log handler on every known SDK logger for the duration of a run
    (one run per process holds the run lock, so the counts belong to that run)."""

    def __enter__(self):
        from tradingagents.llm_clients.retry_policy import KNOWN

        self.counter = _SdkRetryCounter()
        self.installed = []
        for policy in KNOWN:
            logger = logging.getLogger(policy.logger)
            handler = _RetryLogHandler(policy.message_prefix, self.counter)
            self.installed.append((logger, handler, logger.level))
            if logger.getEffectiveLevel() > logging.INFO:
                logger.setLevel(logging.INFO)        # the SDKs log their retries at INFO
            logger.addHandler(handler)
        return self.counter

    def __exit__(self, *exc):
        for logger, handler, level in self.installed:
            logger.removeHandler(handler)
            logger.setLevel(level)
        return False


def config_for_source(source: str, base: Mapping[str, Any] | None = None) -> dict:
    """The run config for an interface: the configured defaults, with MCP runs on a log of their own.

    ``TRADINGAGENTS_MCP_MEMORY_LOG_PATH`` names the MCP log; unset, it sits beside
    the main log as ``mcp_trading_memory.md``.
    """
    _check_source(source)
    config = copy.deepcopy(dict(base if base is not None else DEFAULT_CONFIG))
    if source in BOUNDED_SOURCES:
        if config.get("llm_backup_max_retries") is None and config.get("llm_backup_retries") is None:
            config["llm_backup_retries"] = BOUNDED_BACKUP_RETRIES
        if config.get("llm_timeout") is None:
            config["llm_timeout"] = BOUNDED_TIMEOUT_SECONDS
        if "e1_breaker_seconds" not in config:
            raw = (os.environ.get(BREAKER_ENV) or "").strip().lower()
            if raw in ("0", "off", "none", "false", "no"):
                config["e1_breaker_seconds"] = None
            else:
                try:
                    config["e1_breaker_seconds"] = float(raw) if raw else BOUNDED_BREAKER_SECONDS
                except ValueError:
                    raise ValueError(f"{BREAKER_ENV}={raw!r} is not a number of seconds; use a number, or off") from None
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
    # Per-run LLM limits (run_budget.RunBudget); None = no limits (the CLI default). The dashboard and MCP set them.
    budget: RunBudget | None = None
    # Deadlines (run_deadline.RunDeadline); None = none (the CLI default).
    deadline: RunDeadline | None = None

    def __post_init__(self):
        _check_source(self.source)
        safe_ticker_component(self.ticker)
        if self.budget is not None and not isinstance(self.budget, RunBudget):
            raise TypeError("budget must be a run_budget.RunBudget or None")
        if self.deadline is not None and not isinstance(self.deadline, RunDeadline):
            raise TypeError("deadline must be a run_deadline.RunDeadline or None")
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
        with _observe_sdk_retries() as retries:
            return _run_locked(request, on_event, cancel, stats_handler, extra_callbacks, graph_factory,
                               defer_e3_save, retries)
    finally:
        _RUN_LOCK.release()


def is_busy() -> bool:
    return _RUN_LOCK.locked()


def _run_locked(request, on_event, cancel, stats_handler, extra_callbacks, graph_factory, defer_e3_save,
                retries=None):
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
    if retries is not None:
        retries.describe_for(config)         # which of this run's providers expose their retries
    config["e1_breaker_scope"] = run_id      # the run's breakers (quota-only without e1_breaker_seconds)
    adaptive = _adaptive_routing(config)     # opt-in (e1_adaptive.py); [] when off
    limits = request.budget if request.budget is not None and request.budget.active else None
    deadline = request.deadline
    if (limits is not None or deadline is not None) and cancel is None:
        cancel = threading.Event()          # a limit or deadline stops the run through the cancel path
    guard = CancellationGuard(cancel) if cancel is not None else None
    emit = on_event or (lambda event: None)
    budget_guard = None
    deadline_guard = (DeadlineGuard(deadline, cancel, on_exhausted=lambda snapshot: emit(
        AnalysisEvent(run_id, "deadline", state=snapshot))) if deadline is not None else None)

    def stopped(stage: str) -> AnalysisCancelled:
        """A deadline stop, a budget stop, or else a cancel by the caller (whichever tripped)."""
        in_flight, prevented = (guard.in_flight, guard.prevented) if guard else (0, 0)
        if deadline_guard is not None:
            deadline_guard.check_analysis()   # a cancel sent because the deadline passed is a deadline stop
        if deadline_guard is not None and deadline_guard.exhausted:
            snapshot = deadline_guard.snapshot()
            return AnalysisDeadlineExceeded(run_id, stage, in_flight, prevented + snapshot["llm_calls_refused"],
                                            deadline=snapshot)
        if budget_guard is not None and budget_guard.exhausted:
            snapshot = budget_guard.snapshot()
            return AnalysisBudgetExceeded(run_id, stage, in_flight, prevented + snapshot["llm_calls_refused"],
                                          budget=snapshot)
        return AnalysisCancelled(run_id, stage, in_flight, prevented)

    def check(stage: str) -> None:
        if deadline_guard is not None:
            deadline_guard.check_analysis()        # also between steps, not only at model calls
        if cancel is not None and cancel.is_set():
            raise stopped(stage)

    check("before it started")
    if limits is not None:
        # Raises ValueError for a cost limit without prices, before anything runs or is recorded.
        budget_guard = BudgetGuard(limits, cancel, on_exhausted=lambda snapshot: emit(
            AnalysisEvent(run_id, "budget", state=snapshot)))
        budget_guard.retry_counter = retries
    stats = stats_handler or StatsCallbackHandler()
    e3 = E3Session.from_env(ticker=ticker, analysis_date=trade_date, run_id=run_id,
                            source=request.source)
    # The limit, then the cancel guard, first: a refused call must not be counted as started by the others.
    failed_calls = _FailedCallCounter()
    callbacks = [*([deadline_guard] if deadline_guard else []), *([budget_guard] if budget_guard else []),
                 *([guard] if guard else []), stats,
                 *e3.callbacks, *extra_callbacks, failed_calls]
    # Taken before anything runs, for a failed run's record (a success measures from its usual points).
    began_wall = time.monotonic()      # durations only (a wall-clock step must not change them)
    began_at = datetime.now(UTC).isoformat(timespec="seconds")     # the run record's started_at (display)
    e2_on = bool(config.get("e2_cache_enabled", False) or e2_cache.is_enabled())
    e2_initial = e2_cache.stats_snapshot()
    e2_rows_initial = e2_cache.row_count(config.get("data_cache_dir")) if e2_on else None
    fallback_log = Path(config["results_dir"]) / FALLBACK_LOG_NAME
    fallback_initial = _size(fallback_log)
    started = e2_before = fallback_mark = None

    try:
        try:
            check("before the graph was built")
            e3.mark_analysis_started()
            graph = graph_factory(list(request.analysts), config=config, debug=False, callbacks=callbacks)
            started = time.monotonic()
            e2_before = e2_cache.stats_snapshot()
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
            raise stopped(str(signal)) from None
        except AnalysisCancelled:
            raise
        except BaseException as exc:
            # A step that failed because the run was cancelled under it is a cancellation.
            if cancel is not None and cancel.is_set():
                raise stopped("during a step") from exc
            raise
    except AnalysisCancelled as cancelled:
        # The graph has unwound: LangGraph waited for every running task, so nothing
        # of this run executes past here and the lock can be released.
        cancelled.mark_released()
        e3.mark_analysis_finished()
        if retries is not None:
            e3.sdk_retries_observed = retries.report()
        if isinstance(cancelled, AnalysisDeadlineExceeded):
            cancelled.deadline = deadline_guard.snapshot()
            e3.mark_deadline_exceeded(str(cancelled), cancelled.deadline)
            if budget_guard is not None:
                e3.run_budget = budget_guard.snapshot()
        elif isinstance(cancelled, AnalysisBudgetExceeded):
            cancelled.budget = budget_guard.snapshot()          # final: calls in flight have returned
            e3.mark_budget_exceeded(str(cancelled), cancelled.budget)
        else:
            e3.mark_cancelled(str(cancelled))
        e3.save()
        if not isinstance(cancelled, AnalysisDeadlineExceeded):
            _release_breakers(config)          # (a deadline stop releases them in its record below)
            # A budget stop or a cancel writes no token_usage.json; its run record says how it ended.
            stopped_usage = {**stats.get_stats(), "elapsed_seconds": round(time.monotonic() - (started or began_wall), 1)}
            if getattr(cancelled, "budget", None):
                stopped_usage["run_budget"] = cancelled.budget
            _store_run(config, request, run_id,
                       "budget_exceeded" if isinstance(cancelled, AnalysisBudgetExceeded) else "cancelled",
                       stopped_usage, e1_events=_new_lines(fallback_log, fallback_initial),
                       e3_summary_path=e3.saved_path, error=sanitize_error(cancelled),
                       error_category=error_category(cancelled), started_at=began_at)
        if isinstance(cancelled, AnalysisDeadlineExceeded):
            # Keep what the run recorded (as for a failure), labelled as a deadline stop.
            _record_failure(cancelled, error=sanitize_error(cancelled), request=request, run_id=run_id,
                            config=config, stats=stats, failed_calls=failed_calls.failed,
                            started=started or began_wall, e2_on=e2_on, e2_before=e2_before or e2_initial,
                            e2_rows_before=e2_rows_initial, fallback_log=fallback_log,
                            fallback_mark=fallback_mark if fallback_mark is not None else fallback_initial,
                            budget_guard=budget_guard, e3=e3, retries=retries, status="deadline_exceeded",
                            deadline=cancelled.deadline, started_at=began_at)
        raise
    except BaseException as exc:
        e3.mark_analysis_finished()
        error = sanitize_error(exc)
        e3.mark_failed(error)
        if retries is not None:
            e3.sdk_retries_observed = retries.report()
        if budget_guard is not None:
            e3.run_budget = budget_guard.snapshot()
        e3.save()
        _record_failure(exc, error=error, request=request, run_id=run_id, config=config, stats=stats,
                        failed_calls=failed_calls.failed, started=started or began_wall, started_at=began_at,
                        e2_on=e2_on, e2_before=e2_before or e2_initial, e2_rows_before=e2_rows_initial,
                        fallback_log=fallback_log,
                        fallback_mark=fallback_mark if fallback_mark is not None else fallback_initial,
                        budget_guard=budget_guard, e3=e3, retries=retries)
        raise
    e3.mark_analysis_finished()

    rating = run_rating(final_state)
    usage = {
        **stats.get_stats(),
        "llm_provider": config.get("llm_provider"),
        "quick_think_llm": config.get("quick_think_llm"),
        "deep_think_llm": config.get("deep_think_llm"),
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }
    usage["tokens_total"] = usage["tokens_in"] + usage["tokens_out"]
    # E2: this run's own hits and misses, not the process's running total.
    if config.get("e2_cache_enabled", False) or e2_cache.is_enabled():
        usage["e2_cache"] = _e2_delta(e2_before, e2_cache.stats_snapshot())
    usage["run_id"] = run_id
    usage["source"] = request.source
    if budget_guard is not None:
        usage["run_budget"] = budget_guard.snapshot()
        e3.run_budget = usage["run_budget"]
    if deadline is not None:
        from tradingagents.run_deadline import outcome

        usage["run_deadline"] = outcome(deadline)
    breaker_stats = _release_breakers(config)
    if breaker_stats is not None:
        usage["e1_breaker"] = breaker_stats
    if adaptive:
        usage["e1_adaptive"] = adaptive
    if retries is not None:
        e3.sdk_retries_observed = retries.report()

    results_dir = run_directory(config, ticker, trade_date)
    results_dir.mkdir(parents=True, exist_ok=True)
    usage_path = results_dir / "token_usage.json"
    usage_path.write_text(json.dumps(usage, indent=2), encoding="utf-8")

    e3.mark_completed(decision=final_state.get("final_trade_decision"))
    if not defer_e3_save:
        e3.save()
    fallback_events = _new_lines(fallback_log, fallback_initial)
    _store_run(config, request, run_id, "completed", usage, rating=rating, final_state=final_state,
               e1_events=fallback_events, e3_summary_path=e3.saved_path, started_at=began_at)

    return AnalysisResult(
        run_id=run_id, source=request.source, ticker=ticker, trade_date=trade_date,
        asset_type=asset_type, analysts=request.analysts, rating=rating,
        final_state=final_state, usage=usage, results_dir=results_dir, usage_path=usage_path,
        e3_summary_path=e3.saved_path,
        # From the run's start: an adaptive-routing decision is logged while the graph is built.
        fallback_events=fallback_events,
        graph=graph, e3=e3,
    )


def _adaptive_routing(config: dict) -> list[dict]:
    """E1 adaptive routing (opt-in): decide each tier from recent E3 records; a demotion makes build_model
    open that tier's breaker for the run. Returns the decisions for the run's record ([] when off)."""
    from dataclasses import asdict

    from tradingagents import e1_adaptive

    decisions = e1_adaptive.plan(config)
    config["e1_prefer_backup"] = {d.tier: d.reason for d in decisions if d.action == "prefer_backup"}
    return [asdict(d) for d in decisions]


def _release_breakers(config) -> dict | None:
    """Forget this run's E1 circuit breakers. Returns their trips and skipped calls when the breaker is
    configured (dashboard, MCP) or tripped (a used-up quota, in any source); otherwise None, so a CLI run that
    never needed it keeps Phase 2's record."""
    if not config.get("e1_breaker_scope"):
        return None
    from tradingagents.e1_routing import release_breakers

    stats = release_breakers(config["e1_breaker_scope"])
    return stats if config.get("e1_breaker_seconds") or stats["trips"] else None


def _record_failure(exc, *, error, request, run_id, config, stats, failed_calls, started, e2_on, e2_before,
                    e2_rows_before, fallback_log, fallback_mark, budget_guard, e3, retries=None,
                    status="failed", deadline=None, started_at=None) -> None:
    """Write a failed run's token_usage.json with what is known, and attach it to the exception.

    Counts and tokens are those the run reported before it failed; nothing is estimated for calls
    that reported no usage. Never raises: recording must not hide the original error.
    """
    try:
        counted = stats.get_stats()
        usage = {
            **counted,
            "llm_provider": config.get("llm_provider"),
            "quick_think_llm": config.get("quick_think_llm"),
            "deep_think_llm": config.get("deep_think_llm"),
            "elapsed_seconds": round(time.monotonic() - started, 1),
        }
        usage["tokens_total"] = usage["tokens_in"] + usage["tokens_out"]
        usage.update(
            run_id=run_id, source=request.source, status=status, error=error,
            llm_calls_failed=failed_calls, llm_calls_succeeded=max(counted.get("llm_calls", 0) - failed_calls, 0),
            tokens_note="tokens reported by the calls that completed; calls that failed reported none",
            e1_fallback_events=_new_lines(fallback_log, fallback_mark),
            error_category=error_category(exc),
            e1_breaker=_release_breakers(config),
            sdk_retries_observed=retries.report() if retries is not None else None,
            sdk_retries_observability=retries.observability if retries is not None else "not measured",
        )
        if e2_on:
            usage["e2_cache"] = _e2_delta(e2_before, e2_cache.stats_snapshot())
            rows_after = e2_cache.row_count(config.get("data_cache_dir"))
            if e2_rows_before is not None and rows_after is not None:
                usage["e2_cache"]["saved_entries"] = rows_after - e2_rows_before
        if budget_guard is not None:
            usage["run_budget"] = budget_guard.snapshot()
        if deadline is not None:
            usage["run_deadline"] = deadline
        summary = None
        if e3.saved_path is not None:
            usage["e3_summary_path"] = str(e3.saved_path)
            try:
                summary = json.loads(Path(e3.saved_path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                summary = None
        cost = e3_cost(summary)
        usage["e3_cost"] = {"estimated_cost_usd": cost["estimated_cost_usd"], "basis": cost["basis"],
                            "partial_cost_usd": cost["partial_cost_usd"], "note": cost["note"]}
        if summary:
            usage["langfuse_flush"] = summary.get("langfuse_flush")
            usage["per_agent"] = summary.get("per_agent")
            usage["timing"] = summary.get("timing")
        results_dir = run_directory(config, request.ticker, request.trade_date)
        results_dir.mkdir(parents=True, exist_ok=True)
        usage_path = results_dir / "token_usage.json"
        usage_path.write_text(json.dumps(usage, indent=2), encoding="utf-8")
        _store_run(config, request, run_id, status, usage, e1_events=usage["e1_fallback_events"],
                   e3_summary_path=e3.saved_path, error=error, error_category=usage["error_category"],
                   started_at=started_at)
    except Exception:  # noqa: BLE001 - the run's own error is what the caller must see
        logger.warning("could not record the failed run %s", run_id, exc_info=True)
        return
    for name, value in (("analysis_failure", usage), ("usage_path", usage_path),
                        ("e3_summary_path", e3.saved_path)):
        with contextlib.suppress(AttributeError, TypeError):    # some exception types refuse attributes
            setattr(exc, name, value)


def attach_e3_summary(request: AnalysisRequest, run_id: str, e3_summary_path) -> bool:
    """Link an E3 summary saved after the run's record (the CLI's ``defer_e3_save``) into that record, with its
    cost. Without it a CLI run's record names no E3 summary, so get_run_metrics reports its cost as unavailable.
    Never raises; False when there is nothing to attach or the record could not be updated."""
    from tradingagents import run_store

    if e3_summary_path is None:
        return False
    try:
        config = (dict(request.config) if request.config is not None else config_for_source(request.source))
        record = run_store.load(config["results_dir"], run_id)
        if record is None:
            return False
        record["e3_summary_path"] = str(Path(e3_summary_path).absolute())
        usage = dict(record.get("usage") or {})
        if "e3_cost" not in usage:
            with contextlib.suppress(OSError, ValueError):
                usage["e3_cost"] = e3_cost(json.loads(Path(e3_summary_path).read_text(encoding="utf-8")))
        record["usage"] = usage
        return run_store.save(config["results_dir"], record) is not None
    except Exception:  # noqa: BLE001
        logger.warning("could not attach the E3 summary to the run record for %s", run_id, exc_info=True)
        return False


def _store_run(config, request, run_id, status, usage, *, rating=None, final_state=None, e1_events=None,
               e3_summary_path=None, error=None, error_category=None, started_at=None) -> None:
    """The run's record in run_store, keyed by run ID (every outcome). Never raises.

    ``started_at``: when the service began the run; without it (a worker the parent stopped), when the request's
    deadline started (the dashboard click or the MCP call), else unknown (None)."""
    from tradingagents import run_store

    try:
        if started_at is None and getattr(request, "deadline", None) is not None:
            started_at = datetime.fromtimestamp(request.deadline.started_at, UTC).isoformat(timespec="seconds")
        cost = None
        if e3_summary_path is not None:
            with contextlib.suppress(OSError, ValueError):
                cost = e3_cost(json.loads(Path(e3_summary_path).read_text(encoding="utf-8")))
        record = run_store.build_record(
            run_id=run_id, source=request.source, status=status, ticker=request.ticker,
            trade_date=request.trade_date, asset_type=request.asset_type, analysts=request.analysts,
            usage={**usage, **({"e3_cost": cost} if cost is not None and "e3_cost" not in usage else {})},
            rating=rating, needs_review=is_review(rating) if rating is not None else None,
            final_state=final_state, e1_events=e1_events, e3_summary_path=e3_summary_path,
            error=error, error_category=error_category, started_at=started_at)
        run_store.save(config["results_dir"], record)
    except Exception:  # noqa: BLE001
        logger.warning("could not store the run record for %s", run_id, exc_info=True)


def _e2_delta(before: dict, after: dict) -> dict:
    counts = {key: after[key] - before.get(key, 0) for key in ("hits", "misses", "skipped")}
    looked_up = counts["hits"] + counts["misses"]
    counts["hit_rate_pct"] = round(100 * counts["hits"] / looked_up, 1) if looked_up else None
    for key in ("expired", "refreshed"):          # E2 freshness (Phase 3): shown only when it happened
        delta = after.get(key, 0) - before.get(key, 0)
        if delta:
            counts[key] = delta
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
