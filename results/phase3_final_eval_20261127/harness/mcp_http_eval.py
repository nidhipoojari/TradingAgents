"""F2 over streamable HTTP on loopback: the real server process (scripted models), token required.
Offline (127.0.0.1 only). The token is generated here and never written to the evidence.

    python mcp_http_eval.py OUT_DIR
"""
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
sys.path[:0] = [str(HERE)]
from mcp_stdio_eval import server_env  # noqa: E402

TOKEN = secrets.token_urlsafe(32)
rows = []


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(args, **env):
    return subprocess.Popen([sys.executable, str(HERE / "mcp_scripted_server.py"), "--transport", "http", *args],
                            env=server_env(**env), cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)


def refused(label, args, **env):
    proc = start(args, **env)
    try:
        out, _ = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        out = "(still running after 60 s: NOT refused)"
    rows.append({"label": label, "exit_code": proc.returncode, "output": out.replace(TOKEN, "<token>")[-400:]})
    print(label, "-> exit", proc.returncode, flush=True)


async def calls(url):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    for label, headers in (("no token", {}), ("wrong token", {"Authorization": "Bearer " + "x" * 43}),
                           ("correct token", {"Authorization": f"Bearer {TOKEN}"})):
        try:
            async with Client(StreamableHttpTransport(url, headers=headers), timeout=60) as c:
                tools = [t.name for t in await c.list_tools()]
                runs = (await c.call_tool("list_recent_runs", {"limit": 2})).structured_content
                a = (await c.call_tool("analyze_stock", {"ticker": "NVDA", "trade_date": "2026-01-09",
                                                         "analysts": ["market"]})).structured_content
                rows.append({"label": f"http {label}", "ok": True, "tools": tools, "runs": len(runs["runs"]),
                             "analyze_rating": a["rating"], "analyze_source": a["source"]})
        except Exception as exc:  # noqa: BLE001 - the outcome
            rows.append({"label": f"http {label}", "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
        print(rows[-1]["label"], "ok" if rows[-1]["ok"] else rows[-1]["error"][:120], flush=True)


def main():
    p = port()
    proc = start(["--port", str(p)], TRADINGAGENTS_MCP_TOKEN=TOKEN, NO_PROXY="127.0.0.1,localhost")
    try:
        for _ in range(200):
            try:
                socket.create_connection(("127.0.0.1", p), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        os.environ.update(NO_PROXY="127.0.0.1,localhost", HTTPS_PROXY="", HTTP_PROXY="")
        asyncio.run(calls(f"http://127.0.0.1:{p}/mcp"))
    finally:
        proc.terminate()
        proc.wait(10)
    refused("http without a token configured", ["--port", str(port())], TRADINGAGENTS_MCP_TOKEN="")
    refused("http on 0.0.0.0 without --allow-remote", ["--host", "0.0.0.0", "--port", str(port())],
            TRADINGAGENTS_MCP_TOKEN=TOKEN)
    refused("http --allow-remote without TLS", ["--host", "0.0.0.0", "--allow-remote", "--port", str(port())],
            TRADINGAGENTS_MCP_TOKEN=TOKEN)
    refused("http with a short token", ["--port", str(port())], TRADINGAGENTS_MCP_TOKEN="short")
    text = json.dumps(rows, indent=2)
    assert TOKEN not in text
    (OUT / "http_results.json").write_text(text)


if __name__ == "__main__":
    main()
