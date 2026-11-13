"""Offline smoke tests for E3 metrics; no model calls, API keys, or Langfuse access."""
import sys
import types
import unittest

# Stub callback only if langchain_core is unavailable in the test environment.
try:
    from langchain_core.callbacks import BaseCallbackHandler
    assert BaseCallbackHandler is not None
except ImportError:
    core = types.ModuleType("langchain_core")
    callback = types.ModuleType("langchain_core.callbacks")
    callback.BaseCallbackHandler = type("BaseCallbackHandler", (), {})
    sys.modules["langchain_core"] = core
    sys.modules["langchain_core.callbacks"] = callback

from tradingagents.e3_tracing import E3MetricsCallback


class DummyResponse:
    llm_output = {"token_usage": {"prompt_tokens": 120, "completion_tokens": 30}}

class TestE3Metrics(unittest.TestCase):
    def test_success_and_failure(self):
        callback = E3MetricsCallback()
        callback.on_llm_start({"kwargs": {"model": "sample-model"}}, ["hello"], run_id="one",
                              metadata={"langgraph_node": "market_analyst"})
        callback.on_llm_end(DummyResponse(), run_id="one")
        callback.on_llm_start({}, ["hello"], run_id="two", metadata={"langgraph_node": "risk_debate"})
        callback.on_llm_error(RuntimeError("rate limit"), run_id="two")
        summary = callback.summary(ticker="NVDA", analysis_date="2026-10-23",
                                   status="success", wall_time_seconds=2.3)
        assert summary["observed_llm_calls"] == 2
        assert summary["observed_llm_errors"] == 1
        assert summary["per_agent"]["market_analyst"]["input_tokens_reported"] == 120
        assert summary["per_agent"]["market_analyst"]["output_tokens_reported"] == 30
        assert summary["per_agent"]["risk_debate"]["errors"] == 1
        assert summary["estimated_cost_usd"] is None

if __name__ == "__main__":
    unittest.main()
