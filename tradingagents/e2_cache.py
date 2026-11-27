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

Vendor provenance (Phase 3): every key carries the vendor chain the request was
configured to use, so an answer is only ever served to a run configured for the
same vendors. Phase 2 rows have no vendor in their key and are never matched;
``python -m tradingagents.e2_cache adopt-legacy`` re-keys them only when the
runs' own records account for every row (see adopt_legacy), never on trust.

Turn it on in .env (off = old behavior):
    TRADINGAGENTS_E2_CACHE=true
The file lives at <data_cache_dir>/e2_cache.sqlite. Delete it to clear the cache.

Freshness (Phase 3; both off by default, so Phase 2 behaviour is unchanged):
  - TRADINGAGENTS_E2_MAX_AGE_DAYS=<n>: an answer saved more than n days ago is fetched again and replaced
    (for vendors that correct past data, e.g. restated fundamentals or adjusted prices).
  - TRADINGAGENTS_E2_REFRESH=true: every cacheable request is fetched again and the saved answer replaced.
  - ``python -m tradingagents.e2_cache info`` / ``invalidate --ticker NVDA [--tool ...] [--saved-before ...]``
    to inspect or remove saved answers (``--dry-run`` shows what would go).
Every row keeps when it was saved (saved_at) and, in its key, the vendor chain that produced it.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from tradingagents.dataflows.config import get_config

# Counters so we can report the cache hit rate (reset with reset_stats()).
STATS = {"hits": 0, "misses": 0, "skipped": 0}
# Phase 3 freshness counters, kept apart so the Phase 2 counters keep their shape. An expired or refreshed
# lookup is also a miss (the data is fetched again).
FRESHNESS = {"expired": 0, "refreshed": 0}
# Tool calls from one model turn run in parallel threads; keep the counts exact.
_STATS_LOCK = threading.Lock()


def _count(key: str) -> None:
    with _STATS_LOCK:
        if key in FRESHNESS:
            FRESHNESS[key] += 1
        else:
            STATS[key] += 1

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_ERROR_PREFIXES = ("DATA_UNAVAILABLE", "NO_DATA_AVAILABLE", "Error")


def is_enabled() -> bool:
    return bool(get_config().get("e2_cache_enabled"))


def max_age_days() -> float | None:
    """The configured maximum age of a saved answer in days (None = no expiry, as in Phase 2)."""
    raw = get_config().get("e2_cache_max_age_days")
    if raw in (None, "", "off", "none"):
        return None
    value = float(raw)
    if value < 0:
        raise ValueError(f"e2_cache_max_age_days must be >= 0, got {raw!r}")
    return value


def _refresh() -> bool:
    return bool(get_config().get("e2_cache_refresh"))


def _age_days(saved_at: str | None) -> float | None:
    try:
        return (datetime.now() - datetime.fromisoformat(saved_at)).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


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


def _chain(value) -> str:
    return ",".join(v.strip() for v in str(value).split(",") if v.strip()) or "default"


def vendor_chain(tool: str, data_vendors: dict | None = None, tool_vendors: dict | None = None) -> str:
    """The vendor chain a request for ``tool`` is routed to, as the router resolves it.

    Tool-level configuration wins over the category's. With no mappings given,
    the run's current configuration is used.
    """
    from tradingagents.dataflows.router import get_category_for_method  # router imports this module

    if data_vendors is None and tool_vendors is None:
        config = get_config()
        data_vendors, tool_vendors = config.get("data_vendors") or {}, config.get("tool_vendors") or {}
    if tool in (tool_vendors or {}):
        return _chain(tool_vendors[tool])
    try:
        category = get_category_for_method(tool)
    except ValueError:
        return "default"
    return _chain((data_vendors or {}).get(category, "default"))


def _request_key(args: tuple, kwargs: dict, vendor: str) -> str:
    """The request as text, with its vendor chain, e.g.
    '[["NVDA", "2026-07-23", "2026-10-23"], {}, {"vendors": "yfinance"}]'.

    Phase 3: the vendor chain is always part of the key, so a cached answer is only
    served to a run configured for the same vendors. A Phase 2 key (no vendor) can
    never equal one of these; see adopt_legacy().
    """
    return json.dumps([list(args), kwargs, {"vendors": vendor}], default=str, sort_keys=True)


def _legacy_request_key(args: tuple, kwargs: dict) -> str:
    """The Phase 2 key: the request only, with no vendor."""
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
    """Return the saved answer for this request and vendor chain, or None (= go download it)."""
    request = _request_key(args, kwargs, vendor_chain(tool))
    if not _is_past_request(request):
        _count("skipped")
        return None
    limit = max_age_days()                         # read first, so a bad setting fails on the first request
    if _refresh():
        _count("misses")
        _count("refreshed")
        return None
    with closing(_connect()) as db:
        row = db.execute(
            "SELECT result, saved_at FROM cache WHERE tool = ? AND request = ?", (tool, request)
        ).fetchone()
    if row is None:
        _count("misses")
        return None
    if limit is not None:
        age = _age_days(row[1])
        if age is None or age > limit:             # an unknown age cannot be shown to be fresh
            _count("misses")
            _count("expired")
            return None
    _count("hits")
    return row[0]


def save(tool: str, args: tuple, kwargs: dict, result) -> None:
    """Save a successful answer for a past-dated request, keyed by its vendor chain."""
    if not isinstance(result, str) or not result.strip() or result.startswith(_ERROR_PREFIXES):
        return
    request = _request_key(args, kwargs, vendor_chain(tool))
    if not _is_past_request(request):
        return
    with closing(_connect()) as db, db:
        db.execute(
            "INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?)",
            (tool, request, result, datetime.now().isoformat(timespec="seconds")),
        )


def reset_stats() -> None:
    with _STATS_LOCK:
        for key in STATS:
            STATS[key] = 0
        for key in FRESHNESS:
            FRESHNESS[key] = 0


def stats_snapshot() -> dict:
    """This process's counts plus the hit rate over cacheable lookups (None if none). The freshness
    counters appear only once one is non-zero, so a run without expiry or refresh keeps Phase 2's shape."""
    with _STATS_LOCK:
        counts = dict(STATS)
        counts.update({k: v for k, v in FRESHNESS.items() if v})
    looked_up = counts["hits"] + counts["misses"]
    counts["hit_rate_pct"] = round(100 * counts["hits"] / looked_up, 1) if looked_up else None
    return counts


def row_count(cache_dir) -> int | None:
    """How many answers the cache under ``cache_dir`` holds (read-only), or None without a cache file."""
    if not cache_dir:
        return None
    path = Path(cache_dir) / "e2_cache.sqlite"
    if not path.is_file():
        return 0
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            return db.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    except sqlite3.Error:
        return None


# ---------------------------------------------------------------- legacy (Phase 2) rows
def _vendor_settings(results_dir: Path) -> list[tuple[str, dict, dict]]:
    """(state log, data_vendors, tool_vendors) for every run recorded under ``results_dir``."""
    found = []
    for log in sorted(Path(results_dir).rglob("full_states_log_*.json")):
        try:
            settings = json.loads(log.read_text(encoding="utf-8")).get("run_settings") or {}
        except (OSError, ValueError):
            continue
        found.append((str(log), settings.get("data_vendors"), settings.get("tool_vendors") or {}))
    return found


def _unlogged_runs(results_dir: Path) -> list[str]:
    """Run folders (``<ticker>/<date>`` with a message log or token usage) that left no state log."""
    missing = []
    for marker in sorted({*Path(results_dir).rglob("message_tool.log"), *Path(results_dir).rglob("token_usage.json")}):
        run_dir = marker.parent
        log = run_dir.parent / "TradingAgentsStrategy_logs" / f"full_states_log_{run_dir.name}.json"
        if not log.exists() and str(run_dir) not in missing:
            missing.append(str(run_dir))
    return missing


def adopt_legacy(cache_dir: str | Path, results_dir: str | Path, *, only_these_runs: bool = False,
                 dry_run: bool = False) -> dict:
    """Re-key Phase 2 rows (no vendor) only when the runs' records account for them.

    Phase 2 rows do not say which vendor wrote them, so by default they are never
    served. A row is adopted only when all of these hold, and then only under the
    vendors those runs used:
    - the runs under ``results_dir`` left state logs, and every one records the
      same vendor configuration;
    - no run folder there shows activity without a state log (a run whose vendors
      are unknown may have written rows);
    - no legacy row was saved after the newest state log was written;
    - the operator states ``only_these_runs``: that no other run used this cache.
      The files can show consistency, not prove that nothing else wrote to it.
    The legacy rows are left in place (they are never matched). Returns a report.
    """
    from datetime import datetime as _dt

    runs = _vendor_settings(Path(results_dir))
    report = {"state_logs": len(runs), "adopted": 0, "already_present": 0, "legacy_rows": 0,
              "verified": False, "reason": ""}
    if not runs:
        report["reason"] = f"no state logs under {results_dir}: provenance cannot be verified"
        return report
    unsettled = [log for log, data, _ in runs if not isinstance(data, dict)]
    if unsettled:
        report["reason"] = f"{len(unsettled)} state log(s) record no vendor settings, e.g. {unsettled[0]}"
        return report
    configs = {json.dumps([data, tools], sort_keys=True) for _, data, tools in runs}
    if len(configs) > 1:
        report["reason"] = f"the runs used {len(configs)} different vendor configurations"
        return report
    unlogged = _unlogged_runs(Path(results_dir))
    if unlogged:
        report["reason"] = (f"{len(unlogged)} run(s) used the results folder without leaving a state log, so "
                            f"their vendors are unknown: {', '.join(unlogged[:3])}")
        return report

    path = Path(cache_dir) / "e2_cache.sqlite"
    if not path.exists():
        report["reason"] = f"no cache at {path}"
        return report
    newest_log = max(Path(log).stat().st_mtime for log, _, _ in runs) + 60   # a minute's grace
    with closing(sqlite3.connect(path, timeout=30)) as db:
        rows = db.execute("SELECT tool, request, result, saved_at FROM cache").fetchall()
    legacy = []
    for tool, request, result, saved_at in rows:
        try:
            parts = json.loads(request)
        except ValueError:
            continue
        if isinstance(parts, list) and len(parts) == 2:
            legacy.append((tool, parts, result, saved_at))
    report["legacy_rows"] = len(legacy)
    late = []
    for _, _, _, saved_at in legacy:
        try:
            if _dt.fromisoformat(saved_at).timestamp() > newest_log:
                late.append(saved_at)
        except (TypeError, ValueError):
            late.append(str(saved_at))
    if late:
        report["reason"] = f"{len(late)} legacy row(s) were saved after the last recorded run (e.g. {late[0]})"
        return report
    if not only_these_runs:
        report["reason"] = ("the records are consistent; confirm that no other run used this cache "
                            "(--only-these-runs-used-this-cache) to adopt them")
        return report

    data_vendors, tool_vendors = runs[0][1], runs[0][2]
    report.update(verified=True, data_vendors=data_vendors, tool_vendors=tool_vendors)
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        for tool, parts, result, saved_at in legacy:
            key = _request_key(tuple(parts[0]), parts[1], vendor_chain(tool, data_vendors, tool_vendors))
            if db.execute("SELECT 1 FROM cache WHERE tool = ? AND request = ?", (tool, key)).fetchone():
                report["already_present"] += 1
                continue
            if not dry_run:
                db.execute("INSERT INTO cache VALUES (?, ?, ?, ?)", (tool, key, result, saved_at))
            report["adopted"] += 1
    return report


# ---------------------------------------------------------------- inspection and invalidation
def _rows(db: sqlite3.Connection):
    for tool, request, saved_at in db.execute("SELECT tool, request, saved_at FROM cache"):
        try:
            parts = json.loads(request)
            args = parts[0] if isinstance(parts, list) and parts else []
            vendors = parts[2].get("vendors") if isinstance(parts, list) and len(parts) > 2 else None
        except (ValueError, AttributeError, TypeError):
            args, vendors = [], None
        ticker = str(args[0]).upper() if args else None
        yield tool, request, saved_at, ticker, vendors


def info(cache_dir) -> dict:
    """What the cache holds: rows per tool and ticker, the oldest and newest save, rows without a vendor."""
    path = Path(cache_dir) / "e2_cache.sqlite"
    if not path.is_file():
        return {"path": str(path), "rows": 0}
    by_tool: dict[str, int] = {}
    by_ticker: dict[str, int] = {}
    saved, legacy = [], 0
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        for tool, _request, saved_at, ticker, vendors in _rows(db):
            by_tool[tool] = by_tool.get(tool, 0) + 1
            if ticker:
                by_ticker[ticker] = by_ticker.get(ticker, 0) + 1
            if saved_at:
                saved.append(saved_at)
            legacy += vendors is None
    return {"path": str(path), "rows": sum(by_tool.values()), "by_tool": by_tool, "by_ticker": by_ticker,
            "oldest_saved_at": min(saved) if saved else None, "newest_saved_at": max(saved) if saved else None,
            "rows_without_vendor": legacy}


def invalidate(cache_dir, *, tool: str | None = None, ticker: str | None = None,
               saved_before: str | None = None, dry_run: bool = False) -> dict:
    """Remove saved answers matching every given filter (at least one is required), e.g. after a vendor
    corrected a ticker's history. ``saved_before``: an ISO date or time; rows saved earlier match."""
    if not (tool or ticker or saved_before):
        raise ValueError("name at least one of tool, ticker or saved_before (delete the file to clear everything)")
    if saved_before:
        datetime.fromisoformat(saved_before)        # reject a malformed date before touching anything
    path = Path(cache_dir) / "e2_cache.sqlite"
    if not path.is_file():
        return {"matched": 0, "deleted": 0, "dry_run": dry_run}
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        matched = [(t, request) for t, request, saved_at, row_ticker, _v in _rows(db)
                   if (tool is None or t == tool) and (ticker is None or row_ticker == ticker.upper())
                   and (saved_before is None or (saved_at or "") < saved_before)]
        if not dry_run:
            db.executemany("DELETE FROM cache WHERE tool = ? AND request = ?", matched)
    return {"matched": len(matched), "deleted": 0 if dry_run else len(matched), "dry_run": dry_run}


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m tradingagents.e2_cache",
                                     description="E2 cache maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    adopt = sub.add_parser("adopt-legacy", help="re-key Phase 2 rows whose vendors the runs' state logs prove")
    adopt.add_argument("--cache-dir", required=True, help="folder holding e2_cache.sqlite")
    adopt.add_argument("--results-dir", required=True, help="results folder of the runs that used this cache")
    adopt.add_argument("--only-these-runs-used-this-cache", action="store_true",
                       help="you confirm no run outside --results-dir used this cache")
    adopt.add_argument("--dry-run", action="store_true")
    show = sub.add_parser("info", help="what the cache holds")
    show.add_argument("--cache-dir", required=True)
    drop = sub.add_parser("invalidate", help="remove saved answers (e.g. after a vendor corrected past data)")
    drop.add_argument("--cache-dir", required=True)
    drop.add_argument("--tool")
    drop.add_argument("--ticker")
    drop.add_argument("--saved-before", help="ISO date/time: remove rows saved before it")
    drop.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "info":
        print(json.dumps(info(args.cache_dir), indent=2))
        return 0
    if args.command == "invalidate":
        print(json.dumps(invalidate(args.cache_dir, tool=args.tool, ticker=args.ticker,
                                    saved_before=args.saved_before, dry_run=args.dry_run), indent=2))
        return 0
    report = adopt_legacy(args.cache_dir, args.results_dir, dry_run=args.dry_run,
                          only_these_runs=args.only_these_runs_used_this_cache)
    print(json.dumps(report, indent=2))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
