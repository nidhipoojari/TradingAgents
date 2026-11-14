"""Offline regression tests for E3 metrics and cost reporting."""

from tradingagents.e3_tracing import E3MetricsCallback


def test_partial_cost_when_primary_model_fails():
    callback = E3MetricsCallback()
    callback.records = [
        {
            "run_id": "successful-fallback",
            "agent": "Neutral Analyst",
            "model": "gemini-3.1-flash-lite",
            "latency_ms": 1500,
            "input_tokens": 1000,
            "output_tokens": 100,
            "error": None,
        },
        {
            "run_id": "failed-primary",
            "agent": "Neutral Analyst",
            "model": "gemini-3.5-flash-lite",
            "latency_ms": 500,
            "input_tokens": None,
            "output_tokens": None,
            "error": "HTTP 429 RESOURCE_EXHAUSTED",
        },
    ]

    pricing = {
        "gemini-3.1-flash-lite": {
            "input_per_million_usd": 0.25,
            "output_per_million_usd": 1.50,
        },
        "gemini-3.5-flash-lite": {
            "input_per_million_usd": 0.30,
            "output_per_million_usd": 2.50,
        },
    }

    result = callback.summary(
        ticker="NVDA",
        analysis_date="2026-10-23",
        status="success",
        wall_time_seconds=10.0,
        pricing=pricing,
    )

    assert result["estimated_cost_usd"] is None
    assert result["known_usage_cost_usd"] == 0.0004
    assert result["cost_complete"] is False
    assert result["priced_llm_calls"] == 1
    assert result["unpriced_llm_calls"] == 1
    assert result["observed_llm_errors"] == 1


def test_market_analyst_attribution():
    callback = E3MetricsCallback()

    callback._start(
        serialized={"kwargs": {"model": "gemini-3.5-flash-lite"}},
        run_id="market-call",
        metadata={"langgraph_node": "Market Analyst"},
    )
    callback._finish(
        run_id="market-call",
        input_tokens=1000,
        output_tokens=100,
    )

    result = callback.summary(
        ticker="NVDA",
        analysis_date="2026-10-23",
        status="success",
        wall_time_seconds=1.0,
    )

    assert "Market Analyst" in result["per_agent"]
    assert "agent" not in result["per_agent"]
    assert result["observed_llm_calls"] == 1
