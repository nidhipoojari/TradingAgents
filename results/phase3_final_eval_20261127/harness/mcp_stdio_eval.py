"""F2 over the real MCP stdio protocol: a FastMCP client starts the real server as a subprocess
(mcp_scripted_server.py: production server, scripted models in its worker) and exercises all five tools.

Offline: no API key, no network (an unroutable proxy is set so any stray HTTPS request fails at once),
no orders. Writes results.json and a human-readable transcript to OUT.

    python mcp_stdio_eval.py OUT_DIR
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
WORK = Path(tempfile.mkdtemp(prefix="mcp-eval-"))
PAST = "2026-01-09"

pricing = WORK / "pricing.json"
pricing.write_text(json.dumps({m: {"input_per_million_usd": 0.1, "output_per_million_usd": 0.4}
                               for m in ("gemini-test", "gemini-test-backup")}))


def server_env(**extra) -> dict:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TERM") if k in os.environ}
    env.update(
        PYTHONPATH=f"{ROOT}:{HERE}",
        TRADINGAGENTS_RESULTS_DIR=str(WORK / "results"), TRADINGAGENTS_CACHE_DIR=str(WORK / "cache"),
        TRADINGAGENTS_MEMORY_LOG_PATH=str(WORK / "memory" / "m.md"), TRADINGAGENTS_E2_CACHE="true",
        TRADINGAGENTS_LLM_PROVIDER="google", TRADINGAGENTS_QUICK_THINK_LLM="gemini-test",
        TRADINGAGENTS_DEEP_THINK_LLM="gemini-test", TRADINGAGENTS_QUICK_FALLBACK_LLM="gemini-test-backup",
        TRADINGAGENTS_DEEP_FALLBACK_LLM="gemini-test-backup",
        GOOGLE_API_KEY="offline-placeholder-not-a-key",
        E3_TRACE_ENABLED="1", E3_RESULTS_DIR=str(WORK / "e3"), E3_PRICING_JSON=str(pricing),
        LANGFUSE_PUBLIC_KEY="offline", LANGFUSE_SECRET_KEY="offline", LANGFUSE_BASE_URL="http://localhost.invalid",
        HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9", NO_PROXY="",
        WORKER_FAKE_MARKER=os.devnull,
    )
    env.update(extra)
    return env


def client(**extra):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(command=sys.executable, args=[str(HERE / "mcp_scripted_server.py")],
                               env=server_env(**extra), cwd=str(ROOT))
    return Client(transport, timeout=120)


LOG: list[dict] = []


async def call(c, name, args, label):
    started = time.perf_counter()
    try:
        result = await c.call_tool(name, args)
        entry = {"label": label, "tool": name, "args": args, "ok": True, "seconds": round(time.perf_counter() - started, 3),
                 "data": result.structured_content}
    except Exception as exc:  # noqa: BLE001 - recorded as the outcome
        entry = {"label": label, "tool": name, "args": args, "ok": False,
                 "seconds": round(time.perf_counter() - started, 3), "error_type": type(exc).__name__,
                 "error": str(exc)[:900]}
    LOG.append(entry)
    print(f"[{'OK ' if entry['ok'] else 'ERR'}] {label} ({entry['seconds']} s)"
          + ("" if entry["ok"] else f": {entry['error'][:160]}"), flush=True)
    return entry


async def main_session():
    async with client() as c:
        tools = await c.list_tools()
        listing = [{"name": t.name, "input_schema": t.inputSchema, "output_schema": t.outputSchema,
                    "annotations": t.annotations.model_dump() if t.annotations else None} for t in tools]
        (OUT / "tools_list.json").write_text(json.dumps(listing, indent=2))
        print("tools:", [t["name"] for t in listing], flush=True)

        # analyze_stock: valid, then invalid inputs
        a = await call(c, "analyze_stock", {"ticker": "NVDA", "trade_date": PAST, "analysts": ["market"]},
                       "analyze_stock valid (market only)")
        a2 = await call(c, "analyze_stock", {"ticker": "NVDA", "trade_date": PAST, "analysts": ["market"],
                                            "include_reports": True}, "analyze_stock repeat, warm E2, reports")
        for args, label in [
            ({"ticker": "NVDA", "trade_date": "2999-01-01"}, "future date"),
            ({"ticker": "NVDA", "trade_date": "2026-13-45"}, "invalid date"),
            ({"ticker": "NVDA", "trade_date": "01/09/2026"}, "malformed date"),
            ({"ticker": "NV DA"}, "ticker with a space"),
            ({"ticker": ""}, "empty ticker"),
            ({"ticker": "../../etc"}, "path-like ticker"),
            ({"ticker": "NVDA", "trade_date": PAST, "analysts": ["astrology"]}, "unknown analyst"),
            ({"ticker": 123}, "ticker of wrong type"),
            ({}, "missing ticker"),
            ({"ticker": "NVDA", "include_reports": "yes"}, "include_reports of wrong type"),
        ]:
            await call(c, "analyze_stock", args, f"analyze_stock invalid: {label}")

        rid = a["data"]["run_id"] if a["ok"] else None
        await call(c, "list_recent_runs", {}, "list_recent_runs default")
        for lim in (1, 50, 51, 0, -1, "10", 2.5, True):
            await call(c, "list_recent_runs", {"limit": lim}, f"list_recent_runs limit={lim!r}")
        await call(c, "get_report", {"run_id": rid}, "get_report all sections")
        await call(c, "get_report", {"run_id": rid, "section": "market"}, "get_report section=market")
        await call(c, "get_report", {"run_id": rid, "section": "horoscope"}, "get_report bad section")
        await call(c, "get_report", {"run_id": "../../secrets"}, "get_report path-like run_id")
        await call(c, "get_report", {"run_id": "no-such-run"}, "get_report unknown run_id")
        await call(c, "get_report", {}, "get_report missing run_id")
        await call(c, "get_run_metrics", {"run_id": rid}, "get_run_metrics")
        await call(c, "get_run_metrics", {"run_id": "no-such-run"}, "get_run_metrics unknown run_id")
        await call(c, "propose_trade", {"run_id": rid}, "propose_trade")
        await call(c, "propose_trade", {"run_id": "no-such-run"}, "propose_trade unknown run_id")
        await call(c, "propose_trade", {"run_id": rid, "qty": 10, "approve": True}, "propose_trade extra args")
        # Tools that would change anything do not exist.
        for name in ("submit_order", "approve_trade", "cancel_order"):
            await call(c, name, {"run_id": rid}, f"non-existent tool {name}")
    return rid


async def busy_and_timeout():
    # One server, two simultaneous analyses: the second must be refused at once.
    async with client(MCP_FAKE_SETUP="eval_fakes:slow_2s") as c:
        first = asyncio.create_task(call(c, "analyze_stock", {"ticker": "NVDA", "trade_date": PAST,
                                                              "analysts": ["market"]}, "concurrency: first"))
        await asyncio.sleep(3.0)
        await call(c, "analyze_stock", {"ticker": "AAPL", "trade_date": PAST, "analysts": ["market"]},
                   "concurrency: second while first runs")
        await call(c, "list_recent_runs", {"limit": 3}, "concurrency: read tool while analysis runs")
        await first
    # Server-side timeout of 5 s against a ~20 s run.
    async with client(MCP_FAKE_SETUP="eval_fakes:slow_2s", TRADINGAGENTS_MCP_TIMEOUT_SECONDS="5") as c:
        await call(c, "analyze_stock", {"ticker": "NVDA", "trade_date": PAST, "analysts": ["market"]},
                   "timeout: 5 s limit, ~20 s run")
        await asyncio.sleep(4)
        await call(c, "analyze_stock", {"ticker": "NVDA", "trade_date": PAST, "analysts": ["market"]},
                   "after timeout: server free again (still slow, so times out again)")
    # Per-run LLM-call limit set by the server operator.
    async with client(TRADINGAGENTS_MCP_MAX_LLM_CALLS="3") as c:
        await call(c, "analyze_stock", {"ticker": "NVDA", "trade_date": PAST, "analysts": ["market"]},
                   "budget: server limit of 3 LLM calls")
    # Cost limit without prices: refused before any run.
    async with client(E3_PRICING_JSON="") as c:
        await call(c, "analyze_stock", {"ticker": "NVDA", "trade_date": PAST, "analysts": ["market"]},
                   "budget: cost limit without E3 prices")
    # Langfuse not configured: tracing off, run still completes.
    async with client(LANGFUSE_PUBLIC_KEY="", LANGFUSE_SECRET_KEY="", LANGFUSE_BASE_URL="", E3_TRACE_ENABLED="0") as c:
        await call(c, "analyze_stock", {"ticker": "NVDA", "trade_date": PAST, "analysts": ["market"]},
                   "Langfuse not configured (E3_TRACE_ENABLED=0, no keys)")


def main():
    rid = asyncio.run(main_session())
    asyncio.run(busy_and_timeout())
    results = WORK / "results"
    audit = results / "mcp_requests.jsonl"
    audit_lines = [json.loads(x) for x in audit.read_text().splitlines()] if audit.exists() else []
    side_effects = sorted(str(p.relative_to(WORK)) for p in WORK.rglob("*")
                          if p.is_file() and any(k in p.name for k in ("paper_order", "approval", "ledger", "proposal")))
    summary = {"work_dir": str(WORK), "first_run_id": rid, "calls": LOG, "audit_lines": audit_lines,
               "order_or_approval_files_created": side_effects,
               "run_records": sorted(p.name for p in (results / "runs").glob("*.json"))}
    (OUT / "results.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"\ncalls: {len(LOG)}; audit lines: {len(audit_lines)}; order/approval files: {side_effects}")


if __name__ == "__main__":
    main()
