"""Phase 2 common errors, offline: actionable messages for bad dates, missing E3 packages or credentials,
the wrong virtual environment, unknown models and missing provider keys (`tradingagents doctor`)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cli.prompts import parse_analysis_date
from tradingagents import doctor

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
@pytest.mark.parametrize("given, said", [
    ("2026/10/23", "did you mean 2026-10-23?"),
    ("10-23-2026", "did you mean 2026-10-23?"),
    ("23.10.2026", "did you mean 2026-10-23?"),
    ("05-06-2026", "ambiguous"),
    ("2026-02-30", "2026-02-30 is not a calendar date"),
    ("2026-13-01", "is not a calendar date"),
])
def test_a_malformed_date_says_what_was_probably_meant(given, said):
    with pytest.raises(ValueError, match="use YYYY-MM-DD") as caught:
        parse_analysis_date(given)
    assert said in str(caught.value)


@pytest.mark.unit
def test_good_dates_are_unchanged():
    assert parse_analysis_date(" 2026-10-23 ") == "2026-10-23"
    with pytest.raises(ValueError, match="in the future"):
        parse_analysis_date("2099-01-01")


@pytest.mark.unit
def test_e3_without_langfuse_says_how_to_install_it(monkeypatch):
    from tradingagents.e3_cli_hook import E3Session

    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for key, value in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk",
                       "LANGFUSE_BASE_URL": "http://localhost.invalid"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setitem(sys.modules, "langfuse", None)               # import langfuse -> ImportError
    with pytest.raises(RuntimeError, match=r'pip install "tradingagents\[e3\]".*E3_TRACE_ENABLED=0'):
        E3Session.from_env(ticker="NVDA", analysis_date="2026-10-23")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY")
    with pytest.raises(RuntimeError, match="missing: LANGFUSE_SECRET_KEY.*tradingagents doctor"):
        E3Session.from_env(ticker="NVDA", analysis_date="2026-10-23")


def _levels(checks):
    return {c.name: (c.level, c.detail) for c in checks}


def _all(checks, name):
    return [(c.level, c.detail) for c in checks if c.name == name]


def _config(**extra):
    return {"llm_provider": "google", "quick_think_llm": "gemini-3.5-flash-lite",
            "deep_think_llm": "gemini-3.5-flash-lite", **extra}


@pytest.mark.unit
def test_doctor_reports_missing_keys_by_name_only(tmp_path):
    checks = _levels(doctor.run_checks(_config(), environ={"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "pk-secret"},
                                       project_root=ROOT))
    assert checks["key GOOGLE_API_KEY"][0] == "FAIL" and "add GOOGLE_API_KEY=" in checks["key GOOGLE_API_KEY"][1]
    assert checks["E3 credentials"][0] == "FAIL" and "LANGFUSE_SECRET_KEY" in checks["E3 credentials"][1]
    assert not any("pk-secret" in detail for _, detail in checks.values())       # values are never printed


@pytest.mark.unit
def test_doctor_flags_an_unknown_model_and_a_bad_backup():
    config = _config(quick_think_llm="gemini-does-not-exist", quick_fallback_llm="claude-x",
                     quick_fallback_provider="anthropic")
    found = doctor.run_checks(config, environ={"GOOGLE_API_KEY": "k"}, project_root=ROOT)
    checks = _levels(found)
    assert checks["model quick"][0] == "WARN" and "404 NOT_FOUND" in checks["model quick"][1]
    assert ("FAIL", "quick backup 'claude-x' runs on anthropic, but ANTHROPIC_API_KEY is not set") in \
        _all(found, "backup quick")
    assert checks["provider anthropic"][0] == "OK"                               # its package is a core dependency


@pytest.mark.unit
def test_doctor_detects_the_wrong_install_and_missing_packages(monkeypatch, tmp_path):
    checks = _levels(doctor.run_checks(_config(), environ={"GOOGLE_API_KEY": "k"}, project_root=tmp_path))
    assert checks["install"][0] == "FAIL" and "wrong virtual environment" in checks["install"][1]
    monkeypatch.setattr(doctor, "_has", lambda module: module != "langfuse")
    checks = _levels(doctor.run_checks(_config(), environ={"GOOGLE_API_KEY": "k", "E3_TRACE_ENABLED": "1"},
                                       project_root=ROOT))
    assert checks["package langfuse"][0] == "FAIL" and "tradingagents[e3]" in checks["package langfuse"][1]


@pytest.mark.unit
def test_doctor_explains_the_powershell_policy_on_windows(monkeypatch):
    monkeypatch.setattr(doctor, "_windows", lambda: True)
    checks = _levels(doctor.run_checks(_config(), environ={"GOOGLE_API_KEY": "k"}, project_root=ROOT))
    assert "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned" in checks["powershell"][1]


@pytest.mark.unit
def test_doctor_checks_pricing_covers_the_models(tmp_path):
    prices = tmp_path / "p.json"
    prices.write_text('{"gemini-3.5-flash-lite": {"input_per_million_usd": 0.3, "output_per_million_usd": 2.5}}')
    env = {"GOOGLE_API_KEY": "k", "E3_PRICING_JSON": str(prices)}
    assert _levels(doctor.run_checks(_config(), environ=env, project_root=ROOT))["E3 pricing"][0] == "OK"
    checks = _levels(doctor.run_checks(_config(deep_think_llm="gemini-other"), environ=env, project_root=ROOT))
    assert checks["E3 pricing"] == ("WARN", "no price for gemini-other in p.json")
