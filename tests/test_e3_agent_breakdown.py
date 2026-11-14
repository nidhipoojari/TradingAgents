"""E3 -> E1 routing check: tiers come from E1's table, shares add up, failures are skipped."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "e3_agent_breakdown", Path(__file__).resolve().parents[1] / "scripts" / "e3_agent_breakdown.py")
breakdown_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(breakdown_module)

PRICING = {"main": {"input_per_million_usd": 1.0, "output_per_million_usd": 1.0},
           "backup": {"input_per_million_usd": 1.0, "output_per_million_usd": 1.0}}


def _call(agent, latency_ms, tokens, model="main", error=False):
    return {"agent": agent, "model": model, "latency_ms": latency_ms, "had_error": error,
            "input_tokens": None if error else tokens, "output_tokens": None if error else 0}


def test_tiers_shares_and_failed_calls():
    summary = {"per_call": [
        _call("Market Analyst", 3000, 600_000),
        _call("Portfolio Manager", 10, None, error=True),   # failed: excluded
        _call("Portfolio Manager", 1000, 400_000, model="backup"),
    ]}
    result = breakdown_module.breakdown([summary], PRICING)

    rows = {r["agent"]: r for r in result["agents"]}
    assert rows["Market Analyst"]["tier"] == "quick"
    assert rows["Portfolio Manager"]["tier"] == "deep"
    assert rows["Market Analyst"]["latency_share_pct"] == 75.0
    assert rows["Portfolio Manager"]["cost_share_pct"] == 40.0
    assert result["tiers"]["deep"]["cost_share_pct"] == 40.0
    assert result["backup_model_calls"] == 1
    assert result["verdict"]["largest_agent_on_quick_tier"] is True
