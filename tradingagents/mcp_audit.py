"""F2: one audit line per MCP tool call, ``<results_dir>/mcp_requests.jsonl`` (Phase 3, MCP_REQUEST).

Every call to the five tools (``analyze_stock``, ``get_report``, ``get_run_metrics``, ``list_recent_runs``,
``propose_trade``) is recorded when it ends, whatever the outcome:

    request_id       mcp-<32 hex>, unique per call
    tool             the tool's name
    started_at       ISO-8601 UTC, milliseconds; finished_at likewise
    elapsed_seconds  on the monotonic clock (a wall-clock step does not change it)
    status           ok | error | timeout | cancelled
    error_category   None when ok; else invalid_request, invalid_server_setting, run_not_found,
                     report_unavailable, busy, timeout, cancelled, internal_error, or the failed
                     analysis's own category (service.error_category: e.g. rate_limit, deadline_exceeded)
    run_id           the analysis run the call started or read, when there is one (a logical reference to
                     ANALYSIS_RUN; a run that was not found is not named)
    arguments        a safe summary only: validated identifiers, short values and types, never free text;
                     arguments the tool refused (invalid_request, run_not_found) keep only their types

Never recorded: the bearer token or any header, the client's raw arguments, report text, error messages or
stack traces. Writing the line never raises and never changes a tool's answer or error; a line that cannot be
written is logged to stderr. The log grants nothing: no tool reads it, and it cannot approve or send an order.
Standard library plus ``audit_log`` and ``run_store``; it imports nothing from the execution layer.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradingagents import audit_log, run_store

logger = logging.getLogger(__name__)

LOG_NAME = "mcp_requests.jsonl"
SCHEMA_VERSION = 1
ID_PREFIX = "mcp"
TOOLS = ("analyze_stock", "get_report", "get_run_metrics", "list_recent_runs", "propose_trade")
STATUSES = ("ok", "error", "timeout", "cancelled")
_SAFE_TEXT = re.compile(r"[A-Za-z0-9._^=-]{1,32}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def new_request_id() -> str:
    """``mcp-<32 hex>``: 36 characters."""
    return audit_log.new_id(ID_PREFIX)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _short(value: Any) -> Any:
    """A value that is safe to keep: None, a bool, a number, or a short identifier-like string; else its type."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value if _SAFE_TEXT.fullmatch(value) else "<other text>"
    return f"<{type(value).__name__}>"


def safe_arguments(tool: str, arguments: dict[str, Any], *, validated: bool = True) -> dict[str, Any]:
    """What is kept of a call's arguments. Arguments the tool refused (``validated=False``) keep only their
    type: a refused value is exactly where a pasted secret or arbitrary text would be."""
    if not validated:
        return {name: None if value is None else f"<{type(value).__name__}>" for name, value in arguments.items()}
    out: dict[str, Any] = {}
    for name, value in arguments.items():
        if name == "run_id":
            out[name] = value if run_store.is_valid_run_id(value) else "<invalid>"
        elif name == "trade_date":
            out[name] = value if value is None or (isinstance(value, str) and _DATE.fullmatch(value)) else "<invalid>"
        elif name == "analysts":
            out[name] = ([_short(a) for a in value[:8]] if isinstance(value, list | tuple) else _short(value))
        else:
            out[name] = _short(value)
    return out


@dataclass
class Request:
    """One call: create it when the call starts, ``finish`` it once."""

    tool: str
    arguments: dict[str, Any]
    run_id: str | None = None
    results_dir: str | Path | None = None
    request_id: str = field(default_factory=new_request_id)
    started_at: str = field(default_factory=_now)
    started_mono: float = field(default_factory=time.monotonic)
    finished: bool = False

    def finish(self, status: str, error_category: str | None = None, *, run_id: str | None = None) -> dict | None:
        """Write the call's line; the entry, or None when it was already finished or could not be written."""
        if self.finished:
            return None
        self.finished = True
        try:
            rid = run_id if run_id is not None else self.run_id
            entry = {
                "schema": SCHEMA_VERSION,
                "request_id": self.request_id,
                "tool": self.tool,
                "started_at": self.started_at,
                "finished_at": _now(),
                "elapsed_seconds": round(time.monotonic() - self.started_mono, 3),
                "status": status if status in STATUSES else "error",
                "error_category": None if status == "ok" else (error_category or "internal_error"),
                "run_id": rid if run_store.is_valid_run_id(rid) else None,
                "arguments": safe_arguments(self.tool, self.arguments,
                                            validated=error_category not in ("invalid_request", "run_not_found")),
            }
            audit_log.append(log_path(self.results_dir), entry)
            return entry
        except Exception:  # noqa: BLE001 - auditing must never change a tool's answer
            logger.warning("could not record MCP request %s (%s)", self.request_id, self.tool, exc_info=True)
            return None


def log_path(results_dir: str | Path | None = None) -> Path:
    if results_dir is None:       # the folder the read tools answer from (the MCP run config's results_dir)
        from tradingagents import mcp_read_tools

        results_dir = mcp_read_tools.results_dir()
    return Path(results_dir) / LOG_NAME


def entries(results_dir: str | Path | None = None) -> list[dict[str, Any]]:
    return audit_log.read(log_path(results_dir))
