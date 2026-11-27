"""The real TradingAgents MCP server (tradingagents.mcp_server.main), started as its own process, with one
change: the analysis worker installs scripted models and offline data vendors (no provider, no network).

Everything else is the production path: FastMCP, argument validation, the worker process, the shared
service, E1/E2/E3, budget, deadlines, run records and the MCP audit log.

MCP_FAKE_SETUP picks the worker fake (default tests.worker_fakes:scripted).
"""
import os
import sys
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.chdir(ROOT)

from tradingagents import mcp_server, worker  # noqa: E402

SETUP = os.environ.get("MCP_FAKE_SETUP", "tests.worker_fakes:scripted")
worker.analysis_runner = lambda: partial(worker.run_in_worker, setup=SETUP)

if __name__ == "__main__":
    mcp_server.main(sys.argv[1:])
