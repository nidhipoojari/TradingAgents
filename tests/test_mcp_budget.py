"""F2: per-run LLM-call and estimated-cost limits for MCP analyses (offline, scripted models).

The limits come from the server's environment only (TRADINGAGENTS_MCP_MAX_LLM_CALLS / _MAX_COST_USD); the
tool has no argument that could raise them. The suite's conftest switches the MCP cost limit off (it clears
E3 pricing); the tests here put it back.
"""

import asyncio
import json
import re

import pytest

from tests.test_graph_end_to_end import offline  # noqa: F401
from tests.test_phase3_integration import TRADE_DATE, phase2_on  # noqa: F401
from tradingagents import mcp_server, run_budget, service

PRICES = {"main-model": {"input_per_million_usd": 0.3, "output_per_million_usd": 2.5},
          "backup-model": {"input_per_million_usd": 0.3, "output_per_million_usd": 2.5}}


@pytest.fixture
def default_limits(tmp_path, monkeypatch):
    """The shipped defaults: both MCP variables unset, with a pricing file."""
    monkeypatch.delenv(run_budget.MCP_CALLS_ENV, raising=False)
    monkeypatch.delenv(run_budget.MCP_COST_ENV, raising=False)
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps(PRICES))
    monkeypatch.setenv("E3_PRICING_JSON", str(prices))
    return prices


@pytest.mark.unit
def test_every_mcp_request_gets_conservative_limits_by_default(default_limits):
    market = mcp_server.build_request("NVDA", TRADE_DATE, ["market"]).budget
    assert (market.max_llm_calls, market.max_cost_usd) == (15, 0.04)          # as the dashboard: 10 expected x 1.5
    every = mcp_server.build_request("NVDA", TRADE_DATE).budget
    assert (every.max_llm_calls, every.max_cost_usd) == (24, 0.04)            # 16 expected x 1.5
    assert service.AnalysisRequest(ticker="NVDA", trade_date=TRADE_DATE, source="cli").budget is None  # CLI unchanged


@pytest.mark.unit
@pytest.mark.parametrize("calls, cost, expected", [
    ("8", "0.02", (8, 0.02)),
    ("off", "0.02", (None, 0.02)),
    ("8", "off", (8, None)),
])
def test_the_server_operator_sets_or_switches_off_each_limit(default_limits, monkeypatch, calls, cost, expected):
    monkeypatch.setenv(run_budget.MCP_CALLS_ENV, calls)
    monkeypatch.setenv(run_budget.MCP_COST_ENV, cost)
    budget = mcp_server.build_request("NVDA", TRADE_DATE, ["market"]).budget
    assert (budget.max_llm_calls, budget.max_cost_usd) == expected


@pytest.mark.unit
def test_both_off_means_no_budget(default_limits, monkeypatch):
    monkeypatch.setenv(run_budget.MCP_CALLS_ENV, "off")
    monkeypatch.setenv(run_budget.MCP_COST_ENV, "0")
    assert mcp_server.build_request("NVDA", TRADE_DATE, ["market"]).budget is None


@pytest.mark.unit
@pytest.mark.parametrize("name, value, said", [
    (run_budget.MCP_CALLS_ENV, "fifteen", "is not a number"),
    (run_budget.MCP_COST_ENV, "$0.04", "is not a number"),
    (run_budget.MCP_COST_ENV, "-1", "positive amount"),
])
def test_a_malformed_limit_is_a_server_setting_error_never_no_limit(default_limits, monkeypatch, name, value, said):
    monkeypatch.setenv(name, value)
    with pytest.raises(mcp_server.ServerSettingError, match=said):
        mcp_server.build_request("NVDA", TRADE_DATE, ["market"])


@pytest.mark.unit
def test_a_cost_limit_without_prices_refuses_before_any_run(default_limits, monkeypatch):
    monkeypatch.delenv("E3_PRICING_JSON")
    ran = []
    with pytest.raises(mcp_server.ServerSettingError, match="TRADINGAGENTS_MCP_MAX_COST_USD=off"):
        mcp_server.analyze_stock_sync("NVDA", TRADE_DATE, ["market"], run=lambda *a, **k: ran.append(a))
    assert ran == []


@pytest.mark.unit
def test_the_client_cannot_change_the_limits():
    fastmcp = pytest.importorskip("fastmcp")

    async def schema():
        async with fastmcp.Client(mcp_server.create_server(run=lambda *a, **k: None)) as client:
            return next(t for t in await client.list_tools() if t.name == "analyze_stock")

    tool = asyncio.run(schema())
    assert set(tool.inputSchema["properties"]) == {"ticker", "trade_date", "analysts", "include_reports"}
    assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False


def _call(args):
    fastmcp = pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    async def call():
        async with fastmcp.Client(mcp_server.create_server()) as client:
            try:
                return (await client.call_tool("analyze_stock", args)).data
            except ToolError as exc:
                return str(exc)
    return asyncio.run(call())


@pytest.mark.unit
def test_a_bad_setting_is_reported_over_the_protocol_as_a_server_setting(phase2_on, monkeypatch):  # noqa: F811
    monkeypatch.setenv(run_budget.MCP_CALLS_ENV, "lots")
    text = _call({"ticker": "NVDA", "trade_date": TRADE_DATE, "analysts": ["market"]})
    assert text.startswith("invalid server setting: TRADINGAGENTS_MCP_MAX_LLM_CALLS='lots' is not a number")


@pytest.mark.unit
def test_the_call_limit_stops_a_real_mcp_run_with_its_accounting(phase2_on, monkeypatch):  # noqa: F811
    monkeypatch.setenv(run_budget.MCP_CALLS_ENV, "4")
    text = _call({"ticker": "NVDA", "trade_date": TRADE_DATE, "analysts": ["market"]})
    assert isinstance(text, str), "the run should have been stopped by its limit"
    assert re.match(r"analysis stopped \[budget_exceeded\] run \d{8}-\d{6}-[0-9a-f]{8}: ", text)
    partial = json.loads(text.split("partial metrics: ", 1)[1])
    assert partial["run_budget"]["max_llm_calls"] == 4 and partial["run_budget"]["exhausted"]
    assert partial["run_budget"]["llm_calls_started"] <= 4                   # never past the limit
    assert "Traceback" not in text and str(phase2_on) not in text            # no paths or stack traces


@pytest.mark.unit
def test_the_cost_limit_stops_a_real_mcp_run(phase2_on, monkeypatch):  # noqa: F811
    prices = phase2_on / "prices.json"
    prices.write_text(json.dumps(PRICES))
    monkeypatch.setenv("E3_PRICING_JSON", str(prices))
    monkeypatch.setenv(run_budget.MCP_COST_ENV, "0.0001")                     # below one call's reservation
    text = _call({"ticker": "NVDA", "trade_date": TRADE_DATE, "analysts": ["market"]})
    assert isinstance(text, str) and text.startswith("analysis stopped [budget_exceeded]")
    partial = json.loads(text.split("partial metrics: ", 1)[1])
    assert partial["run_budget"]["max_cost_usd"] == 0.0001 and partial["run_budget"]["exhausted"]


@pytest.mark.unit
def test_a_run_within_its_limits_answers_as_before_and_shows_them(phase2_on, monkeypatch):  # noqa: F811
    prices = phase2_on / "prices.json"
    prices.write_text(json.dumps(PRICES))
    monkeypatch.setenv("E3_PRICING_JSON", str(prices))
    # These scripted models report no token usage, so every call keeps its full reservation (prompt + 2,000
    # output tokens, about $0.005): unreported is never free. Usage-reporting models stay under $0.04 (the
    # evaluation matrix's mcp cells); here a higher ceiling lets the run finish.
    monkeypatch.setenv(run_budget.MCP_COST_ENV, "0.50")
    answer = _call({"ticker": "NVDA", "trade_date": TRADE_DATE, "analysts": ["market"]})
    assert isinstance(answer, dict), answer
    assert answer["rating"] == "Overweight" and answer["source"] == "mcp"
    limits = answer["run_budget"]
    assert (limits["max_llm_calls"], limits["max_cost_usd"], limits["exhausted"]) == (15, 0.5, False)
    assert limits["llm_calls_started"] == answer["usage"]["llm_calls"] <= 15
