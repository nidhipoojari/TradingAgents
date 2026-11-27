"""Phase 2 limitation 4, offline: reported tokens, estimates, cached-input pricing, and reconciliation with a
billing export. An estimate is never labelled a charge."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from tradingagents.billing_reconcile import reconcile
from tradingagents.e3_tracing import E3MetricsCallback, _token_details

PRICES = {"m": {"input_per_million_usd": 0.30, "output_per_million_usd": 2.50}}


def _response(inp, out, cached=None, reasoning=None):
    usage = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
    if cached is not None:
        usage["input_token_details"] = {"cache_read": cached}
    if reasoning is not None:
        usage["output_token_details"] = {"reasoning": reasoning}
    return LLMResult(generations=[[ChatGeneration(message=AIMessage("ok", usage_metadata=usage))]])


def _summary(*responses, prices=PRICES):
    cb = E3MetricsCallback()
    for i, response in enumerate(responses):
        cb.on_chat_model_start({"kwargs": {"model": "m"}}, [], run_id=f"r{i}", metadata={"model": "m"})
        cb.on_llm_end(response, run_id=f"r{i}")
    return cb.summary(ticker="NVDA", analysis_date="2026-10-23", status="success", wall_time_seconds=1.0,
                      pricing=prices)


@pytest.mark.unit
def test_provider_reported_detail_is_kept_per_call_and_in_total():
    s = _summary(_response(1000, 200, cached=600, reasoning=50), _response(500, 100))
    assert [(c["cached_input_tokens"], c["reasoning_tokens"]) for c in s["per_call"]] == [(600, 50), (None, None)]
    assert s["tokens_reported"] == {"input": 1500, "output": 300, "cached_input": 600, "reasoning": 50,
                                    "calls_without_usage": 0}


@pytest.mark.unit
def test_openai_style_raw_usage_details_are_read_too():
    raw = SimpleNamespace(llm_output={"token_usage": {"prompt_tokens_details": {"cached_tokens": 7},
                                                      "completion_tokens_details": {"reasoning_tokens": 3}}},
                          generations=[])
    assert _token_details(raw) == (7, 3)
    assert _token_details(None) == (None, None)


@pytest.mark.unit
def test_cached_input_is_discounted_only_when_the_pricing_file_says_so():
    response = _response(1_000_000, 0, cached=600_000)
    assert _summary(response)["estimated_cost_usd"] == 0.30                  # conservative: full input price
    discounted = {"m": {**PRICES["m"], "cached_input_per_million_usd": 0.075}}
    assert _summary(response, prices=discounted)["estimated_cost_usd"] == pytest.approx(0.4 * 0.30 + 0.6 * 0.075)


@pytest.mark.unit
def test_every_cost_figure_says_what_it_is():
    basis = _summary(_response(10, 10))["cost_basis"]
    assert "not a charge" in basis["estimated_cost_usd"]
    assert basis["actual_billing_usd"] is None and basis["actual_billing_status"].startswith("not verified")


def _e3(tmp_path, day, cost, partial=None, name="a"):
    folder = tmp_path / "e3"
    folder.mkdir(exist_ok=True)
    (folder / f"{name}_e3.json").write_text(json.dumps({
        "created_at_utc": f"{day}T15:00:00+00:00", "estimated_cost_usd": cost,
        "estimated_cost_usd_successful_calls": partial}))
    return folder


@pytest.mark.unit
def test_without_a_billing_export_nothing_is_called_actual(tmp_path):
    folder = _e3(tmp_path, "2026-11-23", 0.01)
    _e3(tmp_path, "2026-11-23", None, partial=0.002, name="b")                  # a failed run: partial estimate
    report = reconcile([folder])
    assert report["billed_usd"] is None and report["billing_status"].startswith("not verified")
    assert report["days"] == [{"day": "2026-11-23", "runs": 2, "e3_estimate_usd": 0.012,
                               "runs_without_full_estimate": 1}]


@pytest.mark.unit
def test_a_billing_export_is_compared_day_by_day(tmp_path):
    folder = _e3(tmp_path, "2026-11-23", 0.012)
    _e3(tmp_path, "2026-11-24", 0.003, name="b")
    csv = tmp_path / "billing.csv"
    csv.write_text("Service description,SKU description,Usage start date,Cost ($)\n"
                   "Generative Language API,Input tokens,2026-11-23T00:00:00Z,0.010\n"
                   "Generative Language API,Output tokens,2026-11-23T00:00:00Z,$0.004\n"
                   "Compute Engine,VM,2026-11-23T00:00:00Z,5.00\n"
                   "Generative Language API,Input tokens,2026-11-22T00:00:00Z,0.020\n")
    report = reconcile([folder], csv, filters={"Service description": "generative language"})
    by_day = {r["day"]: r for r in report["days"]}
    assert by_day["2026-11-23"]["billed_usd"] == 0.014 and by_day["2026-11-23"]["difference_usd"] == 0.002
    assert by_day["2026-11-24"]["billed_usd"] == 0.0                         # exports lag
    assert report["billed_usd"] == 0.034 and report["billing_status"].startswith("actual, from the billing export")
    assert "2026-11-22" in report["note"]                                     # billed, but no run recorded here


@pytest.mark.unit
def test_an_unrecognised_export_fails_with_its_columns(tmp_path):
    csv = tmp_path / "odd.csv"
    csv.write_text("when,amount\n2026-11-23,1\n")
    with pytest.raises(ValueError, match="could not find the date column.*Columns: when, amount"):
        reconcile([tmp_path], csv)
    report = reconcile([tmp_path], csv, date_column="when", cost_column="amount")
    assert report["billed_usd"] == 1.0
