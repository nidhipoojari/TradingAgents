"""Regression tests for two bugs found in the 2026-11-24 live dashboard run (run 20261124-084055-53b8fbf5):

1. "E1 backup-model switches: 3" when one call had moved to the backup: the dashboard (and the MCP server)
   counted every E1 log line, including the circuit-open notice and the backup's own failure.
2. The circuit-open line said "deep tier" for a quick-tier failure: the breaker, shared by both tiers,
   logged the tier that created it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from tests.test_e1_breaker import Overloaded, _build
from tests.test_e1_e2_e3_audit import Named
from tradingagents import e1_routing, mcp_server, service

# The three lines the live run wrote, verbatim apart from the shortened error text.
LIVE_2026_11_24 = [
    "2026-11-24 08:40:59 | quick tier | gemini-3.5-flash-lite failed (GoogleAPIError: 503 UNAVAILABLE. {'error': "
    "{'code': 503, 'message': 'This model is currently experiencing high demand.) -> switching to gemini-3.1-flash-lite",
    "2026-11-24 08:40:59 | deep tier | circuit open: gemini-3.5-flash-lite overloaded (GoogleAPIError); its calls "
    "go to gemini-3.1-flash-lite for 60 s",
    "2026-11-24 08:41:01 | quick tier | BACKUP gemini-3.1-flash-lite also failed (GoogleAPIError: 503 UNAVAILABLE. "
    "{'error': {'code': 503, 'message': 'This model is currently experiencing high demand.) -> run stops",
]
SKIP = "2026-11-24 08:41:05 | deep tier | gemini-3.5-flash-lite skipped (circuit open) -> gemini-3.1-flash-lite"


# ------------------------------------------------------------------ bug 1: counting
@pytest.mark.unit
def test_the_live_runs_log_is_one_switch_not_three():
    counts = e1_routing.summarize_events(LIVE_2026_11_24)
    assert counts == {"backup_calls": 1, "switches": 1, "breaker_skips": 0, "circuit_opens": 1,
                      "backup_failures": 1, "other": 0}
    assert e1_routing.describe_events(LIVE_2026_11_24) == "1 (circuit opened 1x; backup failed 1x)"


@pytest.mark.unit
def test_every_kind_of_e1_line_is_classified():
    lines = [*LIVE_2026_11_24, SKIP, SKIP, "something unexpected"]
    counts = e1_routing.summarize_events(lines)
    assert (counts["switches"], counts["breaker_skips"], counts["backup_calls"]) == (1, 2, 3)
    assert (counts["circuit_opens"], counts["backup_failures"], counts["other"]) == (1, 1, 1)
    assert e1_routing.describe_events(lines) == ("3 (1 after a failed call, 2 skipped by the circuit breaker; "
                                                 "circuit opened 1x; backup failed 1x)")
    assert e1_routing.describe_events([SKIP]) == "1 (skipped by the circuit breaker)"
    assert e1_routing.describe_events([]) == e1_routing.describe_events(None) == "none"


@pytest.mark.unit
def test_an_error_text_that_looks_like_another_kind_does_not_change_the_count():
    # A backup failure whose message happens to contain "-> switching to" and " | " is still a backup failure.
    tricky = ("2026-11-24 08:41:01 | quick tier | BACKUP b also failed (Error: upstream | -> switching to x) "
              "-> run stops")
    assert e1_routing.summarize_events([tricky])["backup_failures"] == 1
    assert e1_routing.summarize_events([tricky])["backup_calls"] == 0


@pytest.mark.unit
def test_the_dashboard_failure_view_shows_the_true_count():
    from tradingagents.dashboard import app

    text = "\n".join(app.failure_lines("20261124-084055-53b8fbf5", {
        "llm_calls": 3, "llm_calls_succeeded": 1, "llm_calls_failed": 2, "tokens_in": 1853, "tokens_out": 140,
        "e1_fallback_events": LIVE_2026_11_24}))
    assert "**E1 backup-model switches:** 1 (circuit opened 1x; backup failed 1x)" in text
    assert all(f"- {line}" in text for line in LIVE_2026_11_24)          # the lines themselves still shown


class _Recorder:
    """Stands in for streamlit in _metrics: records markdown and captions."""

    def __init__(self):
        self.markdown_calls, self.captions = [], []

    def columns(self, n):
        return [type("Col", (), {"metric": lambda *a, **k: None})() for _ in range(n)]

    def markdown(self, text, **kwargs):
        self.markdown_calls.append(text)

    def caption(self, text, **kwargs):
        self.captions.append(text)


@pytest.mark.unit
def test_the_dashboard_success_view_shows_the_true_count(monkeypatch):
    from tradingagents.dashboard import app

    recorder = _Recorder()
    monkeypatch.setattr(app, "st", recorder)
    events = [LIVE_2026_11_24[0], LIVE_2026_11_24[1], SKIP]       # recovered: one switch, then one skip
    result = service.AnalysisResult(
        run_id="r", source="dashboard", ticker="NVDA", trade_date="2026-10-23", asset_type="stock",
        analysts=("market",), rating="Hold", final_state={}, usage={"tokens_in": 1, "tokens_out": 1},
        results_dir=Path("."), usage_path=Path("token_usage.json"), fallback_events=events)
    app._metrics(result)
    assert ("**E1 backup-model switches this run:** 2 (1 after a failed call, 1 skipped by the circuit breaker; "
            "circuit opened 1x)") in recorder.markdown_calls
    assert recorder.captions[:3] == events


@pytest.mark.unit
def test_the_mcp_answer_and_partial_metrics_count_backup_calls():
    events = [*LIVE_2026_11_24[:2], SKIP]
    result = service.AnalysisResult(
        run_id="r", source="mcp", ticker="NVDA", trade_date="2026-10-23", asset_type="stock",
        analysts=("market",), rating="Hold", final_state={"final_trade_decision": "**Rating**: Hold"},
        usage={}, results_dir=Path("."), usage_path=Path("token_usage.json"), fallback_events=events)
    answer = mcp_server.summarize(result)
    assert answer["model_fallbacks"] == 2 and answer["e1_events"]["circuit_opens"] == 1

    failed = RuntimeError("503 UNAVAILABLE")
    failed.analysis_failure = {"run_id": "r", "llm_calls": 3, "e1_fallback_events": LIVE_2026_11_24}
    partial = mcp_server.failure_details(failed, "r")["partial_metrics"]
    assert partial["e1_fallback_switches"] == 1 and partial["e1_events"]["backup_failures"] == 1


# ------------------------------------------------------------------ bug 2: the shared breaker's tier
def _lines(tmp_path):
    return (tmp_path / "e1_fallback_log.txt").read_text().splitlines()


@pytest.mark.unit
def test_a_quick_tier_failure_is_logged_as_quick_even_when_deep_created_the_breaker(tmp_path, monkeypatch):
    main = Overloaded(model_name="main-m", calls_made=[])
    config = _build(tmp_path, monkeypatch, main, Named(model_name="backup-m"), scope="tier-1")
    deep = e1_routing.build_model(config, "deep")            # deep first: it creates the shared breaker
    quick = e1_routing.build_model(config, "quick")
    assert quick.breaker is deep.breaker

    quick.invoke([HumanMessage("hi")])                       # quick fails -> switch + circuit open
    deep.invoke([HumanMessage("hi")])                        # deep is skipped by the open circuit
    switch, opened, skipped = _lines(tmp_path)
    assert "| quick tier | main-m failed" in switch
    assert "| quick tier | circuit open: main-m overloaded" in opened        # was "deep tier" before the fix
    assert "| deep tier | main-m skipped (circuit open)" in skipped
    assert e1_routing.summarize_events([switch, opened, skipped])["backup_calls"] == 2
    e1_routing.release_breakers("tier-1")


@pytest.mark.unit
def test_a_deep_tier_failure_is_logged_as_deep_when_quick_created_the_breaker(tmp_path, monkeypatch):
    main = Overloaded(model_name="main-m", calls_made=[])
    config = _build(tmp_path, monkeypatch, main, Named(model_name="backup-m"), scope="tier-2")
    quick = e1_routing.build_model(config, "quick")          # quick first this time
    deep = e1_routing.build_model(config, "deep")
    deep.invoke([HumanMessage("hi")])
    quick.invoke([HumanMessage("hi")])
    switch, opened, skipped = _lines(tmp_path)
    assert "| deep tier | main-m failed" in switch and "| deep tier | circuit open:" in opened
    assert "| quick tier | main-m skipped (circuit open)" in skipped
    assert len(main.calls_made) == 1                         # routing unchanged: one failed call, then skips
    assert e1_routing.release_breakers("tier-2") == {"trips": 1, "skipped_calls": 1}
