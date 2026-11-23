"""Offline fakes installed inside an analysis worker process (tests only).

The worker is a spawned child: monkeypatches made in the test process do not reach
it, so a test passes ``setup="tests.worker_fakes:<function>"`` and the child calls it
before taking its request. Every function makes the child fully offline: scripted
models, offline vendors, and a stand-in Langfuse.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import types

import pandas as pd

# Module level, not inside _scripted: with_fallbacks (E1 with a backup model) evaluates the
# ``-> Runnable`` annotation of bind_tools, and Python 3.14 resolves it in these globals.
from langchain_core.runnables import Runnable


def _offline_vendors() -> None:
    from langchain_core.callbacks import BaseCallbackHandler

    from tradingagents.agents import context
    from tradingagents.agents.analysts import sentiment_analyst
    from tradingagents.dataflows import router
    from tradingagents.dataflows.vendors.yahoo import market as yahoo_market, snapshot

    for method, vendors in router.VENDOR_METHODS.items():
        for vendor in vendors:
            vendors[vendor] = lambda *a, _m=method, **k: f"{_m} data"
    prices = pd.DataFrame({"Date": pd.bdate_range(end="2026-01-09", periods=60), "Open": 100.0, "High": 101.0,
                           "Low": 99.0, "Close": 100.5, "Volume": 1_000_000})
    snapshot.load_ohlcv = lambda *a, **k: prices.copy()
    sentiment_analyst.fetch_stocktwits_messages = lambda *a, **k: "no posts"
    sentiment_analyst.fetch_reddit_posts = lambda *a, **k: "no posts"
    yahoo_market.yf.Ticker = lambda s: type("T", (), {"info": {"longName": "NVIDIA"}})()
    context._identity.cache_clear()

    class Handler(BaseCallbackHandler):
        pass

    class Client:
        def flush(self):
            pass

    langfuse = types.ModuleType("langfuse")
    langfuse.__path__ = []
    langfuse.get_client = Client
    langchain = types.ModuleType("langfuse.langchain")
    langchain.CallbackHandler = Handler
    sys.modules["langfuse"], sys.modules["langfuse.langchain"] = langfuse, langchain


def _models(model) -> None:
    from tests.test_graph_end_to_end import _Client
    from tradingagents import e1_routing

    e1_routing.create_tier_client = lambda cfg, tier, **k: _Client(
        model.model_copy(update={"callbacks": k.get("callbacks")}))


def _scripted(**fields):
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from tests.test_graph_end_to_end import ScriptedModel

    block_market = fields.pop("block_market", False)
    slow = fields.pop("slow", 0.0)

    class Model(ScriptedModel):
        model_name: str = "gemini-test"

        def bind_tools(self, tools, **kwargs) -> Runnable:
            return super().bind_tools(tools, **kwargs)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if block_market and "get_stock_data" in {t.name for t in self.tools}:
                with open(os.environ["WORKER_FAKE_MARKER"], "a") as marker:
                    marker.write("blocked\n")
                threading.Event().wait()              # a provider call that never returns
            if slow:
                time.sleep(slow)
            message = super()._generate(messages, stop, run_manager, **kwargs).generations[0].message
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content=message.content, tool_calls=message.tool_calls,
                usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}))])

    return Model(**fields)


def scripted() -> None:
    _offline_vendors()
    _models(_scripted())


def slow_calls() -> None:
    _offline_vendors()
    _models(_scripted(slow=0.3))


def hung_market_call() -> None:
    _offline_vendors()
    _models(_scripted(block_market=True))


# ------------------------------------------------------------------ start-up and watchdog faults
def hang_at_startup() -> None:
    threading.Event().wait()                    # never reports ready


def hang_first_startup() -> None:
    """The first worker to start hangs; later ones start normally (recovery)."""
    marker = os.environ["WORKER_FAKE_MARKER"]
    if not os.path.exists(marker):
        with open(marker, "w") as handle:
            handle.write("hung once\n")
        threading.Event().wait()
    scripted()


def crash_at_startup() -> None:
    raise RuntimeError("simulated import failure")


def freeze_during_call() -> None:
    """The whole process stops (no heartbeat) inside the market analyst's first call."""
    import signal

    _offline_vendors()
    model = _scripted()

    class Freezing(type(model)):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if "get_stock_data" in {t.name for t in self.tools}:
                os.kill(os.getpid(), signal.SIGSTOP)
            return super()._generate(messages, stop, run_manager, **kwargs)

    _models(Freezing())


def slow_langfuse_hung_call() -> None:
    """A hung model call, and a Langfuse whose flush takes far longer than any bound."""
    hung_market_call()

    class SlowClient:
        def flush(self):
            time.sleep(600)

    sys.modules["langfuse"].get_client = SlowClient


def record_startup_side_effects() -> None:
    """Counts model clients built and Langfuse clients made, to show start-up makes neither."""
    from tradingagents import e1_routing

    marker = os.environ["WORKER_FAKE_MARKER"]
    _offline_vendors()

    def note(what):
        with open(marker, "a") as handle:
            handle.write(what + "\n")

    real_get_client = sys.modules["langfuse"].get_client
    sys.modules["langfuse"].get_client = lambda: note("langfuse client") or real_get_client()
    model = _scripted()
    e1_routing.create_tier_client = lambda cfg, tier, **k: note(f"model client {tier}") or __import__(
        "tests.test_graph_end_to_end", fromlist=["_Client"])._Client(
        model.model_copy(update={"callbacks": k.get("callbacks")}))
