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


SLOW_STARTUP_SECONDS = 4.0


def slow_start_hung_market_call() -> None:
    """A worker that takes 4 s to start (as a cold one can under load), then hangs in its first call."""
    time.sleep(SLOW_STARTUP_SECONDS)
    hung_market_call()


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


# ------------------------------------------------------------------ provider failures (failed-run records)
_CALLS = {"n": 0}


def _after_two_calls(action):
    """Scripted models whose first two calls succeed; every later call runs ``action``."""
    _offline_vendors()
    model = _scripted()

    class Failing(type(model)):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            _CALLS["n"] += 1
            if _CALLS["n"] > 2:
                action()
            return super()._generate(messages, stop, run_manager, **kwargs)

    _models(Failing())


def _unavailable():
    raise RuntimeError("503 UNAVAILABLE: This model is currently experiencing high demand.")


def fail_after_two_calls() -> None:
    """As the live incident: two successful calls, then the provider answers 503 to everything."""
    _after_two_calls(_unavailable)


def config_error_after_two_calls() -> None:
    """A configuration problem found mid-run (as e1_routing raises for an unusable backup provider)."""
    def refuse():
        from tradingagents.e1_routing import FallbackConfigError

        raise FallbackConfigError("quick backup 'claude-x' runs on anthropic, but ANTHROPIC_API_KEY is not set")

    _after_two_calls(refuse)


def value_error_after_two_calls() -> None:
    """A ValueError raised inside the run, after validation: a run failure, not a bad request."""
    def refuse():
        raise ValueError("unexpected value inside the run")

    _after_two_calls(refuse)


def die_after_two_calls() -> None:
    """The worker process itself dies inside its third model call."""
    _after_two_calls(lambda: os._exit(3))


def freeze_after_two_calls() -> None:
    """The whole worker stops (no messages, no heartbeat) inside its third model call: the parent
    has only what the worker sent before, including its last checkpoint."""
    import signal

    def freeze():
        os.kill(os.getpid(), signal.SIGSTOP)
        # A self-sent SIGSTOP can let the sending thread run on briefly (seen under WSL2): block too,
        # so this call can never complete and report back, as in a real freeze.
        threading.Event().wait()

    _after_two_calls(freeze)


# ------------------------------------------------------------------ latency replay (benchmarks)
def replay() -> None:
    """Scripted models that answer with the per-call latencies of a saved real run.

    REPLAY_PROFILE names a JSON file: ``latencies`` (seconds per successful call, in order),
    ``market_tool_rounds`` (1: all tools in one round, as the 10-call runs; 3: one tool per round, as the
    12-call runs), ``data_seconds_per_tool`` (vendor delay; an E2 cache hit skips it), and optionally
    ``main`` / ``backup`` behaviour ("ok", "503", "hang") with ``main_fail_seconds`` /
    ``backup_fail_seconds`` and ``main_fail_calls`` / ``backup_fail_calls`` (only the first N calls fail;
    omitted: every call). Models named in ``backup_name`` play the backup.
    """
    import json

    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from tests.test_graph_end_to_end import ARGS, TEXT, _Client
    from tradingagents import e1_routing
    from tradingagents.dataflows import router

    # Read on first use, not at start-up: a warm spare starts before the next scenario sets
    # REPLAY_PROFILE, and adopts the request's environment only when it takes the request.
    loaded: dict = {}

    def profile() -> dict:
        if not loaded:
            with open(os.environ["REPLAY_PROFILE"], encoding="utf-8") as handle:
                loaded.update(json.load(handle))
        return loaded

    _offline_vendors()

    def vendor(*args, _method="", **kwargs):
        time.sleep(float(profile().get("data_seconds_per_tool", 0)))
        return f"{_method} data"

    for method, vendors in router.VENDOR_METHODS.items():
        for name in list(vendors):
            vendors[name] = lambda *a, _m=method, **k: vendor(*a, _method=_m, **k)
    state = {"i": 0}
    lock = threading.Lock()
    base = type(_scripted())

    class Replay(base):
        role: str = "main"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            settings = profile()
            latencies, rounds = settings["latencies"], int(settings.get("market_tool_rounds", 1))
            behaviour = settings.get(self.role, "ok")
            if behaviour == "hang":
                threading.Event().wait()
            if behaviour == "503":
                with lock:
                    failures = state.setdefault(f"{self.role}_failures", 0)
                    limit = settings.get(f"{self.role}_fail_calls")      # None: every call fails
                    failing = limit is None or failures < int(limit)
                    if failing:
                        state[f"{self.role}_failures"] = failures + 1
                if failing:
                    time.sleep(float(settings.get(f"{self.role}_fail_seconds", 0)))
                    raise RuntimeError("503 UNAVAILABLE: model overloaded (replay)")
            with lock:
                i = state["i"]
                state["i"] += 1
            time.sleep(latencies[min(i, len(latencies) - 1)])
            usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
            if self.tools:
                done = sum(1 for m in messages if getattr(m, "tool_calls", None))
                if done < rounds:
                    tools = list(self.tools) if rounds == 1 else [self.tools[done % len(self.tools)]]
                    calls = [{"name": t.name, "id": f"call_{done}_{n}",
                              "args": {k: v for k, v in ARGS.items()
                                       if k in t.tool_call_schema.model_json_schema()["properties"]}}
                             for n, t in enumerate(tools)]
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(
                        content="", tool_calls=calls, usage_metadata=usage))])
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=TEXT, usage_metadata=usage))])

    main, backup = Replay(model_name="replay-main"), Replay(model_name="replay-backup", role="backup")
    e1_routing.create_tier_client = lambda cfg, tier, **k: _Client(
        (backup if cfg[f"{tier}_think_llm"] == profile().get("backup_name", "replay-backup") else main).model_copy(
            update={"callbacks": k.get("callbacks")}))
