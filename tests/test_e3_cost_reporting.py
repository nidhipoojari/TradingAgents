"""E3 cost as the dashboard (F1) and MCP server (F2) report it: a partial or unpriced sum is never
presented as the run's estimated cost. Summaries come from E3's own summary builder."""

from __future__ import annotations

import dataclasses
import json

import pytest

from tests.test_mcp_server import PAST, _stub
from tradingagents import mcp_server, service
from tradingagents.e3_tracing import E3MetricsCallback

PRICES = {"main": {"input_per_million_usd": 1.0, "output_per_million_usd": 2.0},
          "backup": {"input_per_million_usd": 0.5, "output_per_million_usd": 1.0},
          "free": {"input_per_million_usd": 0.0, "output_per_million_usd": 0.0}}


def _summary(calls, pricing=PRICES):
    """calls: (model, input_tokens, output_tokens, error) per LLM call."""
    callback = E3MetricsCallback()
    callback.records = [{"run_id": str(i), "agent": "Trader", "model": model, "start_offset_ms": 0.0,
                         "latency_ms": 1.0, "input_tokens": inp, "output_tokens": out, "error": error}
                        for i, (model, inp, out, error) in enumerate(calls)]
    return callback.summary(ticker="NVDA", analysis_date=PAST, status="success", wall_time_seconds=1.0,
                            pricing=pricing)


CLEAN = [("main", 1000, 100, None), ("main", 2000, 200, None)]
FALLBACK = [("main", None, None, "RateLimitError: 429"), ("backup", 1000, 100, None),
            ("main", None, None, "RateLimitError: 429"), ("backup", 2000, 200, None)]
UNPRICED_BACKUP = [("main", None, None, "RateLimitError: 429"), ("unknown-model", 1000, 100, None)]
PARTLY_PRICED = [("main", 1000, 100, None), ("unknown-model", 2000, 200, None)]


def _dashboard_text(cost):
    from tradingagents.dashboard.app import cost_text

    return cost_text(cost)


def _mcp_answer(tmp_path, summary):
    path = tmp_path / "run_e3.json"
    if summary is not None:
        path.write_text(json.dumps(summary))

    def run(request, on_event=None, cancel=None):
        result = _stub()(request, on_event=on_event, cancel=cancel)
        return dataclasses.replace(result, e3_summary_path=path if summary is not None else None)

    return mcp_server.analyze_stock_sync("NVDA", PAST, run=run)


# ------------------------------------------------------------------ the shared rule
@pytest.mark.unit
def test_every_call_priced_is_the_full_estimate():
    summary = _summary(CLEAN)
    cost = service.e3_cost(summary)
    assert cost["basis"] == "all_calls" and cost["partial_cost_usd"] is None
    assert cost["estimated_cost_usd"] == summary["estimated_cost_usd"] == round((3000 * 1 + 300 * 2) / 1e6, 6)


@pytest.mark.unit
def test_a_zero_cost_run_is_still_an_estimate():
    cost = service.e3_cost(_summary([("free", 1000, 100, None)]))
    assert cost["basis"] == "all_calls" and cost["estimated_cost_usd"] == 0.0


@pytest.mark.unit
def test_an_e1_fallback_uses_the_successful_calls_estimate_and_says_so():
    summary = _summary(FALLBACK)
    assert summary["estimated_cost_usd"] is None                      # strict E3 field, unchanged
    cost = service.e3_cost(summary)
    assert cost["basis"] == "successful_calls"
    assert cost["estimated_cost_usd"] == summary["estimated_cost_usd_successful_calls"] == round(
        (3000 * 0.5 + 300 * 1.0) / 1e6, 6)
    assert "2 failed call(s)" in cost["note"] and "not included" in cost["note"]


@pytest.mark.unit
@pytest.mark.parametrize("calls", [UNPRICED_BACKUP, PARTLY_PRICED], ids=["unpriced-backup", "partly-priced"])
def test_an_unpriced_successful_call_gives_no_estimate(calls):
    summary = _summary(calls)
    cost = service.e3_cost(summary)
    assert cost["basis"] == "incomplete" and cost["estimated_cost_usd"] is None
    assert cost["partial_cost_usd"] == summary["known_usage_cost_usd"]
    assert "no complete estimate" in cost["note"]
    if cost["partial_cost_usd"] is not None:                         # partly priced: a lower bound, labelled
        assert "lower bound, not the cost" in cost["note"]


@pytest.mark.unit
def test_no_pricing_file_gives_no_figure_at_all():
    cost = service.e3_cost(_summary(CLEAN, pricing=None))
    assert cost == {**cost, "basis": "incomplete", "estimated_cost_usd": None, "partial_cost_usd": None}


@pytest.mark.unit
def test_a_phase2_summary_without_the_newer_field_is_not_guessed():
    summary = _summary(FALLBACK)
    summary.pop("estimated_cost_usd_successful_calls")                # written before Phase 3
    assert service.e3_cost(summary)["basis"] == "incomplete"


@pytest.mark.unit
def test_tracing_off_is_unavailable():
    assert service.e3_cost(None)["basis"] == "unavailable"


# ------------------------------------------------------------------ F1: the dashboard line
@pytest.mark.unit
def test_the_dashboard_labels_only_a_computed_estimate():
    full = _dashboard_text(service.e3_cost(_summary(CLEAN)))
    assert full.startswith("**E3 estimated cost:** $0.003600") and "every LLM call" in full
    fell_back = _dashboard_text(service.e3_cost(_summary(FALLBACK)))
    assert "$0.001800" in fell_back and "every successful LLM call" in fell_back
    partial = service.e3_cost(_summary(PARTLY_PRICED))
    line = _dashboard_text(partial)
    assert line.startswith("**E3 estimated cost:** not available")
    assert f"${partial['partial_cost_usd']:.6f} (a lower bound, not the cost)" in line
    assert "not available" in _dashboard_text(service.e3_cost(_summary(UNPRICED_BACKUP)))


# ------------------------------------------------------------------ F2: the MCP answer
@pytest.mark.unit
def test_the_mcp_answer_carries_the_estimate_with_its_basis(tmp_path):
    answer = _mcp_answer(tmp_path, _summary(FALLBACK))
    assert answer["estimated_cost_usd"] == 0.0018 and answer["cost_basis"] == "successful_calls"
    assert answer["partial_cost_usd"] is None and "not included" in answer["cost_note"]


@pytest.mark.unit
@pytest.mark.parametrize("calls", [UNPRICED_BACKUP, PARTLY_PRICED], ids=["unpriced-backup", "partly-priced"])
def test_the_mcp_answer_never_reports_a_partial_sum_as_the_estimate(tmp_path, calls):
    summary = _summary(calls)
    answer = _mcp_answer(tmp_path, summary)
    assert answer["estimated_cost_usd"] is None and answer["cost_basis"] == "incomplete"
    assert answer["partial_cost_usd"] == summary["known_usage_cost_usd"]


@pytest.mark.unit
def test_the_mcp_answer_without_tracing(tmp_path):
    answer = _mcp_answer(tmp_path, None)
    assert (answer["estimated_cost_usd"], answer["cost_basis"]) == (None, "unavailable")
