"""Section 4.3 provider / network / tracing error cases, end to end through the F1 job path's runner
(worker.run_in_worker -> service.run_analysis) with injected faults (eval_fakes:faults). Offline.

    python error_scenarios.py OUT_DIR
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT), str(HERE)]
os.chdir(ROOT)
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
WORK = Path(tempfile.mkdtemp(prefix="errors-"))
PAST = "2026-01-09"
pricing = WORK / "pricing.json"
pricing.write_text(json.dumps({m: {"input_per_million_usd": 0.1, "output_per_million_usd": 0.4}
                               for m in ("gemini-test", "gemini-test-backup")}))
for key in [k for k in os.environ if k.startswith(("TRADINGAGENTS_", "E3_", "LANGFUSE_", "GOOGLE_", "ALPACA", "APCA"))]:
    del os.environ[key]
BASE_ENV = dict(
    TRADINGAGENTS_RESULTS_DIR=str(WORK / "results"), TRADINGAGENTS_MEMORY_LOG_PATH=str(WORK / "memory" / "m.md"),
    TRADINGAGENTS_E2_CACHE="true", TRADINGAGENTS_LLM_PROVIDER="google", TRADINGAGENTS_QUICK_THINK_LLM="gemini-test",
    TRADINGAGENTS_DEEP_THINK_LLM="gemini-test", TRADINGAGENTS_QUICK_FALLBACK_LLM="gemini-test-backup",
    TRADINGAGENTS_DEEP_FALLBACK_LLM="gemini-test-backup", GOOGLE_API_KEY="offline-placeholder-not-a-key",
    E3_TRACE_ENABLED="1", E3_RESULTS_DIR=str(WORK / "e3"), E3_PRICING_JSON=str(pricing),
    LANGFUSE_PUBLIC_KEY="offline", LANGFUSE_SECRET_KEY="offline", LANGFUSE_BASE_URL="http://localhost.invalid",
    HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9", WORKER_FAKE_MARKER=os.devnull,
)
os.environ.update(BASE_ENV)

SCENARIOS = [  # label, env overrides, cache folder (shared = warm after the first)
    ("baseline, cold cache", {}, "shared"),
    ("network down for market data, E2 warm (same ticker/date)", {"EVAL_VENDOR": "network"}, "shared"),
    ("network down for market data, E2 cold", {"EVAL_VENDOR": "network"}, "fresh"),
    ("untyped ConnectionError reaching the router, E2 cold (not what real vendors raise)", {"EVAL_VENDOR": "raw"},
     "fresh"),
    ("network down for the LLM provider (main and backup)", {"EVAL_MAIN": "network", "EVAL_BACKUP": "network"}, "shared"),
    ("main model 429 rate limit, backup healthy", {"EVAL_MAIN": "429"}, "shared"),
    ("main model quota exhausted, backup healthy", {"EVAL_MAIN": "quota"}, "shared"),
    ("main model 503, backup healthy (E1 forced failure)", {"EVAL_MAIN": "503"}, "shared"),
    ("main and backup 429", {"EVAL_MAIN": "429", "EVAL_BACKUP": "429"}, "shared"),
    ("main and backup quota exhausted", {"EVAL_MAIN": "quota", "EVAL_BACKUP": "quota"}, "shared"),
    ("main and backup 503", {"EVAL_MAIN": "503", "EVAL_BACKUP": "503"}, "shared"),
    ("Langfuse not configured, E3 on", {"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": "", "LANGFUSE_BASE_URL": ""},
     "shared"),
    ("E3 tracing switched off", {"E3_TRACE_ENABLED": "0"}, "shared"),
    ("nothing configured: E3_TRACE_ENABLED unset, no Langfuse keys",
     {"E3_TRACE_ENABLED": "", "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": "", "LANGFUSE_BASE_URL": ""}, "shared"),
]


def run(label, env, cache):
    from tradingagents import service, worker
    from tradingagents.run_budget import dashboard_budget

    for k in ("EVAL_MAIN", "EVAL_BACKUP", "EVAL_VENDOR"):
        os.environ.pop(k, None)
    os.environ.update(BASE_ENV)
    os.environ.update(env)
    config = service.config_for_source("dashboard")
    # Set on the config itself: DEFAULT_CONFIG folds the environment in at import, so a later env change is not seen.
    config["data_cache_dir"] = str(WORK / ("cache" if cache == "shared" else f"cache-{time.time_ns()}"))
    request = service.AnalysisRequest(ticker="NVDA", trade_date=PAST, source="dashboard", analysts=("market",),
                                      config=config, budget=dashboard_budget(1), run_id=service.new_run_id())
    started = time.perf_counter()
    row = {"label": label, "env": env, "run_id": request.run_id}
    try:
        result = worker.run_in_worker(request, setup="eval_fakes:faults")
        usage = result.usage
        row.update(outcome="completed", rating=result.rating, llm_calls=usage.get("llm_calls"),
                   e2_cache=usage.get("e2_cache"), e1=__import__("tradingagents.e1_routing", fromlist=["x"])
                   .summarize_events(result.fallback_events),
                   market_report_head=(result.final_state.get("market_report") or "")[:120],
                   e3_summary=bool(result.e3_summary_path))
    except Exception as exc:  # noqa: BLE001 - the outcome under test
        record = getattr(exc, "analysis_failure", None) or {}
        row.update(outcome="failed", category=service.error_category(exc), hint=service.failure_hint(exc),
                   message=service.sanitize_error(service.describe_error(exc), 300),
                   partial_llm_calls=record.get("llm_calls"), partial_failed=record.get("llm_calls_failed"))
    row["seconds"] = round(time.perf_counter() - started, 2)
    rec = WORK / "results" / "runs" / f"{request.run_id}.json"
    row["run_record"] = json.loads(rec.read_text())["status"] if rec.exists() else None
    print(json.dumps(row, default=str), flush=True)
    return row


def main():
    rows = [run(*s) for s in SCENARIOS]
    (OUT / "error_scenarios.json").write_text(json.dumps({"work_dir": str(WORK), "rows": rows}, indent=2, default=str))


if __name__ == "__main__":
    main()
