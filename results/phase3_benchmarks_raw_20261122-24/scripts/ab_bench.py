"""Matched A/B: the same latency replay through one code tree (argv[1]); prints median/max per profile."""
import copy, json, os, statistics, sys, tempfile, time
from pathlib import Path

def main():
    global one
    root, label, reps = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    sys.path.insert(0, str(root))
    os.chdir(root)
    from tradingagents import service, worker                       # noqa: E402
    from tradingagents.default_config import DEFAULT_CONFIG          # noqa: E402
    from tradingagents.run_budget import RunBudget                   # noqa: E402
    try:
        from tradingagents.run_deadline import interactive_deadline  # current tree only
    except ImportError:
        interactive_deadline = None
    SETUP = "tests.worker_fakes:replay"
    profiles = json.load(open(sys.argv[4]))
    work = Path(tempfile.mkdtemp())
    for k, v in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "offline", "LANGFUSE_SECRET_KEY": "offline",
                 "LANGFUSE_BASE_URL": "http://localhost.invalid", "WORKER_FAKE_MARKER": os.devnull,
                 "TRADINGAGENTS_ANALYSIS_WORKER": "process"}.items():
        os.environ[k] = v
    pricing = work / "pricing.json"
    pricing.write_text(json.dumps({m: {"input_per_million_usd": 0.3, "output_per_million_usd": 2.5} for m in ("replay-main", "replay-backup")}))
    os.environ["E3_PRICING_JSON"] = str(pricing)


    def one(profile):
        base = work / f"r{time.time_ns()}"
        base.mkdir()
        (base / "p.json").write_text(json.dumps(profile))
        os.environ.update(REPLAY_PROFILE=str(base / "p.json"), E3_RESULTS_DIR=str(base / "e3"))
        pool = worker.pool_for(SETUP)
        pool.replenish()
        for _ in range(600):
            if pool._ready is not None and pool._ready.wait_ready(120):
                break
            time.sleep(0.05)
        config = service.config_for_source("dashboard", copy.deepcopy(DEFAULT_CONFIG))
        config.update(results_dir=str(base / "results"), data_cache_dir=str(base / "cache"), memory_log_path=str(base / "m.md"),
                      e2_cache_enabled=True, llm_provider="google", quick_think_llm="replay-main", deep_think_llm="replay-main",
                      quick_fallback_llm="replay-backup", deep_fallback_llm="replay-backup")
        started = time.time()
        extra = {"deadline": interactive_deadline(started_at=started)} if interactive_deadline else {}
        request = service.AnalysisRequest(ticker="NVDA", trade_date="2026-01-09", source="dashboard", analysts=("market",),
                                          config=config, budget=RunBudget(15, 0.04), **extra)
        result = worker.run_in_worker(request, setup=SETUP)
        assert result.usage["llm_calls"] in (10, 12), result.usage["llm_calls"]
        return time.time() - started


    for name in ("10-call", "12-call"):
        times = [one(profiles[name]) for _ in range(reps)]
        print(f"AB {label:10} {name}: median {statistics.median(times):.2f}s  min {min(times):.2f}s  max {max(times):.2f}s  "
              f"runs {[round(t, 2) for t in times]}", flush=True)


if __name__ == "__main__":       # spawned workers re-import this file: run only as the main script
    main()
