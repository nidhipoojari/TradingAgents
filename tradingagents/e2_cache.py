"""E2: Market-data cache (SQLite).

The idea in one sentence: if an agent asks for the same data again
(same tool, same ticker, same dates), answer from a local SQLite file
instead of downloading it from Yahoo / SEC again.

What gets cached
  - Every data tool that goes through the router: prices, indicators,
    fundamentals, balance sheet, cash flow, income statement, news,
    global news, insider transactions, macro indicators.
  - ONLY requests whose dates are all in the past. Past data does not
    change, so it can be reused forever. Anything about "today" is always
    downloaded fresh.
  - NEVER error answers ("DATA_UNAVAILABLE", "NO_DATA_AVAILABLE", "Error").

No look-ahead bias: the cache key includes the request's dates, so a run
for 2026-10-23 can only ever get back what was fetched for 2026-10-23.

Turn it on in .env (off = old behavior):
    TRADINGAGENTS_E2_CACHE=true
The file lives at <data_cache_dir>/e2_cache.sqlite. Delete it to clear the cache.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from tradingagents.dataflows.config import get_config

# Counters so we can report the cache hit rate (reset with reset_stats()).
STATS = {"hits": 0, "misses": 0, "skipped": 0}

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ERROR_PREFIXES = ("DATA_UNAVAILABLE", "NO_DATA_AVAILABLE", "Error")


def is_enabled() -> bool:
    return bool(get_config().get("e2_cache_enabled"))


def _db_path() -> Path:
    return Path(get_config()["data_cache_dir"]) / "e2_cache.sqlite"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute(
        "CREATE TABLE IF NOT EXISTS cache ("
        " tool TEXT, request TEXT, result TEXT, saved_at TEXT,"
        " PRIMARY KEY (tool, request))"
    )
    return db


def _request_key(args: tuple, kwargs: dict) -> str:
    """The request as text, e.g. '["NVDA", "2026-07-23", "2026-10-23"]'."""
    return json.dumps([list(args), kwargs], default=str, sort_keys=True)


def _is_past_request(request: str) -> bool:
    """True when the request names at least one date and every date is before today."""
    dates = _DATE.findall(request)
    if not dates:
        return False
    today = date.today()
    try:
        return all(datetime.strptime(d, "%Y-%m-%d").date() < today for d in dates)
    except ValueError:
        return False


def lookup(tool: str, args: tuple, kwargs: dict) -> str | None:
    """Return the saved answer for this request, or None (= go download it)."""
    request = _request_key(args, kwargs)
    if not _is_past_request(request):
        STATS["skipped"] += 1
        return None
    with closing(_connect()) as db:
        row = db.execute(
            "SELECT result FROM cache WHERE tool = ? AND request = ?", (tool, request)
        ).fetchone()
    if row is None:
        STATS["misses"] += 1
        return None
    STATS["hits"] += 1
    return row[0]


def save(tool: str, args: tuple, kwargs: dict, result) -> None:
    """Save a successful answer for a past-dated request."""
    if not isinstance(result, str) or not result.strip() or result.startswith(_ERROR_PREFIXES):
        return
    request = _request_key(args, kwargs)
    if not _is_past_request(request):
        return
    with closing(_connect()) as db, db:
        db.execute(
            "INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?)",
            (tool, request, result, datetime.now().isoformat(timespec="seconds")),
        )


def reset_stats() -> None:
    for key in STATS:
        STATS[key] = 0
