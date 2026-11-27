"""Phase 2 limitation 3, offline: optional adaptive routing from recent E3 records."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_e1_breaker import Overloaded
from tests.test_e1_e2_e3_audit import Named
from tests.test_failed_run_reporting import _setup
from tests.test_graph_end_to_end import offline  # noqa: F401
from tradingagents import e1_adaptive, service
from tradingagents.e1_adaptive import Thresholds, decide, plan
from tradingagents.run_budget import RunBudget

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _history(tmp_path, calls, *, model="main-m", age_minutes=10, name="run1"):
    """One E3 summary with ``calls`` = list of (had_error, error_code, latency_ms)."""
    history = tmp_path / "e3"
    history.mkdir(exist_ok=True)
    summary = {"created_at_utc": (NOW - timedelta(minutes=age_minutes)).isoformat(),
               "per_call": [{"model": model, "had_error": e, "error_code": c, "latency_ms": ms} for e, c, ms in calls]}
    (history / f"NVDA_{name}_e3.json").write_text(json.dumps(summary))
    return history


def _config(tmp_path, **extra):
    return {"quick_think_llm": "main-m", "quick_fallback_llm": "backup-m", "deep_think_llm": "deep-m",
            "results_dir": str(tmp_path / "results"), "e1_adaptive_routing": True, **extra}


@pytest.mark.unit
def test_off_by_default_and_without_a_backup():
    from tradingagents.default_config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["e1_adaptive_routing"] is False
    assert plan({"e1_adaptive_routing": False, "results_dir": "/nonexistent"}) == []
    assert decide({"quick_think_llm": "m", "results_dir": "/x"}, "quick", e1_adaptive.Path("/x")) is None


@pytest.mark.unit
def test_too_little_evidence_keeps_the_fixed_table(tmp_path):
    history = _history(tmp_path, [(True, 503, 1000)] * 5)              # 100 % errors, but only 5 calls
    d = decide(_config(tmp_path), "quick", history, now=NOW)
    assert d.action == "keep" and "too little evidence: 5 recent calls" in d.reason


@pytest.mark.unit
def test_a_mostly_overloaded_main_model_is_demoted_with_its_reason(tmp_path):
    history = _history(tmp_path, [(True, 503, 1100)] * 6 + [(True, 429, 900)] * 2 + [(False, None, 2000)] * 4)
    d = decide(_config(tmp_path), "quick", history, now=NOW)
    assert (d.action, d.calls, d.overload_errors, d.error_rate) == ("prefer_backup", 12, 8, 0.667)
    assert "overload error rate 67% >= 50% over the last 12 calls" in d.reason


@pytest.mark.unit
def test_errors_another_model_cannot_avoid_do_not_count(tmp_path):
    history = _history(tmp_path, [(True, 400, 500)] * 8 + [(True, 404, 500)] * 4)
    assert decide(_config(tmp_path), "quick", history, now=NOW).action == "keep"


@pytest.mark.unit
def test_latency_and_cost_never_move_a_tier(tmp_path):
    history = _history(tmp_path, [(False, None, 60_000)] * 20)          # healthy but 60 s per call
    d = decide(_config(tmp_path), "quick", history, now=NOW)
    assert d.action == "keep" and d.median_latency_seconds == 60.0 and "not used to switch" in d.reason


@pytest.mark.unit
def test_old_and_unreadable_records_are_ignored(tmp_path):
    history = _history(tmp_path, [(True, 503, 1000)] * 20, age_minutes=7 * 60)     # outside the 6 h window
    (history / "broken_e3.json").write_text("{not json")
    (history / "partial_e3.json").write_text(json.dumps({"per_call": []}))          # no timestamp
    assert decide(_config(tmp_path), "quick", history, now=NOW).calls == 0


@pytest.mark.unit
def test_hysteresis_and_a_probe_prevent_flapping(tmp_path):
    config = _config(tmp_path)
    log = tmp_path / "results" / e1_adaptive.LOG_NAME
    log.parent.mkdir(parents=True)
    demoted_at = NOW - timedelta(minutes=10)
    log.write_text(json.dumps({"tier": "quick", "main_model": "main-m", "action": "prefer_backup",
                               "decided_at_utc": demoted_at.isoformat()}) + "\n")
    # 30 % errors: below the 50 % that demotes, but above the 20 % that restores -> stays on the backup.
    history = _history(tmp_path, [(True, 503, 1000)] * 3 + [(False, None, 1000)] * 7)
    d = decide(config, "quick", history, now=NOW)
    assert d.action == "prefer_backup" and d.reason.startswith("still demoted")
    # 10 % errors: recovered.
    history = _history(tmp_path, [(True, 503, 1000)] * 1 + [(False, None, 1000)] * 9)
    assert decide(config, "quick", history, now=NOW).reason.startswith("recovered")
    # No new evidence (a demoted model gets no calls), but 30 minutes have passed: probe.
    history.joinpath("NVDA_run1_e3.json").unlink()
    d = decide(config, "quick", history, now=demoted_at + timedelta(minutes=31))
    assert d.action == "keep" and d.reason.startswith("probe")


@pytest.mark.unit
def test_thresholds_must_leave_room_for_hysteresis():
    with pytest.raises(ValueError, match="hysteresis"):
        Thresholds(demote_error_rate=0.3, recover_error_rate=0.3)
    with pytest.raises(ValueError, match="min_calls"):
        Thresholds(min_calls=60, window_calls=50)


@pytest.mark.unit
def test_a_demoted_tier_starts_the_run_on_its_backup_and_records_why(tmp_path, monkeypatch, offline):  # noqa: F811
    main = Overloaded(model_name="main-m", calls_made=[])
    request = _setup(tmp_path, monkeypatch, main=main, backup=Named(model_name="backup-m"), budget=RunBudget(15, 0.04))
    history = tmp_path / "history"
    history.mkdir()
    (history / "x_e3.json").write_text(json.dumps({
        "created_at_utc": datetime.now(UTC).isoformat(),
        "per_call": [{"model": "main-m", "had_error": True, "error_code": 503, "latency_ms": 1000}] * 12}))
    config = dict(request.config, e1_adaptive_routing=True, e1_adaptive_history_dir=str(history))
    result = service.run_analysis(request.__class__(**{**request.__dict__, "config": config}))
    assert main.calls_made == []                                       # never tried: started on the backup
    decisions = {d["tier"]: d for d in result.usage["e1_adaptive"]}
    assert decisions["quick"]["action"] == "prefer_backup" and decisions["deep"]["action"] == "prefer_backup"
    assert any("circuit open: adaptive routing (overload error rate 100%" in e for e in result.fallback_events)
    logged = (tmp_path / "results" / e1_adaptive.LOG_NAME).read_text().splitlines()
    assert len(logged) == 2 and all(json.loads(line)["reason"] for line in logged)
