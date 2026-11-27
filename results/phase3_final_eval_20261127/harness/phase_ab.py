"""Matched Phase 2 vs Phase 3 timing on identical replayed inputs.

usage: phase_ab.py TREE LABEL MODE PROFILE_JSON REPS E2(cold|warm) E3(on|off) OUT_JSONL
MODE: graph   - the Phase 2 CLI's analysis sequence in-process (works on both trees)
      service - Phase 3 shared service in-process, with budget and deadline (Phase 3 tree)
      worker  - Phase 3 worker process, prewarmed (Phase 3 tree)
Times: from just before the run starts to the result in this process.
"""
import copy
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path


def main():
    tree, label, mode, profile_path, reps, e2, e3, out = sys.argv[1:9]
    reps = int(reps)
    sys.path.insert(0, tree)
    os.chdir(tree)
    work = Path(tempfile.mkdtemp())
    pricing = work / "pricing.json"
    pricing.write_text(json.dumps({m: {"input_per_million_usd": 0.3, "output_per_million_usd": 2.5}
                                   for m in ("replay-main", "replay-backup")}))
    os.environ.update(LANGFUSE_PUBLIC_KEY="offline", LANGFUSE_SECRET_KEY="offline",
                      LANGFUSE_BASE_URL="http://localhost.invalid", WORKER_FAKE_MARKER=os.devnull,
                      E3_PRICING_JSON=str(pricing), REPLAY_PROFILE=profile_path,
                      E3_TRACE_ENABLED="1" if e3 == "on" else "0", TRADINGAGENTS_ANALYSIS_WORKER="process")
    from tests import worker_fakes
    from tradingagents.default_config import DEFAULT_CONFIG

    warm_cache = work / "cache-warm"

    def config_for(base):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config.update(results_dir=str(base / "results"), memory_log_path=str(base / "m.md"),
                      data_cache_dir=str(warm_cache if e2 == "warm" else base / "cache"), e2_cache_enabled=True,
                      llm_provider="google", quick_think_llm="replay-main", deep_think_llm="replay-main",
                      quick_fallback_llm="replay-backup", deep_fallback_llm="replay-backup")
        return config

    def run_graph(base):
        worker_fakes.replay()                         # fresh replay state per run, in this process
        from cli.stats_handler import StatsCallbackHandler   # present in both trees (a shim in Phase 3)
        from tradingagents.e3_cli_hook import E3Session
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        config = config_for(base)
        os.environ["E3_RESULTS_DIR"] = str(base / "e3")
        started = time.perf_counter()
        stats = StatsCallbackHandler()
        session = E3Session.from_env(ticker="NVDA", analysis_date="2026-01-09")
        callbacks = [stats] + session.callbacks
        graph = TradingAgentsGraph(["market"], config=config, debug=False, callbacks=callbacks)
        state = graph.create_run_state("NVDA", "2026-01-09", "stock", None)
        args = graph.propagator.get_graph_args(callbacks=callbacks)
        graph.begin_checkpoint("NVDA", "2026-01-09", "stock", None)
        final = {}
        try:
            for _messages, chunk in graph.stream_run(graph.checkpoint_input(state), **args):
                if chunk is not None:
                    final.update(chunk)
            graph.record_decision("NVDA", "2026-01-09", final)
        finally:
            graph.end_checkpoint()
        session.mark_completed(decision=final.get("final_trade_decision"))
        session.save()
        return time.perf_counter() - started, stats.get_stats()["llm_calls"]

    def run_service(base, in_worker):
        from tradingagents import service, worker
        from tradingagents.run_budget import RunBudget
        try:
            from tradingagents.run_deadline import interactive_deadline
        except ImportError:                           # committed tree, before deadlines existed
            interactive_deadline = None

        os.environ["E3_RESULTS_DIR"] = str(base / "e3")
        if in_worker:
            pool = worker.pool_for("tests.worker_fakes:replay")
            pool.replenish()
            for _ in range(600):
                if pool._ready is not None and pool._ready.wait_ready(120):
                    break
                time.sleep(0.05)
        else:
            worker_fakes.replay()
        started_wall, started = time.time(), time.perf_counter()
        # A module missing from an older tree is served from the project (editable install), so check the
        # tree's own AnalysisRequest rather than whether the import worked.
        has_deadline = "deadline" in getattr(service.AnalysisRequest, "__dataclass_fields__", {})
        extra = {"deadline": interactive_deadline(started_at=started_wall)} if has_deadline else {}
        request = service.AnalysisRequest(ticker="NVDA", trade_date="2026-01-09", source="dashboard",
                                          analysts=("market",), config=config_for(base), budget=RunBudget(15, 0.04),
                                          **extra)
        if in_worker:
            result = worker.run_in_worker(request, setup="tests.worker_fakes:replay")
        else:
            result = service.run_analysis(request)
        return time.perf_counter() - started, result.usage["llm_calls"]

    if e2 == "warm":                                  # fill the shared cache once, unscored
        base = work / "fill"
        base.mkdir()
        {"graph": run_graph, "service": lambda b: run_service(b, False),
         "worker": lambda b: run_service(b, True)}[mode](base)
    times, calls = [], set()
    for i in range(reps):
        base = work / f"run{i}"
        base.mkdir()
        seconds, n = {"graph": run_graph, "service": lambda b: run_service(b, False),
                      "worker": lambda b: run_service(b, True)}[mode](base)
        times.append(seconds)
        calls.add(n)
    row = {"label": label, "mode": mode, "profile": Path(profile_path).stem, "e2": e2, "e3": e3, "n": reps,
           "median": round(statistics.median(times), 4), "min": round(min(times), 4), "max": round(max(times), 4),
           "p95": round(sorted(times)[min(reps - 1, round(0.95 * (reps - 1)))], 4), "llm_calls": sorted(calls)}
    with open(out, "a") as handle:
        handle.write(json.dumps(row) + "\n")
    print("ROW", json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
