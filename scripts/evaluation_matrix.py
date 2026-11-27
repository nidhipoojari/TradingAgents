#!/usr/bin/env python3
"""Reproducible offline evaluation matrix (Phase 3; Phase 2 limitation 7). No API keys, no network, no orders.

Every cell runs the real worker, service, E1 (with the circuit breaker), E2, E3, budget and deadline, with
scripted models that replay the per-call latencies of a saved real run of that ticker (eval/phase2):

    tickers   AAPL, MSFT, NVDA                       (profiles: eval/phase2/04_AAPL, 05_MSFT, 01_NVDA)
    dates     2026-10-23, 2026-01-09                 (any past date; E2 keys include it)
    analysts  market | all four (market, social, news, fundamentals)
    depth     shallow (1 debate + 1 risk round) | deeper (2 + 2)
    scenario  ok | primary 503 on its first 2 calls | every primary call 503 | primary and backup 503
    source    dashboard (F1) | mcp (F2): the shared service each interface calls, with its limits

A saved run has as many latencies as it made calls; extra calls (more analysts, deeper debate) use that
run's median call latency. ``--speed 0.1`` divides every delay by 10 so the whole matrix runs in minutes:
its outcomes, call counts, E1/E2/E3 figures and budget accounting are valid, its seconds are scaled. Use
``--speed 1`` (and ``--only``) for timing. The Streamlit page and the MCP protocol layer are exercised by
the integration tests (tests/test_phase3_integration.py, tests/test_mcp_server.py), not here.

    python scripts/evaluation_matrix.py [--speed 0.1] [--reps 1] [--only "NVDA|ok"] [--out results/eval_matrix_<stamp>]
"""

from __future__ import annotations

import argparse
import copy
import csv
import itertools
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SETUP = "tests.worker_fakes:replay"
PROFILE_RUNS = {"AAPL": "04_AAPL", "MSFT": "05_MSFT", "NVDA": "01_NVDA"}
ALL_FOUR = ("market", "social", "news", "fundamentals")
FAIL_SECONDS = 2.816          # the live incident's primary 503 latency
SCENARIOS = {
    "ok": {},
    "primary_503_first_2": {"main": "503", "main_fail_calls": 2},
    "primary_503_every_call": {"main": "503"},
    "primary_and_backup_503": {"main": "503", "backup": "503"},
}


def base_profile(ticker: str) -> dict:
    [path] = sorted((ROOT / "eval" / "phase2" / PROFILE_RUNS[ticker] / "e3").glob("*_e3.json"))[:1]
    summary = json.loads(path.read_text())
    latencies = [c["latency_ms"] / 1000 for c in summary["per_call"] if not c.get("had_error")]
    return {"source": str(path.relative_to(ROOT)), "latencies": latencies, "median": statistics.median(latencies)}


def profile_for(ticker: str, scenario: str, expected_calls: int, speed: float) -> dict:
    base = base_profile(ticker)
    latencies = base["latencies"] + [base["median"]] * max(0, expected_calls - len(base["latencies"]))
    profile = {"latencies": [round(x * speed, 4) for x in latencies], "market_tool_rounds": 1,
               "data_seconds_per_tool": round(0.26 * speed, 4), **SCENARIOS[scenario]}
    for role in ("main", "backup"):
        if profile.get(role) == "503":
            profile[f"{role}_fail_seconds"] = round(FAIL_SECONDS * speed, 4)
    return profile


def run_cell(work: Path, cell: dict, speed: float) -> dict:
    from tradingagents import run_budget, service, worker
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.e1_routing import summarize_events
    from tradingagents.run_budget import expected_llm_calls
    from tradingagents.run_deadline import RunDeadline

    analysts = ALL_FOUR if cell["analysts"] == "all_four" else ("market",)
    rounds = 1 if cell["depth"] == "shallow" else 2
    expected = expected_llm_calls(len(analysts), rounds, rounds)
    base = work / f"run-{time.time_ns()}"
    base.mkdir(parents=True)
    (base / "profile.json").write_text(json.dumps(profile_for(cell["ticker"], cell["scenario"], expected, speed)))
    os.environ.update(REPLAY_PROFILE=str(base / "profile.json"), E3_RESULTS_DIR=str(base / "e3"))
    config = service.config_for_source(cell["source"], copy.deepcopy(DEFAULT_CONFIG))
    config.update(results_dir=str(base / "results"), data_cache_dir=str(work / "cache"),
                  memory_log_path=str(base / "m.md"), e2_cache_enabled=True, llm_provider="google",
                  quick_think_llm="replay-main", deep_think_llm="replay-main", quick_fallback_llm="replay-backup",
                  deep_fallback_llm="replay-backup", max_debate_rounds=rounds, max_risk_discuss_rounds=rounds)
    if config.get("e1_breaker_seconds"):
        config["e1_breaker_seconds"] = config["e1_breaker_seconds"] * speed     # scaled with the delays
    # Each interface's own limits: F1 run_budget.dashboard_budget, F2 run_budget.mcp_budget (the same defaults,
    # set by different variables). The MCP server's pricing check is the integration tests' concern.
    limits_for = run_budget.mcp_budget if cell["source"] == "mcp" else run_budget.dashboard_budget
    budget = limits_for(len(analysts), rounds, rounds)
    started = time.time()
    request = service.AnalysisRequest(
        ticker=cell["ticker"], trade_date=cell["date"], source=cell["source"], analysts=analysts, config=config,
        budget=budget, deadline=RunDeadline(analysis_seconds=180 * max(speed, 0.2), step_seconds=60 * max(speed, 0.2),
                                            started_at=started, target_seconds=41 * speed))
    row = {**cell, "expected_calls": expected, "call_limit": budget.max_llm_calls if budget else None,
           "cost_limit_usd": budget.max_cost_usd if budget else None}
    try:
        result = worker.run_in_worker(request, setup=SETUP)
        usage, record = result.usage, None
        row.update(outcome="completed", rating=result.rating)
    except service.AnalysisCancelled as exc:          # includes budget and deadline stops
        record = getattr(exc, "analysis_failure", None) or {}
        usage = record
        row.update(outcome=service.error_category(exc))
    except Exception as exc:  # noqa: BLE001 - every outcome is a row
        record = getattr(exc, "analysis_failure", None) or {}
        usage = record
        row.update(outcome=f"failed:{service.error_category(exc)}")
    events = (result.fallback_events if record is None else record.get("e1_fallback_events")) or []
    e1 = summarize_events(events)
    e2 = usage.get("e2_cache") or {}
    budget_snapshot = usage.get("run_budget") or {}
    row.update(seconds_scaled=round(time.time() - started, 2), llm_calls=usage.get("llm_calls"),
               tokens_in=usage.get("tokens_in"), tokens_out=usage.get("tokens_out"),
               e1_backup_calls=e1["backup_calls"], e1_breaker_skips=e1["breaker_skips"],
               e1_backup_failures=e1["backup_failures"], e2_hits=e2.get("hits"), e2_misses=e2.get("misses"),
               budget_cost_usd=budget_snapshot.get("budget_cost_usd"),
               e3_cost_usd=(usage.get("e3_cost") or {}).get("estimated_cost_usd"))
    return row


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--speed", type=float, default=0.1, help="delay multiplier (1 = real time)")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--only", default="", help="|-separated substrings; a cell runs if its label has any")
    parser.add_argument("--out", default=str(ROOT / "results" / f"eval_matrix_{time.strftime('%Y%m%d_%H%M%S')}"))
    args = parser.parse_args(argv)
    os.environ.update(E3_TRACE_ENABLED="1", LANGFUSE_PUBLIC_KEY="offline", LANGFUSE_SECRET_KEY="offline",
                      LANGFUSE_BASE_URL="http://localhost.invalid", WORKER_FAKE_MARKER=os.devnull,
                      TRADINGAGENTS_ANALYSIS_WORKER="process")
    pricing = Path(tempfile.mkdtemp()) / "pricing.json"
    pricing.write_text(json.dumps({m: {"input_per_million_usd": 0.3, "output_per_million_usd": 2.5}
                                   for m in ("replay-main", "replay-backup")}))
    os.environ["E3_PRICING_JSON"] = str(pricing)
    out = Path(args.out)
    (out / "work").mkdir(parents=True, exist_ok=True)
    wanted = [w for w in args.only.split("|") if w]
    cells = [dict(zip(("ticker", "date", "analysts", "depth", "scenario", "source"), combo, strict=True))
             for combo in itertools.product(PROFILE_RUNS, ("2026-10-23", "2026-01-09"), ("market", "all_four"),
                                            ("shallow", "deeper"), SCENARIOS, ("dashboard", "mcp"))]
    rows = []
    for cell in cells:
        label = " ".join(cell.values())
        if wanted and not any(w in label for w in wanted):
            continue
        for _ in range(max(1, args.reps)):
            row = run_cell(out / "work", cell, args.speed)
            rows.append(row)
            print(json.dumps(row), flush=True)
    if rows:
        with (out / "matrix.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    (out / "matrix.json").write_text(json.dumps({"speed": args.speed, "rows": rows}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
