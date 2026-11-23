#!/usr/bin/env python3
"""Section 4 evaluation: Phase 1 vs Phase 2 under identical, recorded conditions.

Every run is a fresh ``python -m cli.main`` process started in the tree under
test, with the environment built here from scratch (only API keys are inherited),
so no shell export can silently change a condition. Each plan writes to a NEW
directory and refuses to touch an existing one.

    # 1. See what would run, the exact conditions, the import check, and cost.
    .venv/bin/python scripts/phase2_eval.py run phase1 --out eval/phase1 --dry-run
    # 2. Run (calls the LLM API; costs money).
    .venv/bin/python scripts/phase2_eval.py run phase1  --out eval/phase1
    .venv/bin/python scripts/phase2_eval.py run phase2  --out eval/phase2
    .venv/bin/python scripts/phase2_eval.py run tracing --out eval/tracing --cache-from eval/phase2
    .venv/bin/python scripts/phase2_eval.py run forced-failure --out eval/forced_failure
    # 3. Build the Section 4.2 table from the run directories.
    .venv/bin/python scripts/phase2_eval.py summarize eval/phase1 eval/phase2 eval/tracing eval/forced_failure

Plans
  phase1          Phase 1 tree (no E1/E2/E3): NVDA x3, AAPL, MSFT
  phase2          this tree, E1 + E2 + E3 on:  NVDA x3, AAPL, MSFT
  tracing         this tree, E3 on/off alternating, NVDA x3 each, on a warm cache
  forced-failure  this tree, main model set to a name that does not exist: NVDA x1

Latency is the subprocess wall time: process start to exit (CLI start ->
final rating saved). Tokens and cost come from the CLI's own token_usage.json in
both trees (same counter, same commit 92d7dd0), priced with e3_pricing.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
PHASE1_TREE = HERE.parent / "TradingAgents-Phase1"
DATE = "2026-10-23"
MODEL = "gemini-3.5-flash-lite"
BACKUP = "gemini-3.1-flash-lite"
PRICING = HERE / "e3_pricing.json"

# Only these are inherited from the shell; every setting is set explicitly below.
INHERITED = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ",
             "GOOGLE_API_KEY", "GEMINI_API_KEY",
             "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")

COMMON = {
    "TRADINGAGENTS_LLM_PROVIDER": "google",
    "TRADINGAGENTS_QUICK_THINK_LLM": MODEL,
    "TRADINGAGENTS_DEEP_THINK_LLM": MODEL,
    "TRADINGAGENTS_OUTPUT_LANGUAGE": "English",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "1",
    "TRADINGAGENTS_MAX_RISK_ROUNDS": "1",
    "TRADINGAGENTS_CHECKPOINT_ENABLED": "false",
}
PHASE2 = {
    "TRADINGAGENTS_E2_CACHE": "true",
    "TRADINGAGENTS_QUICK_FALLBACK_LLM": BACKUP,
    "TRADINGAGENTS_DEEP_FALLBACK_LLM": BACKUP,
    "TRADINGAGENTS_LLM_BACKUP_MAX_RETRIES": "1",
    "E3_TRACE_ENABLED": "1",
    "E3_PRICING_JSON": str(PRICING),
}
TRACE_OFF = {"E3_TRACE_ENABLED": "0"}

STANDARD = ["NVDA", "NVDA", "NVDA", "AAPL", "MSFT"]
PLANS = {
    "phase1": {"tree": PHASE1_TREE, "env": {"TRADINGAGENTS_E2_CACHE": "false"},
               "runs": [(t, {}) for t in STANDARD], "est_tokens": 55_575},
    "phase2": {"tree": HERE, "env": PHASE2,
               "runs": [(t, {}) for t in STANDARD], "est_tokens": 31_000},
    # Rerun of tickers whose phase2 runs were slowed by provider 429s (E1 fallbacks).
    # The original runs stay on disk and are reported alongside.
    "phase2-rerun": {"tree": HERE, "env": PHASE2,
                     "runs": [("AAPL", {}), ("MSFT", {})], "est_tokens": 31_000},
    "tracing": {"tree": HERE, "env": PHASE2,
                "runs": [("NVDA", on_off) for _ in range(3) for on_off in ({}, TRACE_OFF)],
                "est_tokens": 31_000},
    "forced-failure": {"tree": HERE,
                       "env": {**PHASE2, "TRADINGAGENTS_QUICK_THINK_LLM": "gemini-does-not-exist",
                               "TRADINGAGENTS_DEEP_THINK_LLM": "gemini-does-not-exist",
                               "TRADINGAGENTS_QUICK_FALLBACK_LLM": os.environ.get("EVAL_FORCED_BACKUP", MODEL),
                               "TRADINGAGENTS_DEEP_FALLBACK_LLM": os.environ.get("EVAL_FORCED_BACKUP", MODEL)},
                       "runs": [("NVDA", {})], "est_tokens": 31_000},
}
CLI_ARGS = ["--date", DATE, "--analysts", "market", "--no-checkpoint", "--save", "--no-show"]
# Phase 1 baseline as stated in the report's Section 4.2 (used when no phase1 plan was run).
STATED_PHASE1 = {"latency_median_s": "41.2", "cost_mean_usd": "0.0311", "tokens_mean": "74611",
                 "tokens_in_mean": "n/a", "tokens_out_mean": "n/a", "llm_calls_mean": "n/a",
                 "success": "n/a", "consistency": "n/a", "nvda_ratings": []}
RATING = re.compile(r"\*\*Rating\*\*:\s*\**\s*([A-Za-z ]+?)\s*(?:\*|\n|$)")


def _git(tree: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True).stdout.strip()


def _pricing() -> dict:
    return json.loads(PRICING.read_text(encoding="utf-8"))


def _cost(tokens_in, tokens_out, model):
    price = _pricing().get(model)
    if price is None or tokens_in is None or tokens_out is None:
        return None
    return round((tokens_in * price["input_per_million_usd"]
                  + tokens_out * price["output_per_million_usd"]) / 1e6, 6)


def _env(run_dir: Path, cache_dir: Path, *layers: dict) -> dict:
    env = {k: os.environ[k] for k in INHERITED if k in os.environ}
    for layer in (COMMON, *layers):
        env.update(layer)
    env.update({
        "TRADINGAGENTS_RESULTS_DIR": str(run_dir / "results"),
        "TRADINGAGENTS_CACHE_DIR": str(cache_dir),
        # Fresh per run: the Memory Log would feed earlier runs' decisions
        # into later ones and break run independence.
        "TRADINGAGENTS_MEMORY_LOG_PATH": str(run_dir / "memory" / "trading_memory.md"),
        "E3_RESULTS_DIR": str(run_dir / "e3"),
        "PYTHONUNBUFFERED": "1",
    })
    return env


def _check_imports(tree: Path, env: dict) -> dict:
    """The tree's own code must be what runs, not another checkout's editable install."""
    out = subprocess.run(
        [sys.executable, "-c", "import cli, tradingagents; print(cli.__file__); print(tradingagents.__file__)"],
        cwd=tree, env=env, capture_output=True, text=True, check=True).stdout.splitlines()
    for path in out:
        if not Path(path).resolve().is_relative_to(tree.resolve()):
            raise SystemExit(f"Refusing to run: {path} is outside {tree}")
    return {"cli": out[0], "tradingagents": out[1]}


def _preflight(model: str) -> None:
    """Three tiny requests, all must succeed: refuse to start on a model out of quota.

    A free-tier daily limit is enforced intermittently near exhaustion, so one
    lucky success does not show the model is available.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI
    try:
        for _ in range(3):
            ChatGoogleGenerativeAI(model=model, max_retries=0).invoke("Reply with OK.")
            time.sleep(2)
    except Exception as exc:  # noqa: BLE001 - any failure means the plan would be contaminated
        detail = re.search(r"quotaId': '([^']+)'.*?retryDelay': '([^']+)'", str(exc))
        why = f"{detail.group(1)}, retry in {detail.group(2)}" if detail else f"{type(exc).__name__}: {str(exc)[:200]}"
        raise SystemExit(f"Preflight failed for {model}: {why}. Not starting.") from exc
    print(f"Preflight OK: {model}")


def _secret_free(env: dict) -> dict:
    return {k: ("<set>" if "KEY" in k else v) for k, v in sorted(env.items())
            if k.startswith(("TRADINGAGENTS_", "E3_"))}


def _collect(run_dir: Path, ticker: str, primary_model: str) -> dict:
    row: dict = {}
    usage_files = list((run_dir / "results").glob(f"{ticker}/{DATE}/token_usage.json"))
    usage = json.loads(usage_files[0].read_text()) if usage_files else {}
    if usage.get("status") == "failed":     # Phase 3 records failed runs too; Phase 2 counted only completed ones
        usage = {}
    row.update(llm_calls=usage.get("llm_calls"), tool_calls=usage.get("tool_calls"),
               tokens_in=usage.get("tokens_in"), tokens_out=usage.get("tokens_out"),
               tokens_total=usage.get("tokens_total"), cli_elapsed_seconds=usage.get("elapsed_seconds"))
    row["cost_usd"] = _cost(row["tokens_in"], row["tokens_out"], primary_model)
    e2 = usage.get("e2_cache") or {}
    row.update(e2_hits=e2.get("hits"), e2_misses=e2.get("misses"), e2_hit_rate_pct=e2.get("hit_rate_pct"))

    e3_files = sorted((run_dir / "e3").glob("*_e3.json"))
    if e3_files:
        e3 = json.loads(e3_files[-1].read_text())
        timing = e3.get("timing") or {}
        row.update(e3_cost_usd=e3.get("estimated_cost_usd"), e3_llm_errors=e3.get("observed_llm_errors"),
                   e3_models=",".join(sorted({c["model"] for c in e3.get("per_call", [])})),
                   e3_llm_busy_seconds=timing.get("llm_busy_seconds"),
                   e3_before_first_llm_seconds=timing.get("before_first_llm_seconds"),
                   e3_between_llm_seconds=timing.get("between_llm_calls_seconds"),
                   e3_after_last_llm_seconds=timing.get("after_last_llm_seconds"))

    decision = list((run_dir / "results").glob(f"{ticker}/{DATE}/reports/final_trade_decision.md"))
    match = RATING.search(decision[0].read_text()) if decision else None
    row["rating"] = match.group(1).strip() if match else None

    fallback_log = run_dir / "results" / "e1_fallback_log.txt"
    row["e1_fallback_lines"] = (len(fallback_log.read_text().splitlines()) if fallback_log.exists() else 0)
    return row


def run_plan(name: str, out: Path, dry_run: bool, pause: float, cache_from: Path | None) -> None:
    plan = PLANS[name]
    tree: Path = plan["tree"]
    # Runs start in the tree under test, so every path handed to them is absolute.
    out = out.resolve()
    cache_from = cache_from.resolve() if cache_from else None
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"Refusing to overwrite existing results: {out}")
    cache_dir = out / "data_cache"
    primary_model = plan["env"].get("TRADINGAGENTS_QUICK_THINK_LLM", MODEL)
    if primary_model.endswith("does-not-exist"):
        primary_model = plan["env"]["TRADINGAGENTS_QUICK_FALLBACK_LLM"]

    probe_env = _env(out / "probe", cache_dir, plan["env"])
    imports = _check_imports(tree, probe_env)
    runs = [(f"{i:02d}_{t}{'_trace_off' if extra else ''}", t, extra) for i, (t, extra) in enumerate(plan["runs"], 1)]
    price = _pricing()[MODEL]
    est = len(runs) * plan["est_tokens"] * (0.87 * price["input_per_million_usd"]
                                            + 0.13 * price["output_per_million_usd"]) / 1e6

    print(f"Plan {name}: {len(runs)} runs in {tree}")
    print(f"Imports from: {imports['tradingagents']}")
    print(f"Estimated API cost: ${est:.3f} ({plan['est_tokens']:,} tokens/run, 87% input)")
    print("Conditions:", json.dumps(_secret_free(probe_env), indent=1))
    for run_id, ticker, extra in runs:
        print(f"  {run_id}: {ticker} {extra or ''}")
    if dry_run:
        return

    _preflight(primary_model)
    out.mkdir(parents=True, exist_ok=True)
    if cache_from is not None:
        shutil.copytree(cache_from / "data_cache", cache_dir)
    manifest = {
        "plan": name, "started_utc": datetime.now(UTC).isoformat(), "tree": str(tree),
        "git_head": _git(tree, "rev-parse", "HEAD"), "git_dirty_files": _git(tree, "status", "--short").splitlines(),
        "python": sys.version, "imports": imports, "cache_from": str(cache_from) if cache_from else None,
        "packages": subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True).stdout.splitlines(),
        "conditions": _secret_free(probe_env), "cli_args": CLI_ARGS, "runs": [],
    }
    rows = []
    for index, (run_id, ticker, extra) in enumerate(runs):
        if index and pause:
            time.sleep(pause)
        run_dir = out / run_id
        run_dir.mkdir()
        env = _env(run_dir, cache_dir, plan["env"], extra)
        print(f"[{name}] {run_id} ...", flush=True)
        began = time.perf_counter()
        with (run_dir / "console.log").open("w") as log:
            proc = subprocess.run([sys.executable, "-m", "cli.main", "--ticker", ticker, *CLI_ARGS],
                                  cwd=tree, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        wall = round(time.perf_counter() - began, 3)
        row = {"plan": name, "run_id": run_id, "ticker": ticker,
               "tracing": env.get("E3_TRACE_ENABLED") == "1", "exit_code": proc.returncode,
               "wall_seconds": wall, **_collect(run_dir, ticker, primary_model)}
        row["success"] = proc.returncode == 0 and row["rating"] is not None
        rows.append(row)
        manifest["runs"].append({"run_id": run_id, "extra_env": extra})
        print(f"    exit={proc.returncode} wall={wall}s tokens={row['tokens_total']} rating={row['rating']}", flush=True)

    fields = list(dict.fromkeys(k for r in rows for k in r))
    with (out / "runs.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    manifest["finished_utc"] = datetime.now(UTC).isoformat()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Saved {out / 'runs.csv'}")


def _rows(directory: Path) -> list[dict]:
    with (directory / "runs.csv").open() as f:
        return list(csv.DictReader(f))


def _num(rows, key):
    return [float(r[key]) for r in rows if r.get(key) not in (None, "")]


def _fmt(values, digits=3, agg=statistics.median):
    return f"{agg(values):.{digits}f}" if values else "n/a"


def summarize(dirs: list[Path]) -> None:
    by_plan: dict[str, list[dict]] = {}
    for d in dirs:
        rows = _rows(d)
        plan = "phase2" if rows[0]["plan"] == "phase2-rerun" else rows[0]["plan"]
        if plan == "phase2" and rows[0]["plan"] == "phase2-rerun":
            # A rerun replaces that ticker's original phase2 row in the table.
            rerun = {r["ticker"] for r in rows}
            by_plan["phase2"] = [r for r in by_plan.get("phase2", []) if r["ticker"] not in rerun]
        by_plan.setdefault(plan, []).extend(rows)

    def block(plan):
        rows = by_plan.get(plan, [])
        ok = [r for r in rows if r["success"] == "True"]
        nvda = [r for r in rows if r["ticker"] == "NVDA"]
        ratings = [r["rating"] for r in nvda[:3]]
        second = nvda[1] if len(nvda) > 1 else {}
        return {
            "n": len(rows),
            "latency_median_s": _fmt(_num(ok, "wall_seconds")),
            "latency_range_s": (f"{min(_num(ok, 'wall_seconds')):.1f}-{max(_num(ok, 'wall_seconds')):.1f}"
                                if ok else "n/a"),
            "cost_mean_usd": _fmt(_num(ok, "cost_usd"), 5, statistics.mean),
            "tokens_mean": _fmt(_num(ok, "tokens_total"), 0, statistics.mean),
            "tokens_in_mean": _fmt(_num(ok, "tokens_in"), 0, statistics.mean),
            "tokens_out_mean": _fmt(_num(ok, "tokens_out"), 0, statistics.mean),
            "llm_calls_mean": _fmt(_num(ok, "llm_calls"), 1, statistics.mean),
            "success": f"{len(ok)}/{len(rows)}",
            "cache_hit_2nd_nvda_pct": second.get("e2_hit_rate_pct") or "n/a",
            "nvda_ratings": ratings,
            "consistency": (f"{max(ratings.count(x) for x in ratings)}/{len(ratings)} same"
                            if ratings and None not in ratings else "n/a"),
        }

    summary = {plan: block(plan) for plan in by_plan}
    if "tracing" in by_plan:
        rows = [r for r in by_plan["tracing"] if r["success"] == "True"]
        on = _num([r for r in rows if r["tracing"] == "True"], "wall_seconds")
        off = _num([r for r in rows if r["tracing"] == "False"], "wall_seconds")
        if on and off:
            delta = statistics.median(on) - statistics.median(off)
            summary["tracing_overhead"] = {
                "on_s": on, "off_s": off, "median_delta_s": round(delta, 3),
                "pct_of_off_median": round(100 * delta / statistics.median(off), 1),
                "note": "Resolvable only if |delta| exceeds the spread within each arm.",
                "spread_on_s": round(max(on) - min(on), 3), "spread_off_s": round(max(off) - min(off), 3),
            }
    if "forced-failure" in by_plan:
        r = by_plan["forced-failure"][0]
        summary["forced_failure"] = {"finished": r["success"], "fallback_log_lines": r["e1_fallback_lines"],
                                     "models_used": r.get("e3_models")}
    print(json.dumps(summary, indent=2))

    if "phase2" in summary:
        p1, p2 = summary.get("phase1", STATED_PHASE1), summary["phase2"]
        print("\nBefore column:", "measured phase1 plan" if "phase1" in summary else "stated Phase 1 baseline (report 4.2)")

        def change(a, b):
            try:
                a, b = float(a), float(b)
                return f"{100 * (b - a) / a:+.1f}%"
            except ValueError:
                return "n/a"
        print("\n| Metric | Before (Phase 1) | After (Phase 2) | Change |\n|---|---:|---:|---:|")
        for label, key in [("Latency per run, median (s)", "latency_median_s"),
                           ("Cost per run, mean (USD)", "cost_mean_usd"),
                           ("Tokens per run, mean", "tokens_mean"),
                           ("Input tokens, mean", "tokens_in_mean"),
                           ("Output tokens, mean", "tokens_out_mean"),
                           ("LLM calls, mean", "llm_calls_mean")]:
            print(f"| {label} | {p1[key]} | {p2[key]} | {change(p1[key], p2[key])} |")
        print(f"| Run success | {p1['success']} | {p2['success']} | |")
        print(f"| Cache hit rate, 2nd NVDA run (%) | n/a | {p2['cache_hit_2nd_nvda_pct']} | |")
        print(f"| Rating consistency (NVDA x3) | {p1['consistency']} {p1['nvda_ratings']} "
              f"| {p2['consistency']} {p2['nvda_ratings']} | |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("plan", choices=sorted(PLANS))
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--pause", type=float, default=10.0, help="seconds between runs (rate limits)")
    run.add_argument("--cache-from", type=Path, help="copy this plan dir's data_cache first (warm cache)")
    summ = sub.add_parser("summarize")
    summ.add_argument("dirs", type=Path, nargs="+")
    args = parser.parse_args()
    if args.command == "run":
        run_plan(args.plan, args.out, args.dry_run, args.pause, args.cache_from)
    else:
        summarize(args.dirs)


if __name__ == "__main__":
    main()
