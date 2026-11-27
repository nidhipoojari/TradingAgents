"""CLI vs F1 vs F2 on one identical scripted workload (offline: scripted models, offline vendors, no network).

- CLI: the real unattended CLI entry (cli.run.run_analysis with flags), in this process, source "cli".
- F1: the dashboard's job path (dashboard.jobs.start_job -> worker.run_in_worker), source "dashboard".
- F2: the MCP tool path (mcp_server.analyze_stock_sync -> worker.run_in_worker), source "mcp".
Each writes its run record (run_store); the records are compared field by field.

    python interface_consistency.py OUT_DIR
"""
import json
import os
import sys
import tempfile
from functools import partial
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT), str(HERE)]
os.chdir(ROOT)
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
WORK = Path(tempfile.mkdtemp(prefix="consistency-"))
PAST = "2026-01-09"

pricing = WORK / "pricing.json"
pricing.write_text(json.dumps({m: {"input_per_million_usd": 0.1, "output_per_million_usd": 0.4}
                               for m in ("gemini-test", "gemini-test-backup")}))
for key in [k for k in os.environ if k.startswith(("TRADINGAGENTS_", "E3_", "LANGFUSE_", "GOOGLE_", "ALPACA", "APCA"))]:
    del os.environ[key]
os.environ.update(
    TRADINGAGENTS_RESULTS_DIR=str(WORK / "results"), TRADINGAGENTS_CACHE_DIR=str(WORK / "cache"),
    TRADINGAGENTS_MEMORY_LOG_PATH=str(WORK / "memory" / "m.md"), TRADINGAGENTS_E2_CACHE="true",
    TRADINGAGENTS_LLM_PROVIDER="google", TRADINGAGENTS_QUICK_THINK_LLM="gemini-test",
    TRADINGAGENTS_DEEP_THINK_LLM="gemini-test", TRADINGAGENTS_QUICK_FALLBACK_LLM="gemini-test-backup",
    TRADINGAGENTS_DEEP_FALLBACK_LLM="gemini-test-backup", TRADINGAGENTS_OUTPUT_LANGUAGE="English",
    TRADINGAGENTS_MAX_DEBATE_ROUNDS="1", TRADINGAGENTS_MAX_RISK_ROUNDS="1",
    TRADINGAGENTS_CHECKPOINT_ENABLED="false",
    GOOGLE_API_KEY="offline-placeholder-not-a-key", E3_TRACE_ENABLED="1", E3_RESULTS_DIR=str(WORK / "e3"),
    E3_PRICING_JSON=str(pricing), LANGFUSE_PUBLIC_KEY="offline", LANGFUSE_SECRET_KEY="offline",
    LANGFUSE_BASE_URL="http://localhost.invalid", HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9",
    WORKER_FAKE_MARKER=os.devnull,
)

from tests import worker_fakes  # noqa: E402

SETUP = "tests.worker_fakes:scripted"


def cli_run():
    worker_fakes.scripted()                          # the same fakes the worker installs, in this process
    from cli import run, selections

    selections.updates.enabled = lambda: False
    run.run_analysis(checkpoint=False, flags={"ticker": "NVDA", "date": PAST, "analysts": "market",
                                              "save": False, "show": False, "html": False})


def f1_run():
    from tradingagents import service, worker
    from tradingagents.dashboard import jobs
    from tradingagents.run_budget import dashboard_budget

    config = service.config_for_source("dashboard")
    request = service.AnalysisRequest(ticker="NVDA", trade_date=PAST, source="dashboard", analysts=("market",),
                                      config=config, budget=dashboard_budget(1))
    job = jobs.start_job(request, run=partial(worker.run_in_worker, setup=SETUP))
    import time

    for _ in range(3000):
        if not job.running:
            break
        time.sleep(0.1)
    assert job.result is not None, (job.status, job.error)


def f2_run():
    from tradingagents import mcp_server, worker

    mcp_server.analyze_stock_sync("NVDA", PAST, ["market"], run=partial(worker.run_in_worker, setup=SETUP))


def main():
    outcome = {}
    for name, fn in (("cli", cli_run), ("dashboard", f1_run), ("mcp", f2_run)):
        try:
            fn()
            outcome[name] = "ok"
        except BaseException as exc:  # noqa: BLE001 - recorded
            outcome[name] = f"{type(exc).__name__}: {exc}"[:400]
        print(name, outcome[name], flush=True)
    records = {}
    for path in sorted((WORK / "results" / "runs").glob("*.json")):
        rec = json.loads(path.read_text())
        records[rec["source"]] = rec
    keys = ("status", "ticker", "trade_date", "analysts", "rating")
    usage_keys = ("llm_calls", "tool_calls", "tokens_in", "tokens_out")
    rows = {}
    for src, rec in records.items():
        usage = rec.get("usage") or {}
        rows[src] = {**{k: rec.get(k) for k in keys}, **{k: usage.get(k) for k in usage_keys},
                     "decision_sha": __import__("hashlib").sha256(
                         json.dumps(rec.get("reports"), sort_keys=True).encode()).hexdigest()[:12],
                     "e2_cache": usage.get("e2_cache"), "estimated_cost_usd": (usage.get("e3_cost") or {}).get("estimated_cost_usd"),
                     "e3_summary_linked": bool(rec.get("e3_summary_path")), "record_keys": len(rec)}
    compare = {k: len({json.dumps(r.get(k), sort_keys=True) for r in rows.values()}) == 1
               for k in (*keys, *usage_keys, "decision_sha")}
    result = {"work_dir": str(WORK), "outcome": outcome, "rows": rows, "identical": compare}
    (OUT / "consistency.json").write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps({"rows": rows, "identical": compare}, indent=2, default=str))


if __name__ == "__main__":
    main()
