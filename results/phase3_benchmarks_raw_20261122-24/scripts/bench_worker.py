"""The same replayed NVDA analysis through the Phase 3 worker (dashboard/MCP path): cold, then warm worker."""
import copy, json, os, sys, tempfile, time
sys.path.insert(0, os.environ["BENCH_DIR"]); sys.path.insert(0, os.getcwd())


def main():
    os.environ["TRADINGAGENTS_ANALYSIS_WORKER"] = "process"
    from tradingagents import service, worker
    from tradingagents.default_config import DEFAULT_CONFIG
    rows = []
    for label in ("cold worker", "warm worker", "warm worker"):
        tmp = tempfile.mkdtemp()
        for k, v in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "x", "LANGFUSE_SECRET_KEY": "x",
                     "LANGFUSE_BASE_URL": "http://localhost.invalid", "E3_RESULTS_DIR": f"{tmp}/e3",
                     "E3_PRICING_JSON": os.environ["PRICING"], "DATA_LOG": f"{tmp}/data.log"}.items():
            os.environ[k] = v
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg.update(results_dir=f"{tmp}/r", data_cache_dir=f"{tmp}/c", memory_log_path=f"{tmp}/m.md",
                   quick_think_llm="gemini-3.5-flash-lite", deep_think_llm="gemini-3.5-flash-lite", llm_provider="google",
                   max_debate_rounds=1, max_risk_discuss_rounds=1, e2_cache_enabled=False)
        request = service.AnalysisRequest(ticker="NVDA", trade_date="2026-10-23", analysts=("market",),
                                          source="dashboard", config=cfg)
        first = {}
        began = time.perf_counter()
        result = worker.run_in_worker(request, setup="replay_fakes:install",
                                      on_event=lambda e: first.setdefault("t", time.perf_counter()))
        total = time.perf_counter() - began
        [e3file] = __import__("pathlib").Path(f"{tmp}/e3").glob("*.json")
        e3 = json.loads(e3file.read_text())
        rows.append({"label": label, "total": round(total, 2), "first_event": round(first["t"] - began, 2),
                     "analysis": result.usage["elapsed_seconds"], "e3_wall": e3["wall_time_seconds"],
                     "calls": e3["observed_llm_calls"], "tokens": f"{result.usage['tokens_in']}/{result.usage['tokens_out']}",
                     "cost": e3["estimated_cost_usd"], "unattributed": "unattributed" in e3["per_agent"],
                     "calls_match": e3["observed_llm_calls"] == result.usage["llm_calls"],
                     "status": e3["status"], "source": e3["source"], "langfuse_flush": e3.get("langfuse_flush")})
        if label == "cold worker":
            time.sleep(8)                          # let the spare finish starting, as between real requests
    for r in rows:
        print(json.dumps(r))


if __name__ == "__main__":
    main()
