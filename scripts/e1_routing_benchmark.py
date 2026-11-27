#!/usr/bin/env python3
"""E1 routing benchmark: same model on both tiers vs a mixed quick/deep pair.

Controlled comparison of cost, latency and report quality. Live runs need
``--approve-live-llm``; without it the script prints the design and an offline
pre-flight and exits 2. ``--analyze DIR`` summarizes finished runs offline (no API).

    python scripts/e1_routing_benchmark.py --deep-model <stronger model>            # design + pre-flight
    python scripts/e1_routing_benchmark.py --deep-model <m> --approve-live-llm      # the live benchmark
    python scripts/e1_routing_benchmark.py --analyze results/phase3_e1_benchmark/<stamp>

Models are overridden per run only; the configured models are not changed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "smoke"))

from _safety import redact  # noqa: E402

DEEP_AGENTS = ("Research Manager", "Portfolio Manager")
DESIGN = """\
E1 routing benchmark -- NOT RUN (pass --approve-live-llm to run it)

Arms (models overridden per run; configuration unchanged):
  A same-model   quick = deep = {quick}
  B mixed        quick = {quick}, deep = {deep}   (only Research Manager and Portfolio Manager change)
Controls: tickers {tickers} on {date}; all four analysts; 1 debate and 1 risk round;
  the same E2 cache for every run, warmed by one unscored data pass, so market data is
  identical and not timed; temperature from TRADINGAGENTS_TEMPERATURE (0 recommended);
  E1 backup off, so a failure is visible instead of masked; arms interleaved A,B,B,A,...
  per ticker to spread rate-limit and time-of-day effects; {repeats} repeats per arm per ticker.
Runs: {runs} scored analyses ({per_arm} per arm), plus {tickers_n} unscored warm-up passes.
Metrics per run (from E3 and the saved reports):
  cost      estimated_cost_usd_successful_calls (needs prices for both models)
  latency   deep-agent LLM seconds (primary), total LLM busy seconds, analysis_seconds
  tokens    input / output, per agent
  quality   (1) rating stability: share of repeats agreeing with the arm's modal rating
            (2) decision completeness: rating, executive summary, thesis, price target, horizon
            (3) blind review: decisions exported with shuffled labels for a human rubric
                (blind_review/ + key.json); no LLM judge (added cost, self-preference bias)
Decision rule (pre-registered): prefer B only if its quality is not worse on (1)-(3) and
  its cost and deep-agent latency increases are acceptable to you; otherwise keep A.
Budget: each analysis is cancelled at --max-calls (default 30) or --max-cost (default $0.10).
Estimated cost: arm A ~= $0.022 per analysis (Phase 2 runs on gemini-3.5-flash-lite);
  arm B depends on the deep model's price, which must be in E3_PRICING_JSON.
"""


# ------------------------------------------------------------------ offline analysis
SECTIONS = {"rating": r"\*\*Rating\*\*\s*:", "executive_summary": r"\*\*Executive Summary\*\*",
            "thesis": r"\*\*Investment Thesis\*\*", "price_target": r"\*\*Price Target\*\*",
            "time_horizon": r"\*\*Time Horizon\*\*"}


def completeness(decision: str) -> float:
    """Share of the Portfolio Manager's expected sections present in the decision text."""
    found = sum(1 for pattern in SECTIONS.values() if re.search(pattern, decision or "", re.IGNORECASE))
    return round(found / len(SECTIONS), 2)


def run_metrics(summary: dict, decision: str, rating: str) -> dict:
    """One analysis's numbers, from its E3 summary and decision text."""
    calls = summary.get("per_call", [])
    deep = [c for c in calls if c["agent"] in DEEP_AGENTS]
    return {
        "cost_usd": summary.get("estimated_cost_usd_successful_calls", summary.get("estimated_cost_usd")),
        "deep_llm_seconds": round(sum(c["latency_ms"] for c in deep) / 1000, 3),
        "llm_busy_seconds": (summary.get("timing") or {}).get("llm_busy_seconds"),
        "analysis_seconds": summary.get("analysis_seconds"),
        "tokens_in": sum(c["input_tokens"] or 0 for c in calls),
        "tokens_out": sum(c["output_tokens"] or 0 for c in calls),
        "errors": summary.get("observed_llm_errors", 0),
        "deep_models": sorted({c["model"] for c in deep}),
        "rating": rating,
        "completeness": completeness(decision),
    }


def summarize(rows: list[dict]) -> dict:
    """Per-arm medians, ranges and rating stability; rows carry 'arm' and 'ticker'."""
    out = {}
    for arm in sorted({r["arm"] for r in rows}):
        mine = [r for r in rows if r["arm"] == arm]
        stats = {"runs": len(mine)}
        for key in ("cost_usd", "deep_llm_seconds", "llm_busy_seconds", "analysis_seconds",
                    "tokens_in", "tokens_out", "completeness"):
            values = [r[key] for r in mine if isinstance(r.get(key), int | float)]
            stats[key] = ({"median": round(statistics.median(values), 4), "min": min(values), "max": max(values),
                           "n": len(values)} if values else None)
        agree = []
        for ticker in sorted({r["ticker"] for r in mine}):
            ratings = [r["rating"] for r in mine if r["ticker"] == ticker]
            modal = max(set(ratings), key=ratings.count)
            agree.append(ratings.count(modal) / len(ratings))
        stats["rating_stability"] = round(statistics.mean(agree), 3) if agree else None
        stats["errors"] = sum(r.get("errors", 0) for r in mine)
        stats["unpriced_runs"] = sum(1 for r in mine if r.get("cost_usd") is None)
        out[arm] = stats
    return out


def analyze(directory: Path) -> dict:
    rows = [json.loads(line) for line in (directory / "runs.jsonl").read_text(encoding="utf-8").splitlines() if line]
    return summarize(rows)


# ------------------------------------------------------------------ live benchmark
def preflight(quick: str, deep: str) -> list[str]:
    problems = []
    if not os.environ.get("GOOGLE_API_KEY"):
        problems.append("GOOGLE_API_KEY is not set")
    if (os.environ.get("E3_TRACE_ENABLED") or "").lower() not in {"1", "true", "yes"}:
        problems.append("E3_TRACE_ENABLED=1 is not set (E3 measures cost and latency)")
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        if not os.environ.get(key):
            problems.append(f"{key} is not set")
    path = os.environ.get("E3_PRICING_JSON")
    prices = json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).is_file() else {}
    if not prices:
        problems.append("E3_PRICING_JSON must name a pricing file")
    for model in (quick, deep):
        if prices and model not in prices:
            problems.append(f"no price for {model} in {path}: add the provider's published price first")
    if not deep:
        problems.append("--deep-model is required (the stronger model for arm B)")
    elif deep == quick:
        problems.append("--deep-model must differ from --quick-model, or there is nothing to compare")
    return problems


def schedule(tickers, repeats):
    """Interleaved arm order per ticker: A,B,B,A,A,B,..."""
    order = []
    for ticker in tickers:
        for i in range(repeats):
            order += [(ticker, "A"), (ticker, "B")] if i % 2 == 0 else [(ticker, "B"), (ticker, "A")]
    return order


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--approve-live-llm", action="store_true")
    parser.add_argument("--analyze", metavar="DIR", help="summarize finished runs offline")
    parser.add_argument("--quick-model", default="gemini-3.5-flash-lite", help="Phase 2 baseline model")
    parser.add_argument("--deep-model", default="", help="the stronger model for arm B")
    parser.add_argument("--tickers", default="NVDA,AAPL,MSFT")
    parser.add_argument("--date", default="2026-10-23")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-calls", type=int, default=30)
    parser.add_argument("--max-cost", type=float, default=0.10)
    parser.add_argument("--out", default=str(ROOT / "results" / "phase3_e1_benchmark" / datetime.now().strftime("%Y%m%d_%H%M%S")))
    args = parser.parse_args(argv)

    if args.analyze:
        print(json.dumps(analyze(Path(args.analyze)), indent=2))
        return 0
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    runs = len(schedule(tickers, args.repeats))
    problems = preflight(args.quick_model, args.deep_model)
    if not args.approve_live_llm:
        print(DESIGN.format(quick=args.quick_model, deep=args.deep_model or "<--deep-model>", tickers=",".join(tickers),
                            date=args.date, repeats=args.repeats, runs=runs, per_arm=runs // 2,
                            tickers_n=len(tickers)))
        print("Pre-flight (offline):", "ready" if not problems else "NOT READY")
        for problem in problems:
            print(f"  - {redact(problem)}")
        return 2
    if problems:
        print("Pre-flight failed; nothing was run:")
        for problem in problems:
            print(f"  - {redact(problem)}")
        return 1

    from live_gemini_langfuse import BudgetGuard

    from tradingagents import service

    out = Path(args.out)
    (out / "blind_review").mkdir(parents=True, exist_ok=True)
    os.environ["E3_RESULTS_DIR"] = str(out / "e3")
    pricing = json.loads(Path(os.environ["E3_PRICING_JSON"]).read_text(encoding="utf-8"))
    base = service.config_for_source("api")
    base.update(llm_provider="google", data_cache_dir=str(out / "cache"), e2_cache_enabled=True,
                max_debate_rounds=1, max_risk_discuss_rounds=1, quick_fallback_llm=None, deep_fallback_llm=None,
                memory_log_path=str(out / "memory" / "trading_memory.md"))
    arms = {"A": {"quick_think_llm": args.quick_model, "deep_think_llm": args.quick_model},
            "B": {"quick_think_llm": args.quick_model, "deep_think_llm": args.deep_model}}

    def one(ticker, arm):
        config = dict(base, **arms[arm], results_dir=str(out / "results" / f"{arm}"))
        cancel = threading.Event()
        guard = BudgetGuard(cancel, args.max_calls, args.max_cost, pricing)
        request = service.AnalysisRequest(ticker=ticker, trade_date=args.date, source="api", config=config)
        result = service.run_analysis(request, cancel=cancel, extra_callbacks=[guard])
        summary = json.loads(result.e3_summary_path.read_text(encoding="utf-8"))
        return result, run_metrics(summary, result.decision_text, result.rating)

    for ticker in tickers:                          # unscored warm-up: fills the shared E2 cache
        one(ticker, "A")
    key, n = {}, 0
    with (out / "runs.jsonl").open("a", encoding="utf-8") as log:
        for ticker, arm in schedule(tickers, args.repeats):
            try:
                result, metrics = one(ticker, arm)
            except Exception as exc:  # noqa: BLE001
                log.write(json.dumps({"arm": arm, "ticker": ticker, "error": redact(f"{type(exc).__name__}: {exc}")[:300]}) + "\n")
                continue
            n += 1
            label = f"decision_{n:02d}"
            (out / "blind_review" / f"{label}.md").write_text(result.decision_text, encoding="utf-8")
            key[label] = {"arm": arm, "ticker": ticker, "run_id": result.run_id}
            log.write(json.dumps({"arm": arm, "ticker": ticker, "run_id": result.run_id, **metrics}) + "\n")
    import random
    labels = list(key)
    random.Random(len(labels)).shuffle(labels)          # review in shuffled order; key kept apart
    (out / "blind_review" / "ORDER.txt").write_text("\n".join(labels) + "\n", encoding="utf-8")
    (out / "key.json").write_text(json.dumps(key, indent=2), encoding="utf-8")
    print(json.dumps(analyze(out), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
