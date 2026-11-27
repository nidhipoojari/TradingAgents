"""F2: an MCP server with five read-only tools.

- ``analyze_stock``: run a new analysis and return the decision (minutes; costs model tokens).
- ``get_report``, ``get_run_metrics``, ``list_recent_runs``, ``propose_trade``: answer from saved runs
  (``tradingagents.mcp_read_tools``, reading ``run_store``); they never start an analysis, call a model,
  fetch market data or place an order.

Other MCP-compatible AI assistants can ask TradingAgents for an analysis of a
stock and get the decision back. The tool runs the same shared analysis service
as the CLI and the dashboard (``tradingagents.service``), so E1 routing and
backup models, the E2 market-data cache and E3 tracing all apply unchanged.

Read-only by construction: this module (and mcp_read_tools, run_store, trade_rules) never imports the
execution layer (``tradingagents.execution``), the dashboard or Alpaca, and places no orders; a test pins
that. ``propose_trade`` applies F1's proposal rules from ``tradingagents.trade_rules`` and returns a
proposal only: nothing here can approve, sign, submit, cancel or execute an order. A run
does write its own analysis records (token_usage.json, the state log, the E3
summary) and records its decision in a separate MCP memory log, so requests from
other assistants do not feed lessons into the CLI's and dashboard's decisions.

Every call to any of the five tools writes one MCP_REQUEST line to ``<results_dir>/mcp_requests.jsonl``
(``mcp_audit``): request ID, tool, times, status, error category, the run ID and a safe argument summary.
Never the token, a header or raw free text. The log grants nothing and changes no answer.

Run it:
    tradingagents-mcp                      # stdio, for a local assistant that launches it
    TRADINGAGENTS_MCP_TOKEN=... tradingagents-mcp --transport http --port 8765

Security: stdio is the recommended transport for a local assistant and needs no
key (the assistant starts the process with your environment). HTTP refuses to
start without ``TRADINGAGENTS_MCP_TOKEN`` (clients send it as ``Authorization:
Bearer <token>``) and binds to 127.0.0.1. Any other address, including 0.0.0.0,
is refused unless ``--allow-remote`` is given together with ``--tls-cert`` and
``--tls-key`` (HTTPS) and a token of at least 32 characters, so the server cannot
be exposed unauthenticated or unencrypted by accident.

Per-run limits: each analysis has an LLM-call limit and an estimated-cost ceiling, as in the
dashboard (``run_budget.mcp_budget``): by default the expected calls for the requested analysts x 1.5
(market only: 15; all four: 24) and $0.04. The server's environment sets them
(``TRADINGAGENTS_MCP_MAX_LLM_CALLS``, ``TRADINGAGENTS_MCP_MAX_COST_USD``; a number, or off); the tool takes
no argument that could raise them. The cost limit needs ``E3_PRICING_JSON``: without prices every call is
refused as a server setting problem rather than run unlimited.

Long runs: an analysis takes one to several minutes. The tool reports progress
as each report is filed, and stops the run at the next step after
``TRADINGAGENTS_MCP_TIMEOUT_SECONDS`` (default 900). One analysis runs at a time;
a second request while one runs is refused at once rather than queued.

Note: no ``from __future__ import annotations`` here; FastMCP reads the tool's
type hints, including the Context class imported inside ``create_server``.
"""

import argparse
import hmac
import logging
import os
import sys
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from tradingagents import e1_routing, mcp_audit, service

logger = logging.getLogger(__name__)

TOKEN_ENV = "TRADINGAGENTS_MCP_TOKEN"
TIMEOUT_ENV = "TRADINGAGENTS_MCP_TIMEOUT_SECONDS"
REPORT_CHARS_ENV = "TRADINGAGENTS_MCP_MAX_REPORT_CHARS"
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_REPORT_CHARS = 4000

TOOL_DESCRIPTION = (
    "Run the TradingAgents multi-agent analysis (market, sentiment, news and fundamentals "
    "analysts, a bull/bear research debate, a trader and a risk debate) for one stock ticker "
    "and return the Portfolio Manager's rating (Buy, Overweight, Hold, Underweight, Sell, or "
    "REVIEW when no rating could be read) with the decision text. Read-only: it never places "
    "orders. Takes one to several minutes. Research output, not financial advice."
)

SERVER_INSTRUCTIONS = (
    "TradingAgents exposes five read-only tools. analyze_stock runs a full multi-agent LLM "
    "analysis that takes minutes and costs model tokens; call it once per ticker and date "
    "and reuse the result. Dates are YYYY-MM-DD and cannot be in the future; omit the date "
    "for today. A REVIEW rating means the decision had no readable rating. The other four answer "
    "from saved runs at no model cost: list_recent_runs finds run IDs, get_report returns a run's "
    "reports and debates, get_run_metrics its routing, cache, token, cost (estimated) and timing "
    "records, and propose_trade what the F1 paper-trading rules would allow (a proposal only; "
    "no tool can place, approve or cancel an order)."
)

READ_TOOLS = {
    "get_report": ("Get report", (
        "Return the saved reports of an earlier analysis run: analyst reports, the bull/bear "
        "investment debate, the investment and trader plans, the risk debate and the final decision, "
        "each truncated. section narrows it to one of: market, sentiment, news, fundamentals, "
        "investment_debate, investment_plan, trader_plan, risk_debate, final_decision. Reads saved "
        "files only; never re-runs the analysis.")),
    "get_run_metrics": ("Get run metrics", (
        "Return the recorded metrics of an earlier run: E1 routing and backup-model events, E2 cache "
        "counts, E3 token usage and per-agent figures, estimated cost (an estimate, not provider "
        "billing), per-run limits, errors and timing. Reads saved files only; makes no model or "
        "market-data request.")),
    "list_recent_runs": ("List recent runs", (
        "List recent saved analysis runs, newest first, with run ID, ticker, trade date, source, "
        "status, rating and summary figures. limit: 1 to 50 (default 10).")),
    "propose_trade": ("Propose trade", (
        "Say what paper trade the F1 rules would allow for an earlier run's rating (a buy, a sell "
        "only to reduce a long position, or no trade, with the reason and the order rules). "
        "Read-only: it never approves, signs, submits, cancels or executes an order; a signed-in "
        "person decides in the TradingAgents dashboard. Research output, not financial advice.")),
}
READ_TIMEOUT_SECONDS = 30.0


def read_annotations(title: str) -> dict:
    return {"title": title, "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True,      # the same saved record gives the same answer
            "openWorldHint": False}      # local saved records only: no provider, no market data, no broker

READ_ONLY_ANNOTATIONS = {
    "title": "Analyze stock",
    "readOnlyHint": True,       # no orders, no changes outside its own analysis records
    "destructiveHint": False,
    "idempotentHint": False,    # a re-run is a new LLM analysis and may differ
    "openWorldHint": True,      # reads market data and calls LLM providers
}

SUMMARY_SECTIONS = {
    "investment_plan": "investment_plan",
    "trader_plan": "trader_investment_plan",
}
REPORT_SECTIONS = {
    "market": "market_report",
    "sentiment": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _truncate(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n[... truncated, {len(text) - limit:,} more characters in the saved run]"


class ServerSettingError(ValueError):
    """The server's own configuration (not the client's arguments) prevents the run."""


class InvalidRequestError(ValueError):
    """The client's arguments failed validation (before any run started)."""


def request_budget(analysts: int):
    """The run's limits from the server's environment; ServerSettingError when they cannot be enforced."""
    from tradingagents import run_budget

    config = service.config_for_source("mcp")
    try:
        budget = run_budget.mcp_budget(analysts, config["max_debate_rounds"], config["max_risk_discuss_rounds"])
        run_budget.check_pricing(budget, cost_env=run_budget.MCP_COST_ENV)
    except ValueError as exc:
        raise ServerSettingError(str(exc)) from None
    return budget


def build_request(ticker: str, trade_date: str | None = None,
                  analysts: list[str] | None = None, run_id: str | None = None,
                  deadline=None) -> service.AnalysisRequest:
    """Validate the tool's arguments the way the CLI validates its flags."""
    from cli.models import AnalystType
    from cli.prompts import (
        detect_asset_type,
        filter_analysts_for_asset_type,
        parse_analysis_date,
        parse_analysts,
        parse_ticker,
    )
    from tradingagents.dataflows.date_window import get_current_date

    symbol = parse_ticker(ticker or "")
    asset_type = detect_asset_type(symbol)
    day = parse_analysis_date(trade_date) if trade_date else get_current_date()
    chosen = (parse_analysts(",".join(analysts), asset_type) if analysts
              else filter_analysts_for_asset_type(list(AnalystType), asset_type))
    return service.AnalysisRequest(
        ticker=symbol, trade_date=day, source="mcp", asset_type=asset_type.value,
        analysts=tuple(a.value for a in chosen), run_id=run_id, deadline=deadline,
        budget=request_budget(len(chosen)),
    )


def summarize(result: service.AnalysisResult, include_reports: bool = False,
              max_chars: int | None = None) -> dict[str, Any]:
    """The tool's answer: the decision first, sizes bounded for the caller's context window."""
    limit = int(max_chars or _env_float(REPORT_CHARS_ENV, DEFAULT_REPORT_CHARS))
    state = result.final_state
    usage = result.usage
    answer: dict[str, Any] = {
        "run_id": result.run_id,
        "source": result.source,
        "ticker": result.ticker,
        "trade_date": result.trade_date,
        "asset_type": result.asset_type,
        "analysts": ["sentiment" if a == "social" else a for a in result.analysts],
        "rating": result.rating,
        "needs_review": result.is_review,
        "decision": _truncate(result.decision_text, limit),
        **{name: _truncate(state.get(key), limit) for name, key in SUMMARY_SECTIONS.items()},
        "usage": {key: usage.get(key) for key in
                  ("llm_calls", "tool_calls", "tokens_in", "tokens_out", "tokens_total",
                   "elapsed_seconds", "e2_cache") if key in usage},
        **({"run_budget": _budget_view(usage["run_budget"])} if usage.get("run_budget") else {}),
        "model_fallbacks": e1_routing.summarize_events(result.fallback_events)["backup_calls"],
        "e1_events": e1_routing.summarize_events(result.fallback_events),
        **_e3_cost(result),
        "note": "Research output, not financial advice. This tool never places orders.",
    }
    if include_reports:
        answer["reports"] = {name: _truncate(state.get(key), limit)
                             for name, key in REPORT_SECTIONS.items() if state.get(key)}
    return answer


def _e3_cost(result: service.AnalysisResult) -> dict[str, Any]:
    """E3's cost for the run (see service.e3_cost): a partial sum is never given as the estimate."""
    import json

    summary = None
    if result.e3_summary_path is not None:
        try:
            summary = json.loads(result.e3_summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            summary = None
    cost = service.e3_cost(summary)
    return {"estimated_cost_usd": cost["estimated_cost_usd"], "cost_basis": cost["basis"],
            "partial_cost_usd": cost["partial_cost_usd"], "cost_note": cost["note"]}


_BUDGET_KEYS = ("max_llm_calls", "max_cost_usd", "llm_calls_started", "reported_usage_cost_usd",
                "unreported_usage_estimate_usd", "budget_cost_usd", "exhausted", "reason")


def _budget_view(budget: dict) -> dict:
    return {k: budget.get(k) for k in _BUDGET_KEYS}


_PARTIAL_KEYS = ("llm_calls", "llm_calls_succeeded", "llm_calls_failed", "llm_calls_in_flight", "tokens_in",
                 "tokens_out", "tool_calls", "sdk_retries_observed", "elapsed_seconds")


def failure_details(exc: BaseException, run_id: str | None) -> dict[str, Any]:
    """What a stopped run can tell an MCP client: run ID, a category, a sanitized message, and the partial
    metrics the run recorded before it stopped. No file paths, stack traces or credentials."""
    record = getattr(exc, "analysis_failure", None) or {}
    details: dict[str, Any] = {
        "run_id": record.get("run_id") or run_id,
        "category": service.error_category(exc),
        "message": service.sanitize_error(service.describe_error(exc), 400),
    }
    hint = service.failure_hint(exc)
    if hint:
        details["hint"] = hint
    partial = {key: record[key] for key in _PARTIAL_KEYS if record.get(key) is not None}
    if record.get("e1_fallback_events") is not None:
        events = e1_routing.summarize_events(record["e1_fallback_events"])
        partial["e1_fallback_switches"] = events["backup_calls"]
        partial["e1_events"] = events
    if record.get("e2_cache"):
        partial["e2_cache"] = record["e2_cache"]
    if record.get("e3_cost"):
        partial["e3_cost"] = record["e3_cost"]
    budget = record.get("run_budget") or getattr(exc, "budget", None)
    if budget:
        partial["run_budget"] = _budget_view(budget)
    if partial:
        partial["note"] = "what the run reported before it stopped; not complete"
        details["partial_metrics"] = partial
    return details


def failure_message(exc: BaseException, run_id: str | None, lead: str = "analysis failed") -> str:
    details = failure_details(exc, run_id)
    import json

    text = f"{lead} [{details['category']}] run {details['run_id']}: {details['message']}"
    if details.get("hint"):
        text += f"\nwhat to do: {details['hint']}"
    if "partial_metrics" in details:
        text += "\npartial metrics: " + json.dumps(details["partial_metrics"], sort_keys=True)
    return text


def analyze_stock_sync(
    ticker: str,
    trade_date: str | None = None,
    analysts: list[str] | None = None,
    include_reports: bool = False,
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
    cancel: threading.Event | None = None,
    run=None,
    run_id: str | None = None,
    deadline=None,
) -> dict[str, Any]:
    """The tool without the protocol: validate, run through the shared service, summarize.

    ``run`` defaults to the analysis worker process, so a timed-out or cancelled
    request releases the server within the grace period even if a model call hangs.
    """
    if run is None:
        from tradingagents.worker import analysis_runner

        run = analysis_runner()
    try:
        request = build_request(ticker, trade_date, analysts, run_id=run_id, deadline=deadline)
    except ServerSettingError:
        raise
    except ValueError as exc:
        # Only argument validation reads as "invalid request"; a ValueError raised later, inside the run,
        # is a run failure and is reported with its run ID, category and partial metrics.
        raise InvalidRequestError(str(exc)) from None
    last = [-1]

    def on_event(event: service.AnalysisEvent) -> None:
        done, total, label = event.progress
        if on_progress is not None and event.kind == "step" and done != last[0]:
            last[0] = done
            on_progress(done, total, label)

    result = run(request, on_event=on_event, cancel=cancel)
    return summarize(result, include_reports)


def api_key_verifier(token: str):
    """Bearer-token check for the HTTP transport, compared in constant time."""
    from fastmcp.server.auth import AccessToken, TokenVerifier

    class ApiKeyVerifier(TokenVerifier):
        async def verify_token(self, presented: str):
            if presented and hmac.compare_digest(presented.encode(), token.encode()):
                return AccessToken(token=presented, client_id="tradingagents-mcp", scopes=[])
            return None

    return ApiKeyVerifier()


class _Failure(Exception):
    """A ToolError with how the call ended, for the MCP_REQUEST line; analyze_stock re-raises the ToolError."""

    def __init__(self, error: Exception, status: str, category: str, *, ran: bool = True):
        super().__init__(str(error))
        self.error, self.status, self.category, self.ran = error, status, category, ran


def create_server(*, auth=None, timeout: float | None = None, run=None):
    """Build the FastMCP server with its five tools. ``run`` is the service call (tests stub it)."""
    from typing import Annotated

    import anyio
    from fastmcp import Context, FastMCP
    from fastmcp.exceptions import ToolError
    from pydantic import Field

    limit = timeout or _env_float(TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS)
    mcp = FastMCP("TradingAgents", instructions=SERVER_INSTRUCTIONS, auth=auth)

    @mcp.tool(name="analyze_stock", description=TOOL_DESCRIPTION, annotations=READ_ONLY_ANNOTATIONS)
    async def analyze_stock(
        ticker: str,
        ctx: Context,
        trade_date: str | None = None,
        analysts: list[str] | None = None,
        # Strict: a JSON boolean only. In lax mode "yes", "on", "1" or 1 become True and start a full (paid)
        # analysis from a malformed argument; the same reason RecentLimit below is strict.
        include_reports: Annotated[bool, Field(strict=True)] = False,
    ) -> dict:
        """ticker: e.g. NVDA, 0700.HK, BTC-USD. trade_date: YYYY-MM-DD, today or earlier; omit
        for today. analysts: any of market, sentiment, news, fundamentals; omit for all.
        include_reports: also return each analyst's report (truncated)."""
        cancel = threading.Event()
        run_id = service.new_run_id()           # known before the run, so every outcome can cite it
        audit = mcp_audit.Request("analyze_stock", {"ticker": ticker, "trade_date": trade_date,
                                                    "analysts": analysts, "include_reports": include_reports})
        try:
            answer = await _analyze(ticker, ctx, trade_date, analysts, include_reports, cancel, run_id)
        except _Failure as failure:
            audit.finish(failure.status, failure.category, run_id=run_id if failure.ran else None)
            raise failure.error from None
        except anyio.get_cancelled_exc_class():
            audit.finish("cancelled", "cancelled", run_id=run_id)
            raise
        except BaseException:
            audit.finish("error", "internal_error", run_id=run_id)
            raise
        audit.finish("ok", run_id=run_id)
        return answer

    async def _analyze(ticker, ctx, trade_date, analysts, include_reports, cancel, run_id) -> dict:
        from tradingagents.run_deadline import interactive_deadline

        try:
            deadline = interactive_deadline()   # counts from this call; worker start-up included
        except ValueError as exc:
            raise _Failure(ToolError(f"invalid server setting: {exc}"), "error", "invalid_server_setting",
                           ran=False) from None

        def progress(done: int, total: int, label: str) -> None:
            # Called from the worker thread; a lost notification must not stop the run.
            try:
                anyio.from_thread.run(ctx.report_progress, done, total, f"{done}/{total} done; next: {label}")
            except Exception:  # noqa: BLE001
                logger.debug("progress notification failed", exc_info=True)

        call = partial(analyze_stock_sync, ticker, trade_date, analysts, include_reports,
                       on_progress=progress, cancel=cancel, run=run, run_id=run_id, deadline=deadline)
        try:
            with anyio.fail_after(limit):
                return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
        except TimeoutError:
            cancel.set()   # the run stops at its next step and frees the server
            raise _Failure(ToolError(f"analysis timed out after {limit:.0f}s and was stopped [timeout] run "
                                     f"{run_id}; raise {TIMEOUT_ENV} or analyze fewer analysts"),
                           "timeout", "timeout") from None
        except anyio.get_cancelled_exc_class():
            cancel.set()   # the client went away or cancelled the request
            raise
        except ServerSettingError as exc:
            raise _Failure(ToolError(f"invalid server setting: {service.sanitize_error(str(exc), 400)}"),
                           "error", "invalid_server_setting", ran=False) from None
        except InvalidRequestError as exc:
            raise _Failure(ToolError(f"invalid request: {service.sanitize_error(str(exc), 400)}"),
                           "error", "invalid_request", ran=False) from None
        except service.AnalysisBusyError as exc:
            raise _Failure(ToolError(str(exc)), "error", "busy", ran=False) from None
        except service.AnalysisCancelled as exc:
            category = service.error_category(exc)
            raise _Failure(ToolError(failure_message(exc, run_id, lead="analysis stopped")),
                           "timeout" if category == "timeout" else "cancelled" if category == "cancelled" else "error",
                           category) from None
        except Exception as exc:  # noqa: BLE001 - a provider or data failure, reported briefly
            # The traceback goes to the server's log (stderr), never to the client.
            logger.exception("analyze_stock failed (run %s)", run_id)
            raise _Failure(ToolError(failure_message(exc, run_id)), "error", service.error_category(exc)) from None

    async def read_tool(name: str, function, *args, arguments: dict) -> dict:
        """Run a read tool off the event loop; its errors become plain tool errors (no paths, no traces).
        Every call, whatever its outcome, writes one MCP_REQUEST line (mcp_audit)."""
        from tradingagents import mcp_read_tools as tools

        rid = arguments.get("run_id")
        audit = mcp_audit.Request(name, arguments, run_id=rid if isinstance(rid, str) else None)
        try:
            with anyio.fail_after(READ_TIMEOUT_SECONDS):
                answer = await anyio.to_thread.run_sync(partial(function, *args), abandon_on_cancel=True)
        except TimeoutError:
            audit.finish("timeout", "timeout")
            raise ToolError(f"{name} timed out after {READ_TIMEOUT_SECONDS:.0f}s reading saved runs") from None
        except tools.ToolInputError as exc:
            audit.finish("error", "invalid_request")
            raise ToolError(f"invalid request: {exc}") from None
        except tools.ServerSetting as exc:
            audit.finish("error", "invalid_server_setting")
            raise ToolError(f"invalid server setting: {service.sanitize_error(str(exc), 400)}") from None
        except tools.RunNotFound as exc:
            audit.finish("error", "run_not_found", run_id="")       # not a reference to a stored run
            raise ToolError(f"run not found: {exc}") from None
        except tools.ReportUnavailable as exc:
            audit.finish("error", "report_unavailable")
            raise ToolError(f"report unavailable: {service.sanitize_error(str(exc), 400)}") from None
        except anyio.get_cancelled_exc_class():
            audit.finish("cancelled", "cancelled")
            raise
        except Exception:  # noqa: BLE001 - the traceback goes to the server log, never to the client
            audit.finish("error", "internal_error")
            logger.exception("%s failed", name)
            raise ToolError(f"{name} failed while reading saved runs; see the server log") from None
        audit.finish("ok")
        return answer

    from tradingagents import mcp_read_tools

    # Strict: a JSON integer only. Without it, pydantic's lax mode turns "10" into 10, 2.0 into 2 and true into 1
    # before the tool sees them. 0 and negatives pass this check and get the tool's own message (see
    # mcp_read_tools.list_recent_runs); above 50 is capped there, not refused. "minimum" tells clients the range.
    RecentLimit = Annotated[int, Field(strict=True, json_schema_extra={"minimum": 1})]

    @mcp.tool(name="get_report", description=READ_TOOLS["get_report"][1],
              annotations=read_annotations(READ_TOOLS["get_report"][0]))
    async def get_report(run_id: str, section: str | None = None) -> dict:
        """run_id: from analyze_stock or list_recent_runs. section: one part only, or omit for all."""
        return await read_tool("get_report", mcp_read_tools.get_report, run_id, section,
                               arguments={"run_id": run_id, "section": section})

    @mcp.tool(name="get_run_metrics", description=READ_TOOLS["get_run_metrics"][1],
              annotations=read_annotations(READ_TOOLS["get_run_metrics"][0]))
    async def get_run_metrics(run_id: str) -> dict:
        """run_id: from analyze_stock or list_recent_runs."""
        return await read_tool("get_run_metrics", mcp_read_tools.get_run_metrics, run_id,
                               arguments={"run_id": run_id})

    @mcp.tool(name="list_recent_runs", description=READ_TOOLS["list_recent_runs"][1],
              annotations=read_annotations(READ_TOOLS["list_recent_runs"][0]))
    async def list_recent_runs(limit: RecentLimit = mcp_read_tools.DEFAULT_RECENT) -> dict:
        """limit: how many runs, 1 to 50 (more is capped at 50)."""
        return await read_tool("list_recent_runs", mcp_read_tools.list_recent_runs, limit,
                               arguments={"limit": limit})

    @mcp.tool(name="propose_trade", description=READ_TOOLS["propose_trade"][1],
              annotations=read_annotations(READ_TOOLS["propose_trade"][0]))
    async def propose_trade(run_id: str) -> dict:
        """run_id: a completed run, from analyze_stock or list_recent_runs."""
        return await read_tool("propose_trade", mcp_read_tools.propose_trade, run_id,
                               arguments={"run_id": run_id})

    return mcp


MIN_TOKEN_CHARS = 16
MIN_REMOTE_TOKEN_CHARS = 32


def is_loopback(host: str) -> bool:
    """127.0.0.0/8, ::1 or the name localhost. Anything else (0.0.0.0, a LAN or public
    address, any other host name) can be reached from other machines."""
    import ipaddress

    if (host or "").strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def http_exposure_problems(host: str, token: str, *, allow_remote: bool,
                           tls_cert: str | None, tls_key: str | None) -> list[str]:
    """Why the HTTP transport must not start with these settings; empty when it may.

    Loopback needs a bearer token. Any other address needs, all together,
    ``--allow-remote``, a TLS certificate and key (so the token never crosses the
    network in clear text) and a token of at least MIN_REMOTE_TOKEN_CHARS.
    """
    problems = []
    if len(token) < MIN_TOKEN_CHARS:
        problems.append(f"the http transport needs {TOKEN_ENV} set to a secret of at least "
                        f"{MIN_TOKEN_CHARS} characters")
    if bool(tls_cert) != bool(tls_key):
        problems.append("TLS needs both --tls-cert and --tls-key")
    for label, path in (("--tls-cert", tls_cert), ("--tls-key", tls_key)):
        if path and not Path(path).is_file():
            problems.append(f"{label} {path} is not a readable file")
    if is_loopback(host):
        return problems
    if not allow_remote:
        problems.append(f"{host} can be reached from other machines; the server binds to 127.0.0.1 unless "
                        "--allow-remote is given (with --tls-cert and --tls-key). For a local assistant, "
                        "use the stdio transport instead")
    if not (tls_cert and tls_key):
        problems.append(f"serving on {host} needs TLS (--tls-cert and --tls-key); without it the bearer "
                        "token and the analyses cross the network unencrypted")
    if len(token) < MIN_REMOTE_TOKEN_CHARS:
        problems.append(f"serving beyond this machine needs {TOKEN_ENV} of at least "
                        f"{MIN_REMOTE_TOKEN_CHARS} characters")
    return problems


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tradingagents-mcp",
        description="TradingAgents MCP server. stdio (the default) is the recommended local transport.")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="http only; loopback unless --allow-remote")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-remote", action="store_true",
                        help="http only: allow a non-loopback address; needs TLS and a 32+ character token")
    parser.add_argument("--tls-cert", help="http only: PEM certificate file for HTTPS")
    parser.add_argument("--tls-key", help="http only: PEM private key file for HTTPS")
    args = parser.parse_args(argv)

    # stdout is the protocol channel under stdio; every log line goes to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.transport == "stdio":
        if args.allow_remote or args.tls_cert or args.tls_key or args.host != "127.0.0.1":
            parser.error("--host, --allow-remote and --tls-* apply to the http transport only")
        from tradingagents.worker import prewarm

        prewarm()                                # the first call starts on a warm worker
        create_server().run(transport="stdio", show_banner=False)
        return
    token = os.environ.get(TOKEN_ENV, "").strip()
    problems = http_exposure_problems(args.host, token, allow_remote=args.allow_remote,
                                      tls_cert=args.tls_cert, tls_key=args.tls_key)
    if problems:
        parser.error("; ".join(problems))
    uvicorn_config = ({"ssl_certfile": args.tls_cert, "ssl_keyfile": args.tls_key}
                      if args.tls_cert else None)
    create_server(auth=api_key_verifier(token)).run(
        transport="http", host=args.host, port=args.port, show_banner=False,
        uvicorn_config=uvicorn_config)


if __name__ == "__main__":
    main()
