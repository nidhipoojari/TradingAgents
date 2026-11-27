"""E1 circuit breaker (interactive runs): an overloaded main model is skipped for a cooldown, with every
trip and skip logged, so a persistent 503 costs one failed call instead of one per agent step."""

from __future__ import annotations

import json
import time

import pytest
from langchain_core.messages import HumanMessage

from tests.test_e1_e2_e3_audit import Named
from tests.test_failed_run_reporting import Flaky, _setup
from tests.test_graph_end_to_end import TRADE_DATE, _Client, offline  # noqa: F401
from tradingagents import e1_routing, service
from tradingagents.run_budget import RunBudget


class Overloaded(Named):
    calls_made: list = []
    message: str = "503 UNAVAILABLE: model overloaded"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls_made.append(1)
        raise RuntimeError(self.message)


def _build(tmp_path, monkeypatch, main, backup, seconds=30.0, scope="run-1"):
    models = {"main-m": main, "backup-m": backup}
    monkeypatch.setattr(e1_routing, "create_tier_client", lambda cfg, tier, **k: _Client(
        models[cfg[f"{tier}_think_llm"]].model_copy(update={"callbacks": k.get("callbacks")})))
    config = {"llm_provider": "google", "quick_think_llm": "main-m", "deep_think_llm": "main-m",
              "quick_fallback_llm": "backup-m", "deep_fallback_llm": "backup-m", "results_dir": str(tmp_path),
              "e1_breaker_seconds": seconds, "e1_breaker_scope": scope}
    return config


@pytest.mark.unit
def test_an_overload_opens_the_breaker_and_later_calls_skip_the_main_model(tmp_path, monkeypatch):
    main, backup = Overloaded(model_name="main-m", calls_made=[]), Named(model_name="backup-m")
    config = _build(tmp_path, monkeypatch, main, backup)
    model = e1_routing.build_model(config, "quick")
    for _ in range(3):
        model.invoke([HumanMessage("hi")])
    assert len(main.calls_made) == 1                          # tried once, then skipped
    log = (tmp_path / "e1_fallback_log.txt").read_text()
    assert "circuit open: main-m overloaded" in log and log.count("skipped (circuit open)") == 2
    assert e1_routing.release_breakers("run-1") == {"trips": 1, "skipped_calls": 2}


@pytest.mark.unit
def test_the_breaker_survives_bind_tools_and_is_shared_by_the_tiers(tmp_path, monkeypatch):
    from langchain_core.tools import tool

    @tool
    def get_stock_data(symbol: str) -> str:
        """Prices."""
        return "p"

    main, backup = Overloaded(model_name="main-m", calls_made=[]), Named(model_name="backup-m")
    config = _build(tmp_path, monkeypatch, main, backup, scope="run-2")
    quick, deep = e1_routing.build_model(config, "quick"), e1_routing.build_model(config, "deep")
    bound = quick.bind_tools([get_stock_data])
    assert isinstance(bound, e1_routing.BreakerFallbacks) and bound.breaker is quick.breaker is deep.breaker
    quick.invoke([HumanMessage("hi")])
    deep.invoke([HumanMessage("hi")])                         # the deep tier skips the same overloaded model
    assert len(main.calls_made) == 1
    e1_routing.release_breakers("run-2")


@pytest.mark.unit
def test_a_non_overload_error_does_not_open_it(tmp_path, monkeypatch):
    main = Overloaded(model_name="main-m", calls_made=[], message="400 INVALID_ARGUMENT: bad request")
    config = _build(tmp_path, monkeypatch, main, Named(model_name="backup-m"), scope="run-3")
    model = e1_routing.build_model(config, "quick")
    model.invoke([HumanMessage("hi")])
    model.invoke([HumanMessage("hi")])
    assert len(main.calls_made) == 2 and not model.breaker.is_open()
    e1_routing.release_breakers("run-3")


@pytest.mark.unit
def test_after_the_cooldown_the_main_model_is_tried_again(tmp_path, monkeypatch):
    main = Overloaded(model_name="main-m", calls_made=[])
    config = _build(tmp_path, monkeypatch, main, Named(model_name="backup-m"), seconds=0.2, scope="run-4")
    model = e1_routing.build_model(config, "quick")
    model.invoke([HumanMessage("hi")])
    model.invoke([HumanMessage("hi")])                        # skipped
    time.sleep(0.3)
    model.invoke([HumanMessage("hi")])                        # half-open: tried again
    assert len(main.calls_made) == 2
    e1_routing.release_breakers("run-4")


@pytest.mark.unit
def test_without_the_setting_e1_is_unchanged(tmp_path, monkeypatch):
    main = Overloaded(model_name="main-m", calls_made=[])
    config = _build(tmp_path, monkeypatch, main, Named(model_name="backup-m"), seconds=None)
    model = e1_routing.build_model(config, "quick")
    model.invoke([HumanMessage("hi")])
    model.invoke([HumanMessage("hi")])
    # Phase 2 behaviour for a 503 or a per-minute 429: every call tries the main model. (The CLI's breaker is
    # quota-only: it opens only for a daily/monthly/billing quota, test_e1_quota.)
    assert len(main.calls_made) == 2
    assert service.config_for_source("cli").get("e1_breaker_seconds") is None
    assert service.config_for_source("dashboard")["e1_breaker_seconds"] == 60.0


@pytest.mark.unit
def test_a_persistently_overloaded_primary_now_completes_within_the_budget(tmp_path, monkeypatch, offline):  # noqa: F811
    """Without the breaker every step pays a failed call: 10 calls become 20 and the 15-call limit stops
    the run. With it, one failed call opens the circuit and the run completes in 11."""
    request = _setup(tmp_path, monkeypatch, main=Flaky(model_name="main-m", counter={}, ok_calls=0),
                     backup=Named(model_name="backup-m"), budget=RunBudget(15, 0.04))
    config = dict(request.config, e1_breaker_seconds=30.0)
    result = service.run_analysis(request.__class__(**{**request.__dict__, "config": config}))
    assert result.usage["llm_calls"] == 11 and result.usage["run_budget"]["failed_calls"] == 1
    assert result.usage["e1_breaker"] == {"trips": 1, "skipped_calls": 9}
    events = (tmp_path / "results" / service.FALLBACK_LOG_NAME).read_text()
    assert "circuit open" in events and events.count("skipped (circuit open)") == 9

    no_breaker = _setup(tmp_path / "b", monkeypatch, main=Flaky(model_name="main-m", counter={}, ok_calls=0),
                        backup=Named(model_name="backup-m"), budget=RunBudget(15, 0.04))
    with pytest.raises(service.AnalysisBudgetExceeded):
        service.run_analysis(no_breaker)
    assert json.loads(next((tmp_path / "b" / "e3").glob("*_e3.json")).read_text())["status"] == "budget_exceeded"
