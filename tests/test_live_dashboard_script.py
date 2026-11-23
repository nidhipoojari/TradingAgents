"""scripts/smoke/live_dashboard.py, offline: headless start-up, terminal-state detection, no retry."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def live_dashboard(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts" / "smoke"))
    spec = importlib.util.spec_from_file_location("live_dashboard", ROOT / "scripts" / "smoke" / "live_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_dashboard"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("live_dashboard", None)


# ------------------------------------------------------------------ start-up
@pytest.mark.unit
def test_the_dashboard_starts_headless_with_paper_trading_off(live_dashboard, tmp_path):
    env = live_dashboard.dashboard_env(tmp_path, {"PATH": "/usr/bin", "ALPACA_API_KEY": "real-paper-key"})
    assert env["STREAMLIT_SERVER_HEADLESS"] == "true"              # no first-run e-mail prompt
    assert env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] == "false"
    assert env["ALPACA_API_KEY"] == env["ALPACA_SECRET_KEY"] == ""   # blanked even when the caller has keys
    assert env["TRADINGAGENTS_DASHBOARD_MAX_LLM_CALLS"] == "15" and env["TRADINGAGENTS_DASHBOARD_MAX_COST_USD"] == "0.04"
    assert env["TRADINGAGENTS_E2_CACHE"] == "true" and env["PATH"] == "/usr/bin"
    for key in ("TRADINGAGENTS_RESULTS_DIR", "TRADINGAGENTS_CACHE_DIR", "TRADINGAGENTS_MEMORY_LOG_PATH",
                "E3_RESULTS_DIR", "TRADINGAGENTS_DASHBOARD_USERS", "TRADINGAGENTS_AUTH_DB"):
        assert env[key].startswith(str(tmp_path)), key           # nothing lands in ~/.tradingagents
    assert live_dashboard.dashboard_env(tmp_path, {}, extra={"PYTHONPATH": "/x"})["PYTHONPATH"] == "/x"


@pytest.mark.unit
def test_the_launcher_hands_that_environment_to_the_dashboard(live_dashboard, tmp_path, monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def popen(cmd, cwd=None, env=None, **kwargs):
        seen.update(cmd=cmd, cwd=cwd, env=env, stdin=kwargs.get("stdin"))
        raise Stop

    monkeypatch.setattr(live_dashboard, "preflight", lambda env, python: [])
    monkeypatch.setattr(live_dashboard.subprocess, "run",
                        lambda *a, **k: type("Done", (), {"returncode": 0, "stderr": "", "stdout": ""})())
    monkeypatch.setattr(live_dashboard.os, "chmod", lambda *a: None)
    monkeypatch.setattr(live_dashboard.subprocess, "Popen", popen)
    with pytest.raises(Stop):
        live_dashboard.main(["--approve-live-llm", "--out", str(tmp_path / "run")])
    assert seen["env"]["STREAMLIT_SERVER_HEADLESS"] == "true" and seen["env"]["ALPACA_API_KEY"] == ""
    assert seen["cmd"][0].endswith("tradingagents-dashboard") and seen["cwd"] == live_dashboard.ROOT
    assert seen["stdin"] == live_dashboard.subprocess.DEVNULL       # nothing can wait on a prompt


@pytest.mark.unit
def test_tradingagents_dashboard_itself_is_unchanged():
    """Headless is the test launcher's choice; the interactive launcher keeps Streamlit's defaults."""
    assert "HEADLESS" not in (ROOT / "tradingagents" / "dashboard" / "launch.py").read_text().upper()


@pytest.mark.unit
def test_without_approval_it_only_prints_the_plan(live_dashboard, monkeypatch, capsys):
    def forbidden(*a, **k):
        raise AssertionError("no process or network call without approval")

    monkeypatch.setattr(live_dashboard.subprocess, "Popen", forbidden)
    monkeypatch.setattr(live_dashboard.subprocess, "run", forbidden)
    monkeypatch.setattr(live_dashboard.urllib.request, "urlopen", forbidden)
    assert live_dashboard.main([]) == 2
    out = capsys.readouterr().out
    assert "NOT RUN" in out and "15 calls" in out and "headless" in out


# ------------------------------------------------------------------ terminal states
def _view(**kw):
    return {"has_rating": False, "alerts": [], "cancel_button": False, "run_enabled": False, **kw}


@pytest.mark.unit
@pytest.mark.parametrize("view, state", [
    (_view(has_rating=True, run_enabled=True), "completed"),
    (_view(alerts=["The analysis failed: RuntimeError: GoogleAPIError: 503 UNAVAILABLE."], run_enabled=True), "failed"),
    (_view(alerts=["Stopped by the per-run limit, not by you and not by a model error. analysis X was stopped"]),
     "budget_exceeded"),
    (_view(alerts=["analysis 20261123-113910-4266de44 was cancelled after 2 report(s)"], run_enabled=True),
     "cancelled"),
    (_view(cancel_button=True), None),                                      # still running
    (_view(alerts=["Today's data is always fetched fresh"], cancel_button=True), None),
])
def test_every_terminal_state_is_recognised(live_dashboard, view, state):
    assert live_dashboard.classify(view) == state


@pytest.mark.unit
def test_a_terminated_run_is_never_cancelled(live_dashboard):
    for state in live_dashboard.TERMINAL:
        assert not live_dashboard.should_cancel(state, _view(cancel_button=True))
    assert live_dashboard.should_cancel(None, _view(cancel_button=True))       # still running at the limit
    assert not live_dashboard.should_cancel(None, _view())                    # nothing to click


@pytest.mark.unit
def test_a_failure_is_detected_at_once_not_after_the_time_limit(live_dashboard, monkeypatch):
    views = iter([_view(cancel_button=True), _view(cancel_button=True),
                  _view(alerts=["The analysis failed: RuntimeError: 503 UNAVAILABLE"], run_enabled=True)])
    monkeypatch.setattr(live_dashboard, "_view", lambda page: next(views))
    monkeypatch.setattr(live_dashboard.time, "sleep", lambda s: None)
    state, view = live_dashboard.wait_for_terminal(page=None, timeout=300)
    assert state == "failed" and "503" in view["alerts"][0]
