#!/usr/bin/env python3
"""Offline Phase 3 latency benchmark: the real worker, E1, E2, E3, checkpoints, budget and deadlines,
with scripted models that replay the per-call latencies of saved real runs (no network, no API keys).

Profiles come from saved evidence:
  10-call: results/phase3_smoke/20261122_225216 (the approved live smoke run: 24.1 s of LLM time,
           market analyst asks for its 3 tools in one round, 1.7 s of non-LLM time)
  12-call: consistency_evaluation/nvda_live/run_1 (Phase 2 live: 26.4 s of LLM time, 3 tool rounds,
           14.2 s of non-LLM time, mostly live market data)
The model latencies are real measurements replayed; the provider, network and Gemini's own variance
are not. Times are from request creation to the result in the parent, so worker start-up is included.

    python scripts/benchmark_phase3_latency.py [--reps 5] [--out results/phase3_smoke/benchmark_<stamp>]  (git-ignored)
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SETUP = "tests.worker_fakes:replay"
FRAMEWORK_SECONDS = 0.9      # graph construction and lookups before the first call (E3 before_first_llm)


def _profile(e3_glob: str, usage: str, rounds: int) -> dict:
    [path] = sorted(ROOT.glob(e3_glob))[:1]
    summary = json.loads(path.read_text())
    latencies = [round(c["latency_ms"] / 1000, 3) for c in summary["per_call"]]
    tools = json.loads((ROOT / usage).read_text())["tool_calls"]
    non_llm = summary["wall_time_seconds"] - sum(latencies)
    return {"source": str(path.relative_to(ROOT)), "latencies": latencies, "market_tool_rounds": rounds,
            "data_seconds_per_tool": round(max(0.0, non_llm - FRAMEWORK_SECONDS) / tools, 3),
            "reference_wall_seconds": summary["wall_time_seconds"]}


def profiles() -> dict:
    return {
        "10-call": _profile("results/phase3_smoke/20261122_225216/e3/*_e3.json",
                            "results/phase3_smoke/20261122_225216/results/NVDA/2026-10-23/token_usage.json", 1),
        "12-call": _profile("consistency_evaluation/nvda_live/run_1/e3/NVDA_2026-10-23_27d3d8aa_e3.json",
                            "consistency_evaluation/nvda_live/run_1/results/NVDA/2026-10-23/token_usage.json", 3),
    }


def _request(base: Path, cache: Path, deadline):
    from tradingagents import run_budget, service
    from tradingagents.default_config import DEFAULT_CONFIG

    config = service.config_for_source("dashboard", copy.deepcopy(DEFAULT_CONFIG))
    config.update(results_dir=str(base / "results"), data_cache_dir=str(cache), memory_log_path=str(base / "m.md"),
                  e2_cache_enabled=True, llm_provider="google", quick_think_llm="replay-main",
                  deep_think_llm="replay-main", quick_fallback_llm="replay-backup", deep_fallback_llm="replay-backup")
    return service.AnalysisRequest(ticker="NVDA", trade_date="2026-01-09", source="dashboard", analysts=("market",),
                                   config=config, budget=run_budget.dashboard_budget(1, 1, 1), deadline=deadline)


def _warm_spare(cold: bool) -> None:
    from tradingagents import worker

    pool = worker.pool_for(SETUP)
    if cold:
        spare, pool._ready = pool._ready, None
        if spare is not None:
            spare.stop()
        return
    pool.replenish()
    for _ in range(600):
        if pool._ready is not None and pool._ready.wait_ready(120):
            return
        time.sleep(0.05)


def run_one(work: Path, profile: dict, *, e2_warm: bool, cold: bool, deadline_kwargs=None,
            cancel_after: float | None = None) -> dict:
    from tradingagents import service, worker
    from tradingagents.run_deadline import RunDeadline, interactive_deadline

    cache = work / ("cache-warm" if e2_warm else f"cache-{time.time_ns()}")
    base = work / f"run-{time.time_ns()}"
    base.mkdir(parents=True)
    profile_path = base / "profile.json"
    profile_path.write_text(json.dumps(profile))
    os.environ.update(REPLAY_PROFILE=str(profile_path), E3_RESULTS_DIR=str(base / "e3"))
    _warm_spare(cold)
    started = time.time()
    deadline = (RunDeadline(started_at=started, **deadline_kwargs) if deadline_kwargs
                else interactive_deadline(started_at=started))
    cancel = threading.Event()
    if cancel_after is not None:
        threading.Timer(cancel_after, cancel.set).start()
    outcome = "completed"
    try:
        result = worker.run_in_worker(_request(base, cache, deadline), cancel=cancel, setup=SETUP)
        calls = result.usage["llm_calls"]
    except service.AnalysisDeadlineExceeded as exc:
        outcome, calls = f"deadline_exceeded ({exc.deadline.get('which')})", None
    except service.AnalysisBudgetExceeded:
        outcome, calls = "budget_exceeded", None
    except service.AnalysisCancelled:
        outcome, calls = "cancelled", None
    except Exception as exc:  # noqa: BLE001
        outcome, calls = f"failed ({service.error_category(exc)})", None
    total = time.time() - started
    return {"seconds": round(total, 2), "outcome": outcome, "llm_calls": calls}


def summarize(rows: list[dict], scripted: float | None) -> dict:
    values = sorted(r["seconds"] for r in rows)
    p95 = values[min(len(values) - 1, round(0.95 * (len(values) - 1)))]
    out = {"n": len(values), "median": round(statistics.median(values), 2), "p95": p95, "max": values[-1],
           "outcomes": sorted({r["outcome"] for r in rows})}
    if scripted is not None:
        out["non_model_seconds_median"] = round(statistics.median(values) - scripted, 2)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--out", default=str(ROOT / "results" / "phase3_smoke" / f"benchmark_{time.strftime('%Y%m%d_%H%M%S')}"))
    parser.add_argument("--only", default="", help="|-separated parts of scenario names to run")
    args = parser.parse_args(argv)
    for name, value in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "offline", "LANGFUSE_SECRET_KEY": "offline",
                        "LANGFUSE_BASE_URL": "http://localhost.invalid", "WORKER_FAKE_MARKER": os.devnull}.items():
        os.environ[name] = value
    pricing = Path(tempfile.mkdtemp()) / "pricing.json"
    pricing.write_text(json.dumps({m: {"input_per_million_usd": 0.3, "output_per_million_usd": 2.5}
                                   for m in ("replay-main", "replay-backup")}))
    os.environ["E3_PRICING_JSON"] = str(pricing)
    out = Path(args.out)
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    p = profiles()
    p10, p12 = p["10-call"], p["12-call"]
    # "non_model_seconds" below = end-to-end minus the replayed model time: worker start-up and hand-off,
    # data fetching (replayed delays; skipped on E2 hits), graph, E1/E2/E3, checkpoints and result transfer.
    scripted10, scripted12 = sum(p10["latencies"]), sum(p12["latencies"])
    fail = 2.816          # the live incident's primary 503 latency (E3 per_call, run 20261123-113910-4266de44)
    r = max(1, args.reps)
    scenarios = [
        ("10-call, warm worker, E2 cold", {"profile": p10, "e2_warm": False, "cold": False}, r, scripted10),
        ("10-call, warm worker, E2 warm", {"profile": p10, "e2_warm": True, "cold": False}, max(1, r - 2),
         scripted10),
        ("10-call, cold worker, E2 cold", {"profile": p10, "e2_warm": False, "cold": True}, max(1, r - 2), scripted10),
        ("12-call, warm worker, E2 cold (live-data delays)", {"profile": p12, "e2_warm": False, "cold": False}, r,
         scripted12),
        ("12-call, warm worker, E2 warm", {"profile": p12, "e2_warm": True, "cold": False}, max(1, r - 2),
         scripted12),
        ("12-call, cold worker, E2 cold (live-data delays)", {"profile": p12, "e2_warm": False, "cold": True},
         max(1, r - 2), scripted12),
        ("12-call, primary 503 on its first 2 calls, backup covers them",
         {"profile": {**p12, "main": "503", "main_fail_seconds": fail, "main_fail_calls": 2}, "e2_warm": False,
          "cold": False}, max(1, r - 2), scripted12),
        ("hung primary (shorter: 30 s step deadline, 2 s grace)",
         {"profile": {**p10, "main": "hang"}, "e2_warm": False, "cold": False,
          "deadline_kwargs": {"analysis_seconds": 180, "step_seconds": 30, "grace_seconds": 2}}, max(1, r - 3), None),
        ("10-call, every primary call 503, backup answers", {"profile": {**p10, "main": "503", "main_fail_seconds": fail},
                                                          "e2_warm": False, "cold": False}, max(1, r - 2), None),
        ("10-call, primary 503 on its first 2 calls, backup covers them",
         {"profile": {**p10, "main": "503", "main_fail_seconds": fail, "main_fail_calls": 2}, "e2_warm": False,
          "cold": False}, max(1, r - 2), scripted10),
        ("10-call, primary and backup 503", {"profile": {**p10, "main": "503", "main_fail_seconds": fail,
                                                          "backup": "503", "backup_fail_seconds": 2 * fail + 1.5},
                                                 "e2_warm": False, "cold": False}, max(1, r - 2), None),
        ("12-call, cancelled after 5 s", {"profile": p12, "e2_warm": False, "cold": False, "cancel_after": 5.0},
         max(1, r - 2), None),
        ("12-call, 20 s analysis deadline", {"profile": p12, "e2_warm": False, "cold": False,
                                                 "deadline_kwargs": {"analysis_seconds": 20, "step_seconds": 60,
                                                                  "grace_seconds": 5}}, 2, None),
        ("hung primary (defaults: 60 s step deadline)", {"profile": {**p10, "main": "hang"}, "e2_warm": False,
                                                              "cold": False}, max(1, r - 2), None),
        ("primary 503, hung backup (defaults)", {"profile": {**p10, "main": "503", "main_fail_seconds": fail,
                                                             "backup": "hang"}, "e2_warm": False, "cold": False}, 1, None),
    ]
    wanted = {s.strip() for s in args.only.split("|") if s.strip()}
    report = {"profiles": {k: {x: v[x] for x in ("source", "market_tool_rounds", "data_seconds_per_tool",
                                                  "reference_wall_seconds")} | {"llm_seconds": round(sum(v["latencies"]), 2)}
                           for k, v in p.items()}, "scenarios": {}}
    # E2 warm scenarios need a filled cache: one unscored pass per profile first.
    for profile in (p10, p12):
        run_one(work, profile, e2_warm=True, cold=False)
    for name, kwargs, reps, scripted in scenarios:
        if wanted and not any(w in name for w in wanted):
            continue
        rows = [run_one(work, **kwargs) for _ in range(reps)]
        report["scenarios"][name] = {**summarize(rows, scripted), "runs": rows}
        print(f"{name:55} {json.dumps(summarize(rows, scripted))}", flush=True)
    (out / "benchmark.json").write_text(json.dumps(report, indent=1))
    return 0                       # the worker module stops its pools at exit


if __name__ == "__main__":
    sys.exit(main())
