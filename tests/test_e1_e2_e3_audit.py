"""Audit of E1, E2 and E3 behavior on the real graph, through the shared service.

Distinct, named scripted models that report token usage make every claim checkable:
which model each agent used, how many calls, the token totals three ways, the cost.
"""

from __future__ import annotations

import copy
import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable

from tests.test_analysis_service import _fake_langfuse
from tests.test_graph_end_to_end import TRADE_DATE, ScriptedModel, _Client, offline  # noqa: F401
from tradingagents import e1_routing, service
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.e3_tracing import E3MetricsCallback

AGENT_NODES = {   # E1 routing-table key -> the graph node E3 attributes calls to
    "market_analyst": "Market Analyst", "social_analyst": "Sentiment Analyst", "news_analyst": "News Analyst",
    "fundamentals_analyst": "Fundamentals Analyst", "bull_researcher": "Bull Researcher",
    "bear_researcher": "Bear Researcher", "research_manager": "Research Manager", "trader": "Trader",
    "aggressive_analyst": "Aggressive Analyst", "neutral_analyst": "Neutral Analyst",
    "conservative_analyst": "Conservative Analyst", "portfolio_manager": "Portfolio Manager",
}
IN, OUT = 100, 20


class Named(ScriptedModel):
    model_name: str = "model"
    broken: bool = False

    def bind_tools(self, tools, **kwargs) -> Runnable:   # annotated, as real chat models are
        return super().bind_tools(tools, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.broken:
            self.calls.append(1)
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        message = super()._generate(messages, stop, run_manager, **kwargs).generations[0].message
        message = AIMessage(content=message.content, tool_calls=message.tool_calls,
                            usage_metadata={"input_tokens": IN, "output_tokens": OUT, "total_tokens": IN + OUT})
        return ChatResult(generations=[ChatGeneration(message=message)])


def _run(tmp_path, monkeypatch, *, quick="quick-m", deep="deep-m", broken=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    _fake_langfuse(monkeypatch, tmp_path)
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({n: {"input_per_million_usd": 1.0, "output_per_million_usd": 2.0}
                                   for n in {quick, deep, "backup-m"}}))
    monkeypatch.setenv("E3_PRICING_JSON", str(pricing))
    models = {name: Named(model_name=name, broken=broken) for name in {quick, deep}}
    models["backup-m"] = Named(model_name="backup-m")
    monkeypatch.setattr(e1_routing, "create_tier_client", lambda cfg, tier, **k: _Client(
        models[cfg[f"{tier}_think_llm"]].model_copy(update={"callbacks": k.get("callbacks")})))
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
                  memory_log_path=str(tmp_path / "memory.md"), quick_think_llm=quick, deep_think_llm=deep)
    if broken:
        config.update(quick_fallback_llm="backup-m", deep_fallback_llm="backup-m")
    result = service.run_analysis(service.AnalysisRequest(ticker="NVDA", trade_date=TRADE_DATE, config=config))
    return result, json.loads(result.e3_summary_path.read_text()), models


# ------------------------------------------------------------------ E1
@pytest.mark.unit
def test_every_agent_uses_the_tier_the_routing_table_gives_it(tmp_path, monkeypatch, offline):  # noqa: F811
    _, summary, models = _run(tmp_path, monkeypatch)
    per_agent = summary["per_agent"]
    assert "unattributed" not in per_agent
    assert set(per_agent) == set(AGENT_NODES.values())
    for key, node in AGENT_NODES.items():
        expected = "deep-m" if e1_routing.ROUTING_TABLE[key] == "deep" else "quick-m"
        assert per_agent[node]["models"] == [expected], node
    # Two judgment calls on the deep model; everything else on the quick one.
    assert (len(models["deep-m"].calls), len(models["quick-m"].calls)) == (2, 13)


@pytest.mark.unit
def test_call_counts_per_agent(tmp_path, monkeypatch, offline):  # noqa: F811
    _, summary, _ = _run(tmp_path, monkeypatch)
    calls = {agent: row["llm_calls"] for agent, row in summary["per_agent"].items()}
    # A tool-using analyst: one call that asks for tools, one that writes the report.
    for node in ("Market Analyst", "News Analyst", "Fundamentals Analyst"):
        assert calls[node] == 2
    assert all(n == 1 for node, n in calls.items()
               if node not in ("Market Analyst", "News Analyst", "Fundamentals Analyst"))
    assert summary["observed_llm_calls"] == 15


@pytest.mark.unit
def test_the_same_model_on_both_tiers_changes_no_call(tmp_path, monkeypatch, offline):  # noqa: F811
    """With one model for both tiers, routing has nothing to route; it must still cost nothing extra."""
    result, summary, models = _run(tmp_path, monkeypatch, quick="same-m", deep="same-m")
    assert summary["observed_llm_calls"] == 15 and len(models["same-m"].calls) == 15
    assert result.rating == "Overweight"


@pytest.mark.unit
def test_a_fallback_is_one_failed_record_plus_one_backup_record(tmp_path, monkeypatch, offline):  # noqa: F811
    result, summary, models = _run(tmp_path, monkeypatch, broken=True)
    failures = len(models["quick-m"].calls) + len(models["deep-m"].calls)
    assert failures == 15 and len(models["backup-m"].calls) == 15
    assert summary["observed_llm_calls"] == 30 and summary["observed_llm_errors"] == 15   # no double counting
    assert len(result.fallback_events) == 15
    assert result.usage["llm_calls"] == 30     # token_usage counts provider attempts, failures included
    for node in AGENT_NODES.values():
        row = summary["per_agent"][node]
        assert row["errors"] * 2 == row["llm_calls"] and "backup-m" in row["models"]


# ------------------------------------------------------------------ E3
@pytest.mark.unit
def test_tokens_agree_three_ways(tmp_path, monkeypatch, offline):  # noqa: F811
    result, summary, _ = _run(tmp_path, monkeypatch)
    e3_in = sum(row["input_tokens_reported"] for row in summary["per_agent"].values())
    e3_out = sum(row["output_tokens_reported"] for row in summary["per_agent"].values())
    assert (e3_in, e3_out) == (result.usage["tokens_in"], result.usage["tokens_out"]) == (15 * IN, 15 * OUT)


@pytest.mark.unit
def test_cost_estimates_with_and_without_an_e1_fallback(tmp_path, monkeypatch, offline):  # noqa: F811
    expected = round(15 * (IN * 1.0 + OUT * 2.0) / 1_000_000, 6)
    _, clean, _ = _run(tmp_path / "clean", monkeypatch)
    assert clean["estimated_cost_usd"] == clean["estimated_cost_usd_successful_calls"] == expected
    assert clean["failed_calls_without_usage"] == 0

    _, fell_back, _ = _run(tmp_path / "fallback", monkeypatch, broken=True)
    assert fell_back["estimated_cost_usd"] is None            # strict, as in Phase 2
    assert fell_back["failed_calls_without_usage"] == 15
    assert fell_back["estimated_cost_usd_successful_calls"] == fell_back["known_usage_cost_usd"] == expected


@pytest.mark.unit
def test_the_successful_calls_estimate_needs_every_successful_call_priced():
    callback = E3MetricsCallback()
    base = {"agent": "Trader", "latency_ms": 1.0, "start_offset_ms": 0.0}
    callback.records = [
        {**base, "run_id": "a", "model": "priced", "input_tokens": 10, "output_tokens": 5, "error": None},
        {**base, "run_id": "b", "model": "unpriced", "input_tokens": 10, "output_tokens": 5, "error": None},
        {**base, "run_id": "c", "model": "priced", "input_tokens": None, "output_tokens": None, "error": "429"},
    ]
    pricing = {"priced": {"input_per_million_usd": 1.0, "output_per_million_usd": 1.0}}
    summary = callback.summary(ticker="T", analysis_date="2026-01-01", status="success",
                               wall_time_seconds=1.0, pricing=pricing)
    assert summary["failed_calls_without_usage"] == 1
    assert summary["estimated_cost_usd_successful_calls"] is None   # one successful call has no price


@pytest.mark.unit
def test_run_labels_reach_token_usage_e3_and_langfuse_metadata(tmp_path, monkeypatch, offline):  # noqa: F811
    result, summary, _ = _run(tmp_path, monkeypatch)
    usage = json.loads(result.usage_path.read_text())
    assert usage["run_id"] == summary["run_id"] == result.run_id
    assert usage["source"] == summary["source"] == "api"
    assert result.run_id in result.e3_summary_path.name
