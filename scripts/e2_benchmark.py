"""E2 benchmark: how long the data tools take with and without the cache.

Calls ONLY the data tools (Yahoo / SEC), never Gemini, so it costs no LLM quota.
It asks for the same data the analysts ask for in one NVDA run, three times.
Each run is a fresh Python process (no leftover in-memory caches) and uses its
own temporary cache folder (your real cache is not touched):

  1. E2 off            -> the Phase 1 behaviour (baseline), empty folder
  2. E2 on, cold cache -> first run, fills the E2 cache, empty folder
  3. E2 on, warm cache -> second run, same folder as run 2

Usage (from the TradingAgents folder):
    python scripts/e2_benchmark.py --ticker NVDA --date 2026-10-23
Results are printed and saved to results/phase2/e2_benchmark.csv
"""
import argparse
import csv
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from tradingagents import e2_cache
from tradingagents.dataflows import router
from tradingagents.dataflows.config import get_config, set_config


def requests_for(ticker: str, day: str) -> list[tuple]:
    """The data requests one analysis makes (market, news, fundamentals analysts)."""
    d = datetime.strptime(day, "%Y-%m-%d")
    three_months_ago = (d - timedelta(days=90)).strftime("%Y-%m-%d")
    week_ago = (d - timedelta(days=7)).strftime("%Y-%m-%d")
    reqs = [("get_stock_data", ticker, three_months_ago, day)]
    for ind in ("close_50_sma", "close_200_sma", "rsi", "macd", "boll_lb", "atr"):
        reqs.append(("get_indicators", ticker, ind, day, 30))
    reqs += [
        ("get_fundamentals", ticker, day),
        ("get_balance_sheet", ticker, "quarterly", day),
        ("get_cashflow", ticker, "quarterly", day),
        ("get_income_statement", ticker, "quarterly", day),
        ("get_news", ticker, week_ago, day),
        ("get_global_news", day, 7, 10),
        ("get_insider_transactions", ticker, day),
    ]
    return reqs


def run_once(reqs: list[tuple], cache_on: bool, cache_dir: str) -> dict:
    """One run inside this process: ask for every request and time it."""
    set_config({**get_config(), "e2_cache_enabled": cache_on, "data_cache_dir": cache_dir})
    e2_cache.reset_stats()
    start = time.perf_counter()
    for method, *args in reqs:
        router.route_to_vendor(method, *args)
    seconds = time.perf_counter() - start
    s = dict(e2_cache.STATS)
    looked_up = s["hits"] + s["misses"]
    return {"requests": len(reqs), "seconds": round(seconds, 3), "cache_hits": s["hits"],
            "cache_misses": s["misses"],
            "hit_rate_pct": round(100 * s["hits"] / looked_up, 1) if looked_up else 0.0}


def run_in_fresh_process(label: str, ticker: str, day: str, cache_on: bool, cache_dir: str) -> dict:
    out = subprocess.run(
        [sys.executable, __file__, "--ticker", ticker, "--date", day,
         "--one", "on" if cache_on else "off", "--cache-dir", cache_dir],
        capture_output=True, text=True, check=True,
    )
    row = {"run": label, **json.loads(out.stdout.strip().splitlines()[-1])}
    print(f"{label:<22} {row['seconds']:>8.3f} s   hits {row['cache_hits']:>2} / "
          f"misses {row['cache_misses']:>2}   hit rate {row['hit_rate_pct']}%")
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ticker", default="NVDA")
    parser.add_argument("--date", default="2026-10-23", help="a PAST date, YYYY-MM-DD")
    parser.add_argument("--one", choices=["on", "off"], help=argparse.SUPPRESS)
    parser.add_argument("--cache-dir", help=argparse.SUPPRESS)
    args = parser.parse_args()
    reqs = requests_for(args.ticker, args.date)

    if args.one:  # child process: do one run, print its numbers as JSON
        print(json.dumps(run_once(reqs, args.one == "on", args.cache_dir)))
        return

    print(f"E2 benchmark: {args.ticker} {args.date}, {len(reqs)} data requests per run\n")
    with tempfile.TemporaryDirectory() as off_dir, tempfile.TemporaryDirectory() as on_dir:
        rows = [
            run_in_fresh_process("1. E2 off (Phase 1)", args.ticker, args.date, False, off_dir),
            run_in_fresh_process("2. E2 on, cold cache", args.ticker, args.date, True, on_dir),
            run_in_fresh_process("3. E2 on, warm cache", args.ticker, args.date, True, on_dir),
        ]
    out = Path("results/phase2/e2_benchmark.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
