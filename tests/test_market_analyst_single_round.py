"""Market Analyst: one parallel tool round, same tools, same verification step.

The three market tools each load their own data, so the analyst can request them
together instead of in three sequential LLM rounds. These tests pin that the
prompt asks for it, that the subgraph executes a parallel round and then reports
after a single further LLM call, and that the verification snapshot still reaches
the model unshortened.
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from tradingagents.agents import tools as agent_tools
from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.graph.analyst_execution import build_analyst_execution_plan
from tradingagents.graph.setup import _analyst_graph

TRADE_DATE = "2026-10-23"
SNAPSHOT = "## Verified market data snapshot for NVDA\n" + "| Close | 210.96 |\n" * 40


class ParallelThenReport(BaseChatModel):
    """First turn: every bound tool at once. Afterwards: the report."""

    tools: tuple = ()
    seen: list = Field(default_factory=list)  # message lists, shared across bound copies

    @property
    def _llm_type(self) -> str:
        return "parallel-then-report"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tools": tuple(tools)})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        if self.tools and not any(isinstance(m, ToolMessage) for m in messages):
            args = {
                "get_stock_data": {"start_date": "2026-06-01", "end_date": TRADE_DATE},
                "get_indicators": {"indicator": "close_50_sma,rsi,atr,vwma", "curr_date": TRADE_DATE},
                "get_verified_market_snapshot": {"curr_date": TRADE_DATE, "look_back_days": 5},
            }
            calls = [{"name": t.name, "id": f"call_{i}", "args": args[t.name]}
                     for i, t in enumerate(self.tools)]
            message = AIMessage(content="", tool_calls=calls)
        else:
            message = AIMessage(content="Market report.")
        return ChatResult(generations=[ChatGeneration(message=message)])


def _system_prompt(model) -> str:
    return next(m.content for m in model.seen[0] if isinstance(m, SystemMessage))


def _run(monkeypatch):
    vendor_calls = []
    monkeypatch.setattr(agent_tools, "route_to_vendor",
                        lambda method, *a: vendor_calls.append((method, a)) or f"{method} {a[1]} data")
    monkeypatch.setattr(agent_tools, "build_verified_market_snapshot", lambda *a: SNAPSHOT)

    model = ParallelThenReport()
    spec = build_analyst_execution_plan(["market"]).specs[0]
    graph = _analyst_graph(spec, create_market_analyst(model), max_tool_rounds=20)
    out = graph.invoke({
        "messages": [HumanMessage("NVDA")],
        "company_of_interest": "NVDA",
        "trade_date": TRADE_DATE,
    })
    return model, out, vendor_calls


def test_prompt_requests_one_parallel_round_and_keeps_verification(monkeypatch):
    model, _, _ = _run(monkeypatch)
    prompt = _system_prompt(model)

    # The old instruction was false (get_indicators loads its own data) and
    # forced three sequential LLM rounds.
    assert "call get_stock_data first" not in prompt
    assert "parallel tool calls" in prompt
    # Verification is still required and still the source of truth.
    assert "get_verified_market_snapshot" in prompt
    assert "source of truth" in prompt
    assert "up to **4 indicators**" in prompt


def test_parallel_round_then_report_in_two_llm_calls(monkeypatch):
    model, out, vendor_calls = _run(monkeypatch)

    assert len(model.seen) == 2
    assert out["market_report"] == "Market report."
    # Every requested indicator still goes to the vendor individually.
    assert [a[1] for m, a in vendor_calls if m == "get_indicators"] == [
        "close_50_sma", "rsi", "atr", "vwma"]
    assert any(m == "get_stock_data" for m, _ in vendor_calls)

    results = {m.name: m.content for m in model.seen[1] if isinstance(m, ToolMessage)}
    assert set(results) == {"get_stock_data", "get_indicators", "get_verified_market_snapshot"}
    assert results["get_verified_market_snapshot"] == SNAPSHOT  # never compacted
