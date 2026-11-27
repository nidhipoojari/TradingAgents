"""F2 read tools against the real F1 run 20261127-111852-5a8696f8, through the PRODUCTION server entry point
(.venv/bin/tradingagents-mcp, unmodified, stdio), started from the project root as an assistant would.

No provider call is possible: TRADINGAGENTS_MCP_MAX_LLM_CALLS=-1 makes analyze_stock refuse as a server setting
before any run starts, GOOGLE_API_KEY is set empty (dotenv never overrides it), Alpaca keys are empty, and an
unroutable proxy catches any stray HTTPS request. The server reads a COPY of the run record (RESULTS below), so the
real results folder and its audit log are untouched.

    python mcp_live_record_eval.py OUT_DIR
"""
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(sys.argv[1])
RUN = "20261127-111852-5a8696f8"
REAL = Path.home() / ".tradingagents" / "logs"
RESULTS = OUT / "results_copy"
(RESULTS / "runs").mkdir(parents=True, exist_ok=True)
shutil.copy2(REAL / "runs" / f"{RUN}.json", RESULTS / "runs" / f"{RUN}.json")
F1_PROPOSAL = json.loads((REAL / "proposals" / f"ta-{RUN}-buy.json").read_text())
LOG = []


def env(**extra):
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("TRADINGAGENTS_", "E3_", "LANGFUSE_", "GOOGLE_", "ALPACA", "APCA", "GEMINI"))}
    base.update(TRADINGAGENTS_RESULTS_DIR=str(RESULTS), TRADINGAGENTS_MCP_MAX_LLM_CALLS="-1", GOOGLE_API_KEY="",
                GEMINI_API_KEY="", ALPACA_API_KEY="", ALPACA_SECRET_KEY="", LANGFUSE_PUBLIC_KEY="",
                LANGFUSE_SECRET_KEY="", E3_TRACE_ENABLED="0",
                HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")
    base.update(extra)
    return base


def client(**extra):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    return Client(StdioTransport(command=str(ROOT / ".venv" / "bin" / "tradingagents-mcp"), args=[],
                                 env=env(**extra), cwd=str(ROOT)), timeout=60)


async def call(c, name, args, label):
    started = time.perf_counter()
    try:
        data = (await c.call_tool(name, args)).structured_content
        entry = {"label": label, "tool": name, "args": args, "ok": True, "data": data}
    except Exception as exc:  # noqa: BLE001 - the outcome
        entry = {"label": label, "tool": name, "args": args, "ok": False, "error": str(exc)[:600]}
    entry["seconds"] = round(time.perf_counter() - started, 3)
    LOG.append(entry)
    print(f"[{'OK ' if entry['ok'] else 'ERR'}] {label} ({entry['seconds']} s)"
          + ("" if entry["ok"] else f": {entry['error'][:150]}"), flush=True)
    return entry


async def main_session():
    async with client() as c:
        tools = await c.list_tools()
        (OUT / "tools_list.json").write_text(json.dumps(
            [{"name": t.name, "description": t.description, "input_schema": t.inputSchema,
              "output_schema": t.outputSchema, "annotations": t.annotations.model_dump() if t.annotations else None}
             for t in tools], indent=2))
        print("tools:", [t.name for t in tools], flush=True)
        await call(c, "list_recent_runs", {}, "list_recent_runs default")
        await call(c, "list_recent_runs", {"limit": 1}, "list_recent_runs limit=1")
        await call(c, "get_report", {"run_id": RUN}, "get_report all sections")
        for s in ("market", "sentiment", "news", "fundamentals", "investment_debate", "investment_plan",
                  "trader_plan", "risk_debate", "final_decision"):
            await call(c, "get_report", {"run_id": RUN, "section": s}, f"get_report section={s}")
        await call(c, "get_run_metrics", {"run_id": RUN}, "get_run_metrics")
        await call(c, "propose_trade", {"run_id": RUN}, "propose_trade")
        # invalid input
        for name, args, label in [
            ("get_report", {"run_id": RUN, "section": "horoscope"}, "bad section"),
            ("get_report", {"run_id": "../runs/" + RUN}, "path-like run_id"),
            ("get_report", {"run_id": RUN + "x"}, "unknown run_id"),
            ("get_report", {"run_id": 20261127}, "run_id of wrong type"),
            ("get_run_metrics", {"run_id": ""}, "empty run_id"),
            ("propose_trade", {"run_id": RUN, "qty": 5}, "propose_trade with qty"),
            ("list_recent_runs", {"limit": 0}, "limit 0"),
            ("list_recent_runs", {"limit": "5"}, "limit as string"),
            ("analyze_stock", {"ticker": "NVDA", "trade_date": "2026-10-23", "analysts": ["market"]},
             "analyze_stock (guarded: must refuse before any run)"),
            ("analyze_stock", {"ticker": "NVDA", "include_reports": "yes"}, "analyze_stock include_reports='yes'"),
            ("analyze_stock", {"ticker": "NVDA", "trade_date": "2999-01-01"}, "analyze_stock future date"),
        ]:
            await call(c, name, args, f"invalid: {label}")
        # concurrency: 12 read calls at once on one server
        started = time.perf_counter()
        batch = await asyncio.gather(*[call(c, n, a, f"concurrent {i}: {n}") for i, (n, a) in enumerate(
            [("get_report", {"run_id": RUN}), ("get_run_metrics", {"run_id": RUN}), ("propose_trade", {"run_id": RUN}),
             ("list_recent_runs", {})] * 3)])
        LOG.append({"label": "concurrent batch", "calls": len(batch), "all_ok": all(b["ok"] for b in batch),
                    "wall_seconds": round(time.perf_counter() - started, 3)})
        print("concurrent batch:", LOG[-1], flush=True)
    # source visibility: a server limited to MCP runs must not see the dashboard run
    async with client(TRADINGAGENTS_MCP_RUN_SOURCES="mcp") as c:
        await call(c, "list_recent_runs", {}, "sources=mcp: list_recent_runs")
        await call(c, "get_report", {"run_id": RUN}, "sources=mcp: get_report of the dashboard run")
    async with client(TRADINGAGENTS_MCP_RUN_SOURCES="dashboard") as c:
        await call(c, "list_recent_runs", {}, "sources=dashboard: list_recent_runs")


def main():
    asyncio.run(main_session())
    audit_path = RESULTS / "mcp_requests.jsonl"
    audit = [json.loads(x) for x in audit_path.read_text().splitlines()] if audit_path.exists() else []
    files = sorted(str(p.relative_to(RESULTS)) for p in RESULTS.rglob("*") if p.is_file())
    blob = json.dumps(LOG) + json.dumps(audit)
    secrets = [k for k in ("GOOGLE_API_KEY", "ALPACA_SECRET_KEY", "LANGFUSE_SECRET_KEY")
               if os.environ.get(k) and os.environ[k] in blob]
    (OUT / "results.json").write_text(json.dumps({"calls": LOG, "audit": audit, "files_after": files,
                                                  "f1_proposal": F1_PROPOSAL, "secret_values_found": secrets},
                                                 indent=2, default=str))
    print(f"\ncalls {sum(1 for x in LOG if 'tool' in x)}; audit lines {len(audit)}; files {files}; secrets {secrets}")


if __name__ == "__main__":
    main()
