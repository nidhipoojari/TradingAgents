"""F2: the four read-only MCP tools that answer from saved runs (Phase 3).

``get_report``, ``get_run_metrics``, ``list_recent_runs`` and ``propose_trade`` read the per-run records the
shared service writes (``run_store``: ``<results_dir>/runs/<run_id>.json``), so they work after the analysis
worker has exited and across server restarts. None of them starts an analysis, calls a model, fetches market
data or changes a file (the server records each call in its MCP request log, ``mcp_audit``; that is all). ``propose_trade`` applies F1's proposal rules (``trade_rules``) and never reaches the
execution layer: this module imports no broker, identity, approval or dashboard code (a test pins that), so it
cannot approve, sign, submit, cancel or execute an order.

Which runs are visible: by default every source (cli, dashboard, mcp, api) in the server's results folder,
because they are all analysis output. ``TRADINGAGENTS_MCP_RUN_SOURCES`` (comma-separated) narrows it, e.g.
``mcp`` to show MCP clients only their own runs. A run from another source then reads as not found.
Never returned: approval and order logs (``approval_decisions.jsonl``, ``paper_orders.jsonl``), user names,
file paths, credentials, or raw error stack traces.

Every function returns plain JSON-able data or raises ``ToolInputError`` (the client's arguments),
``RunNotFound`` or ``ReportUnavailable``. ``mcp_server`` turns these into MCP tool errors.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from tradingagents import e1_routing, run_store, service, trade_rules

SOURCES_ENV = "TRADINGAGENTS_MCP_RUN_SOURCES"
REPORT_CHARS_ENV = "TRADINGAGENTS_MCP_MAX_REPORT_CHARS"     # shared with analyze_stock
DEFAULT_REPORT_CHARS = 4000
MAX_REPORT_ANSWER_CHARS = 60_000      # one get_report answer, all sections together
DEFAULT_RECENT = 10
MAX_RECENT = 50
MAX_EVENT_LINES = 50

SECTIONS = {
    # name -> (group in the record's "reports", key inside the group or None for a top-level text)
    "market": ("analyst_reports", "market"),
    "sentiment": ("analyst_reports", "sentiment"),
    "news": ("analyst_reports", "news"),
    "fundamentals": ("analyst_reports", "fundamentals"),
    "investment_debate": ("investment_debate", None),
    "investment_plan": ("investment_plan", None),
    "trader_plan": ("trader_plan", None),
    "risk_debate": ("risk_debate", None),
    "final_decision": ("final_decision", None),
}
BILLING_NOTE = ("Estimated from the tokens each call reported x the prices in E3_PRICING_JSON; this is not "
                "the provider's bill. Actual charges are only in the provider's billing export "
                "(compare with scripts/reconcile_billing.py).")
NOT_AN_ORDER = ("This is a proposal, not an order. This tool never approves, signs, submits, cancels or "
                "executes an order; a signed-in person decides in the F1 dashboard.")


class ToolInputError(ValueError):
    """The client's arguments are malformed."""


class RunNotFound(LookupError):
    """No visible saved run has this ID."""


class ReportUnavailable(LookupError):
    """The run exists, but the requested output was not produced or not saved."""


class ServerSetting(ValueError):
    """A server environment setting is malformed."""


# ------------------------------------------------------------------ access to the records
def results_dir() -> Path:
    return Path(service.config_for_source("mcp")["results_dir"])


def visible_sources() -> frozenset[str]:
    raw = (os.environ.get(SOURCES_ENV) or "").strip()
    if not raw:
        return frozenset(service.SOURCES)
    chosen = frozenset(s.strip().lower() for s in raw.split(",") if s.strip())
    unknown = chosen - frozenset(service.SOURCES)
    if unknown or not chosen:
        raise ServerSetting(f"{SOURCES_ENV}={raw!r}: use a comma-separated subset of "
                            f"{', '.join(sorted(service.SOURCES))}")
    return chosen


def _run_id(run_id: Any) -> str:
    if not isinstance(run_id, str) or not run_store.is_valid_run_id(run_id.strip()):
        raise ToolInputError("run_id must be a run ID such as 20261125-091530-1a2b3c4d "
                             "(letters, digits, '-' and '_', up to 64 characters)")
    return run_id.strip()


def load_run(run_id: Any) -> dict[str, Any]:
    rid = _run_id(run_id)
    try:
        record = run_store.load(results_dir(), rid)
    except ValueError as exc:
        raise ReportUnavailable(str(exc)) from None
    if record is None or record.get("source") not in visible_sources():
        raise RunNotFound(f"no saved run {rid} on this server (list_recent_runs shows the runs it has; runs "
                          "made before per-run records existed, or in another results folder, are not visible)")
    return record


def _limit_chars() -> int:
    try:
        value = int(float(os.environ.get(REPORT_CHARS_ENV) or DEFAULT_REPORT_CHARS))
    except ValueError:
        return DEFAULT_REPORT_CHARS
    return value if value > 0 else DEFAULT_REPORT_CHARS


def _truncate(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n[... truncated, {len(text) - limit:,} more characters in the saved run]"


def _identity(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in ("run_id", "source", "status", "ticker", "trade_date", "asset_type",
                                             "analysts", "rating", "needs_review", "recorded_at")}


def _cost(record: dict[str, Any]) -> dict[str, Any]:
    usage = record.get("usage") or {}
    cost = usage.get("e3_cost")
    if not cost:
        cost = service.e3_cost(_e3_summary(record))
        cost = {"estimated_cost_usd": cost["estimated_cost_usd"], "basis": cost["basis"],
                "partial_cost_usd": cost["partial_cost_usd"], "note": cost["note"]}
    return {**cost, "kind": "estimate", "actual_billing": None, "billing_note": BILLING_NOTE}


def _e3_summary(record: dict[str, Any]) -> dict[str, Any] | None:
    path = record.get("e3_summary_path")
    if not path:
        return None
    try:
        summary = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return summary if isinstance(summary, dict) and summary.get("run_id") in (None, record.get("run_id")) else None


# ------------------------------------------------------------------ get_report
def get_report(run_id: Any, section: Any = None) -> dict[str, Any]:
    if section is not None and section not in SECTIONS:
        raise ToolInputError(f"section must be one of {', '.join(SECTIONS)}, or omitted for all")
    record = load_run(run_id)
    reports = record.get("reports")
    if not reports:
        why = (f"run {record['run_id']} ended as {record.get('status')}"
               + (f" [{record['error_category']}]" if record.get("error_category") else "")
               + "; only completed runs save their reports (get_run_metrics shows what it recorded)")
        raise ReportUnavailable(why)
    limit = _limit_chars()
    wanted = [section] if section else list(SECTIONS)
    out: dict[str, Any] = {}
    missing = []
    used = 0
    for name in wanted:
        group, key = SECTIONS[name]
        value = reports.get(group)
        if key is not None:
            value = (value or {}).get(key)
        if not value:
            missing.append(name)
            continue
        if isinstance(value, dict):
            value = {k: _truncate(v, limit) for k, v in value.items() if v}
            size = sum(len(v) for v in value.values())
        else:
            value = _truncate(value, limit)
            size = len(value)
        if used + size > MAX_REPORT_ANSWER_CHARS:
            out[name] = f"[omitted to keep this answer under {MAX_REPORT_ANSWER_CHARS:,} characters; " \
                        f"ask with section=\"{name}\"]"
            continue
        out[name] = value
        used += size
    if section and missing:
        analysts = ", ".join(record.get("analysts") or []) or "none"
        raise ReportUnavailable(f"run {record['run_id']} has no {section} output (analysts run: {analysts})")
    return {**_identity(record), "sections": out, "not_produced": missing,
            "note": "Saved output of an earlier analysis; nothing was re-run. Research output, not financial advice."}


# ------------------------------------------------------------------ get_run_metrics
_USAGE_KEYS = ("llm_calls", "llm_calls_succeeded", "llm_calls_failed", "llm_calls_in_flight", "tool_calls",
               "tokens_in", "tokens_out", "tokens_total", "tokens_note", "sdk_retries_observed",
               "llm_provider", "quick_think_llm", "deep_think_llm")
_BUDGET_KEYS = ("max_llm_calls", "max_cost_usd", "llm_calls_started", "reported_usage_cost_usd",
                "unreported_usage_estimate_usd", "budget_cost_usd", "exhausted", "reason",
                # How the conservative figure was reached: failed calls and calls without usage are estimated,
                # never given provider token counts they did not report.
                "failed_calls", "failed_calls_with_usage", "calls_without_usage", "unpriced_calls", "basis")
E2_OFF = {"enabled": False, "note": "E2 was off for this run, so no cache counters were recorded"}


def get_run_metrics(run_id: Any) -> dict[str, Any]:
    record = load_run(run_id)
    usage = record.get("usage") or {}
    summary = _e3_summary(record)
    events = list(record.get("e1_events") or usage.get("e1_fallback_events") or [])
    timing = {"elapsed_seconds": usage.get("elapsed_seconds")}
    if usage.get("run_deadline"):
        timing["run_deadline"] = usage["run_deadline"]
    e3_timing = usage.get("timing") or (summary or {}).get("timing")
    if e3_timing:
        timing["e3_timing"] = e3_timing
    per_agent = usage.get("per_agent") or (summary or {}).get("per_agent")
    answer: dict[str, Any] = {
        **_identity(record),
        "timing": timing,
        "e1_routing": {
            "summary": e1_routing.summarize_events(events),
            "events": [service.sanitize_error(line, 300) for line in events[-MAX_EVENT_LINES:]],
            "events_truncated": max(0, len(events) - MAX_EVENT_LINES),
            "breaker": usage.get("e1_breaker"),
            "adaptive": usage.get("e1_adaptive"),
        },
        # The service records e2_cache only when E2 is on: its absence means off, not "no data".
        "e2_cache": usage.get("e2_cache") or E2_OFF,
        "e3_usage": {key: usage.get(key) for key in _USAGE_KEYS if usage.get(key) is not None},
        "per_agent": per_agent,
        "cost": _cost(record),
        "errors": {"status": record.get("status"), "error_category": record.get("error_category"),
                   "error": record.get("error")},
        "note": "Recorded when the run ended; nothing was re-run and no model or market data was requested.",
    }
    if usage.get("run_budget"):
        answer["run_budget"] = {k: usage["run_budget"].get(k) for k in _BUDGET_KEYS}
    if usage.get("worker_checkpoint"):
        answer["worker_checkpoint"] = usage["worker_checkpoint"]
    return answer


# ------------------------------------------------------------------ list_recent_runs
def list_recent_runs(limit: Any = DEFAULT_RECENT) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ToolInputError(f"limit must be a whole number from 1 to {MAX_RECENT}")
    applied = min(limit, MAX_RECENT)
    sources = visible_sources()
    rows = []
    for record in run_store.recent(results_dir(), run_store.MAX_SCAN_FILES):
        if record.get("source") not in sources:
            continue
        usage = record.get("usage") or {}
        cost = usage.get("e3_cost") or {}
        rows.append({**_identity(record), "elapsed_seconds": usage.get("elapsed_seconds"),
                     "llm_calls": usage.get("llm_calls"), "tokens_total": usage.get("tokens_total"),
                     "estimated_cost_usd": cost.get("estimated_cost_usd"),
                     "error_category": record.get("error_category"),
                     "has_reports": bool(record.get("reports"))})
        if len(rows) >= applied:
            break
    return {"runs": rows, "limit_requested": limit, "limit_applied": applied, "max_limit": MAX_RECENT,
            "visible_sources": sorted(sources),
            "note": "Newest first. Costs are E3 estimates, not billing. Use get_report / get_run_metrics for one run."}


# ------------------------------------------------------------------ propose_trade
def propose_trade(run_id: Any, *, today: str | None = None) -> dict[str, Any]:
    record = load_run(run_id)
    try:
        limits = trade_rules.OrderLimits.from_env()
    except ValueError as exc:
        raise ServerSetting(str(exc)) from None
    ticker, rating = record.get("ticker") or "", record.get("rating")
    asset_type = record.get("asset_type") or "stock"
    base = {**_identity(record), "read_only": True, "note": NOT_AN_ORDER}
    if record.get("status") != "completed" or not rating:
        return {**base, "proposal": {"status": "no_trade", "side": None,
                                     "reason": f"run {record['run_id']} ended as {record.get('status')}; "
                                               "only a completed analysis can propose a trade"}}
    # F1's own rule. This server cannot see the paper account, so it proposes with no position held.
    proposal = trade_rules.propose_order(ticker, rating, 0.0, asset_type)
    if proposal.allowed:
        status, side, reason = "buy_allowed", proposal.side.value, proposal.reason
    elif rating in trade_rules.SELL_RATINGS and trade_rules.is_paper_tradable(ticker, asset_type):
        status, side = "sell_only_if_long", trade_rules.Side.SELL.value
        reason = (f"{rating}: the F1 rules allow only reducing or closing an existing long position (no short "
                  "selling). This read-only server cannot see the paper account; the dashboard checks the "
                  "position before any sell.")
    else:
        status, side, reason = "no_trade", None, proposal.reason
    # The ID the F1 dashboard records this proposal under (proposal_store); this tool records nothing.
    answer = {**base, "proposal_id": trade_rules.proposal_id(record["run_id"], side),
              "proposal": {"status": status, "side": side, "reason": reason}}
    if side is not None:
        day = today or date.today().isoformat()
        try:
            order_id = trade_rules.client_order_id(record["run_id"], trade_rules.Side(side))
        except ValueError:          # a caller-chosen run ID too long for Alpaca: the dashboard refuses it too
            order_id = None
        answer["order_rules"] = {
            "order_type": "limit (market orders are disabled)",
            "time_in_force": "day, regular session only",
            "quantity": "whole shares, typed by the signed-in person; never sized from the analysis text",
            "max_order_notional_usd": limits.max_notional,
            "client_order_id": order_id,
            "one_order_per_run_and_side": True,
            "needs": ["a signed-in person in the F1 dashboard", "their password again (step-up)",
                      "an explicit approval tick", "the broker's final checks"],
        }
        if record.get("trade_date") and record["trade_date"] != day:
            answer["order_rules"]["stale_analysis"] = (f"the analysis is for {record['trade_date']}, not today "
                                                       f"({day}); the dashboard asks for confirmation")
    return answer
