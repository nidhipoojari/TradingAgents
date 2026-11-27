"""Per-run analysis records, keyed by run ID: ``<results_dir>/runs/<run_id>.json`` (Phase 3, F2).

``token_usage.json`` and the state log sit under ``<ticker>/<date>`` and are replaced by the next run of the
same ticker and date, so they cannot answer "what did run X say?". The shared service therefore also writes
one record per run when it ends, from every interface (CLI, dashboard, MCP) and with every outcome:
completed, failed, deadline_exceeded, budget_exceeded or cancelled. A run stopped by terminating its worker is
recorded by the parent process. The MCP read tools (get_report, get_run_metrics, list_recent_runs,
propose_trade) read these records, so a run can be retrieved after its worker process has exited.

A record holds the run's identity (ticker, date, source, analysts, rating, status), its reports and
debates (completed runs), and its usage as already written to token_usage.json (E1 events, E2 counters,
E3 tokens and estimated cost, budget, deadline, timing). Error text is the service's sanitized error. No
credential, password or token is ever stored here. Writes are atomic (temporary file, then rename), and
saving never raises: a record that cannot be written must not hide the run's own outcome.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RUNS_DIRNAME = "runs"
# 1: the first records (Phase 3 F2). 2: adds started_at. A version-1 record is read unchanged; a missing field
# reads as absent (readers use .get).
SCHEMA_VERSION = 2
# service.new_run_id() gives 20261125-091530-1a2b3c4d; callers may pass their own. No dots, no slashes:
# a run ID is a file name and can never name a path outside the runs folder.
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MAX_TEXT_CHARS = 200_000          # per stored text field; reports are normally 2-20k characters
MAX_SCAN_FILES = 2_000            # list_recent_runs reads at most this many newest records

REPORT_KEYS = {"market": "market_report", "sentiment": "sentiment_report", "news": "news_report",
               "fundamentals": "fundamentals_report"}
DEBATE_KEYS = {"bull": "bull_history", "bear": "bear_history", "judge": "judge_decision"}
RISK_KEYS = {"aggressive": "aggressive_history", "conservative": "conservative_history",
             "neutral": "neutral_history", "judge": "judge_decision"}


def is_valid_run_id(run_id: Any) -> bool:
    return isinstance(run_id, str) and RUN_ID_PATTERN.fullmatch(run_id) is not None


def runs_dir(results_dir: str | Path) -> Path:
    return Path(results_dir) / RUNS_DIRNAME


def _text(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + f"\n\n[... stored truncated: {len(text) - MAX_TEXT_CHARS:,} more characters]"
    return text


def reports_from_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """The run's written output, from its final graph state, as plain text."""
    debate = state.get("investment_debate_state") or {}
    risk = state.get("risk_debate_state") or {}
    sections = {
        "analyst_reports": {name: _text(state.get(key)) for name, key in REPORT_KEYS.items()},
        "investment_debate": {name: _text(debate.get(key)) for name, key in DEBATE_KEYS.items()},
        "investment_plan": _text(state.get("investment_plan")),
        "trader_plan": _text(state.get("trader_investment_plan")),
        "risk_debate": {name: _text(risk.get(key)) for name, key in RISK_KEYS.items()},
        "final_decision": _text(state.get("final_trade_decision")),
    }
    for name in ("analyst_reports", "investment_debate", "risk_debate"):
        sections[name] = {k: v for k, v in sections[name].items() if v}
    return sections


def build_record(*, run_id: str, source: str, status: str, ticker: str, trade_date: str, asset_type: str | None,
                 analysts, usage: Mapping[str, Any] | None, rating: str | None = None,
                 needs_review: bool | None = None, final_state: Mapping[str, Any] | None = None,
                 e1_events: list[str] | None = None, e3_summary_path: str | Path | None = None,
                 error: str | None = None, error_category: str | None = None,
                 started_at: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "source": source,
        "status": status,
        "started_at": started_at,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ticker": ticker,
        "trade_date": trade_date,
        "asset_type": asset_type,
        "analysts": list(analysts or ()),
        "rating": rating,
        "needs_review": needs_review,
        "error": error,
        "error_category": error_category,
        "e1_events": list(e1_events or []),
        # Absolute: E3 saves under a folder relative to the writer's working directory by default, and a reader
        # (the MCP server, another process) may run from elsewhere.
        "e3_summary_path": str(Path(e3_summary_path).absolute()) if e3_summary_path else None,
        "usage": dict(usage or {}),
    }
    if final_state:
        record["reports"] = reports_from_state(final_state)
    return record


def save(results_dir: str | Path, record: Mapping[str, Any]) -> Path | None:
    """Write the record atomically; the path, or None (logged) when it could not be written. Never raises."""
    run_id = record.get("run_id")
    try:
        if not is_valid_run_id(run_id):
            logger.warning("run record not saved: %r is not a storable run ID", run_id)
            return None
        folder = runs_dir(results_dir)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{run_id}.json"
        handle, temporary = tempfile.mkstemp(dir=folder, prefix=f".{run_id}.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(record, out, indent=1, default=str)
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        return target
    except Exception:  # noqa: BLE001 - the run's own outcome is what the caller must see
        logger.warning("could not save the run record for %s", run_id, exc_info=True)
        return None


def load(results_dir: str | Path, run_id: str) -> dict[str, Any] | None:
    """The record of ``run_id``, or None when there is none. ValueError for a malformed run ID."""
    if not is_valid_run_id(run_id):
        raise ValueError(f"{run_id!r} is not a run ID (letters, digits, '-' and '_', up to 64 characters)")
    path = runs_dir(results_dir) / f"{run_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"the record of run {run_id} could not be read ({type(exc).__name__})") from None
    return record if isinstance(record, dict) and record.get("run_id") == run_id else None


def recent(results_dir: str | Path, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` records, newest first (by file time); unreadable files are skipped."""
    folder = runs_dir(results_dir)
    try:
        paths = [p for p in folder.glob("*.json") if is_valid_run_id(p.stem)]
    except OSError:
        return []
    paths.sort(key=lambda p: _mtime(p), reverse=True)
    out = []
    for path in paths[:MAX_SCAN_FILES]:
        if len(out) >= limit:
            break
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("run_id") == path.stem:
            out.append(record)
    return out


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
