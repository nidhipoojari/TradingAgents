"""The E1 routing benchmark script, offline: design-only without approval, sound pre-flight,
interleaved schedule, and the metrics it reports. No API calls."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bench():
    spec = importlib.util.spec_from_file_location("e1_routing_benchmark", ROOT / "scripts" / "e1_routing_benchmark.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_without_approval_it_prints_the_design_and_runs_nothing(bench, monkeypatch, capsys):
    from tradingagents import service

    monkeypatch.setattr(service, "run_analysis", lambda *a, **k: pytest.fail("no analysis without approval"))
    assert bench.main(["--deep-model", "gemini-x-pro"]) == 2
    out = capsys.readouterr().out
    assert "NOT RUN" in out and "18 scored analyses (9 per arm)" in out and "blind review" in out


@pytest.mark.unit
def test_preflight_needs_both_prices_and_two_different_models(bench, tmp_path, monkeypatch):
    pricing = tmp_path / "p.json"
    pricing.write_text(json.dumps({"gemini-3.5-flash-lite": {"input_per_million_usd": 0.3,
                                                             "output_per_million_usd": 2.5}}))
    monkeypatch.setenv("E3_PRICING_JSON", str(pricing))
    problems = bench.preflight("gemini-3.5-flash-lite", "gemini-3.5-flash")
    assert any("no price for gemini-3.5-flash" in p for p in problems)
    assert any("must differ" in p for p in bench.preflight("gemini-3.5-flash-lite", "gemini-3.5-flash-lite"))
    assert any("required" in p for p in bench.preflight("gemini-3.5-flash-lite", ""))


@pytest.mark.unit
def test_an_approved_run_with_a_failed_preflight_runs_nothing(bench, monkeypatch, capsys):
    from tradingagents import service

    monkeypatch.setattr(service, "run_analysis", lambda *a, **k: pytest.fail("pre-flight must stop it"))
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert bench.main(["--deep-model", "gemini-x-pro", "--approve-live-llm"]) == 1


@pytest.mark.unit
def test_the_arms_are_interleaved_per_ticker(bench):
    order = bench.schedule(["NVDA", "AAPL"], 3)
    assert order[:6] == [("NVDA", "A"), ("NVDA", "B"), ("NVDA", "B"), ("NVDA", "A"), ("NVDA", "A"), ("NVDA", "B")]
    assert sum(arm == "A" for _, arm in order) == sum(arm == "B" for _, arm in order) == 6


@pytest.mark.unit
def test_decision_completeness(bench):
    full = ("**Rating**: Hold\n\n**Executive Summary**: s\n\n**Investment Thesis**: t\n\n"
            "**Price Target**: 1\n\n**Time Horizon**: 3 months")
    assert bench.completeness(full) == 1.0
    assert bench.completeness("**Rating**: Hold\n\nprose") == 0.2
    assert bench.completeness("") == 0.0


@pytest.mark.unit
def test_metrics_and_summary(bench, tmp_path):
    def summary(model, deep_ms, cost):
        calls = [{"agent": "Market Analyst", "model": "q", "latency_ms": 1000.0, "input_tokens": 100,
                  "output_tokens": 10},
                 {"agent": "Portfolio Manager", "model": model, "latency_ms": deep_ms, "input_tokens": 50,
                  "output_tokens": 5}]
        return {"per_call": calls, "estimated_cost_usd_successful_calls": cost, "observed_llm_errors": 0}

    rows = [{"arm": "A", "ticker": "NVDA", **bench.run_metrics(summary("q", 2000, 0.02), "**Rating**: Buy", r)}
            for r in ("Buy", "Buy", "Hold")]
    rows += [{"arm": "B", "ticker": "NVDA", **bench.run_metrics(summary("d", 9000, None), "", "Buy")}]
    (tmp_path / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    result = bench.analyze(tmp_path)
    assert result["A"]["deep_llm_seconds"]["median"] == 2.0 and result["A"]["cost_usd"]["median"] == 0.02
    assert result["A"]["rating_stability"] == round(2 / 3, 3)
    assert result["B"]["cost_usd"] is None and result["B"]["unpriced_runs"] == 1
    assert rows[0]["tokens_in"] == 150 and rows[-1]["deep_models"] == ["d"]


@pytest.mark.unit
def test_models_are_overridden_per_run_not_configured(bench):
    text = (ROOT / "scripts" / "e1_routing_benchmark.py").read_text()
    assert "set_key" not in text and ".env" not in text.replace(".environ", "")
    assert "DEFAULT_CONFIG[" not in text
