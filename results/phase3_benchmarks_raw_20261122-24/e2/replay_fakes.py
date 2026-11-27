"""Deterministic replay of a real Phase 2 NVDA run, for Phase 2 vs Phase 3 comparisons.

Each model call sleeps the latency recorded for that agent's n-th call in the
reference E3 summary (REPLAY_E3) and reports that call's recorded token usage.
The market analyst makes the recorded number of calls: one tool round per tool,
then its report. Data requests answer offline after DATA_DELAY seconds
(0 = cached files, >0 = live fetch). Works in the Phase 2 and Phase 3 trees and
inside a Phase 3 worker process.
"""
import json, os, sys, threading, time, types

import pandas as pd

TEXT = "Report.\n\n**Rating**: Overweight\n\nFINAL TRANSACTION PROPOSAL: **BUY**"
TOOL_ARGS = {"get_stock_data": {"symbol": "NVDA", "start_date": "2026-09-23", "end_date": "2026-10-23"},
             "get_indicators": {"symbol": "NVDA", "indicator": "close_50_sma,rsi,atr,vwma", "curr_date": "2026-10-23"},
             "get_verified_market_snapshot": {"ticker": "NVDA", "curr_date": "2026-10-23", "symbol": "NVDA"}}


def _delay():
    d = float(os.environ.get("DATA_DELAY") or 0)
    if d:
        time.sleep(d)
    with open(os.environ["DATA_LOG"], "a") as f:
        f.write("request\n")


def install():
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.runnables import Runnable

    from tradingagents import e1_routing
    from tradingagents.agents import context
    from tradingagents.dataflows import router
    from tradingagents.dataflows.vendors.yahoo import market as yahoo_market, snapshot

    reference = json.load(open(os.environ["REPLAY_E3"]))
    by_agent = {}
    for call in reference["per_call"]:
        by_agent.setdefault(call["agent"], []).append(call)
    counters, lock = {}, threading.Lock()

    for method, vendors in router.VENDOR_METHODS.items():
        for vendor in vendors:
            vendors[vendor] = lambda *a, _m=method, **k: _delay() or f"{_m} data " + "x" * 2000
    prices = pd.DataFrame({"Date": pd.bdate_range(end="2026-10-23", periods=260), "Open": 100.0, "High": 101.0,
                           "Low": 99.0, "Close": 100.5, "Volume": 1_000_000})
    snapshot.load_ohlcv = lambda *a, **k: _delay() or prices.copy()
    yahoo_market.yf.Ticker = lambda s: type("T", (), {"info": {"longName": "NVIDIA Corporation"}})()
    context._identity.cache_clear()

    class Handler(BaseCallbackHandler):
        pass

    class Client:
        def flush(self):
            pass

    lf = types.ModuleType("langfuse"); lf.__path__ = []; lf.get_client = Client
    lc = types.ModuleType("langfuse.langchain"); lc.CallbackHandler = Handler
    sys.modules["langfuse"], sys.modules["langfuse.langchain"] = lf, lc

    class Replay(BaseChatModel):
        model_name: str = "gemini-3.5-flash-lite"
        tools: tuple = ()

        @property
        def _llm_type(self):
            return "replay"

        def bind_tools(self, tools, **kwargs) -> Runnable:
            return self.model_copy(update={"tools": tuple(tools)})

        def with_structured_output(self, schema, **kwargs):
            raise NotImplementedError          # free-text path, one call per decision agent

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            agent = (getattr(run_manager, "metadata", None) or {}).get("langgraph_node", "?")
            with lock:
                n = counters.get(agent, 0); counters[agent] = n + 1
            recorded = by_agent.get(agent) or [{"latency_ms": 1000, "input_tokens": 1000, "output_tokens": 100}]
            call = recorded[min(n, len(recorded) - 1)]
            time.sleep(call["latency_ms"] / 1000)
            tools_round = self.tools and n < len(recorded) - 1
            if tools_round:
                tool = self.tools[n % len(self.tools)]
                props = tool.tool_call_schema.model_json_schema()["properties"]
                args = {k: v for k, v in TOOL_ARGS.get(tool.name, {}).items() if k in props}
                message = AIMessage(content="", tool_calls=[{"name": tool.name, "id": f"c{n}", "args": args}])
            else:
                message = AIMessage(content=TEXT)
            message.usage_metadata = {"input_tokens": call["input_tokens"], "output_tokens": call["output_tokens"],
                                      "total_tokens": call["input_tokens"] + call["output_tokens"]}
            return ChatResult(generations=[ChatGeneration(message=message)])

    model = Replay()

    class _Client:
        def __init__(self, m): self.m = m
        def get_llm(self): return self.m

    e1_routing.create_tier_client = lambda cfg, tier, **k: _Client(model.model_copy(update={"callbacks": k.get("callbacks")}))
