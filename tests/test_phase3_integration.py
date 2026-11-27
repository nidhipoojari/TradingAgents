"""Phase 3 interfaces end to end on the real graph: E1, E2 and E3 reach the dashboard and MCP.

Each test drives the interface's own entry point (the MCP tool over FastMCP's
in-memory client, the Streamlit page through AppTest) with scripted models and
offline vendors. The main model always fails, so every answer comes from the
E1 backup; the E2 cache is on; E3 tracing is on with an offline Langfuse stand-in.
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import json
import time
from pathlib import Path

import pytest
from langchain_core.runnables import Runnable

from tests.test_analysis_service import _fake_langfuse
from tests.test_graph_end_to_end import TRADE_DATE, ScriptedModel, _Client, offline  # noqa: F401
from tradingagents import e1_routing, service
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.memory import TradingMemoryLog


class Provider(ScriptedModel):
    # Real chat models annotate bind_tools; with_fallbacks re-binds the backup only then.
    def bind_tools(self, tools, **kwargs) -> Runnable:
        return super().bind_tools(tools, **kwargs)


class Broken(Provider):
    def _generate(self, *a, **k):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")


@pytest.fixture
def phase2_on(tmp_path, monkeypatch, offline):  # noqa: F811
    """The configured defaults with E1 backups and E2 on, and E3 tracing on."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
                  memory_log_path=str(tmp_path / "memory" / "trading_memory.md"),
                  quick_think_llm="main-model", deep_think_llm="main-model",
                  quick_fallback_llm="backup-model", deep_fallback_llm="backup-model",
                  e2_cache_enabled=True)
    # config_for_source reads the defaults at call time, as each interface does.
    monkeypatch.setattr(service, "DEFAULT_CONFIG", config)
    broken, backup = Broken(), Provider()

    def create(cfg, tier, **kwargs):
        model = broken if cfg[f"{tier}_think_llm"] == "main-model" else backup
        return _Client(model.model_copy(update={"callbacks": kwargs.get("callbacks")}))

    monkeypatch.setattr(e1_routing, "create_tier_client", create)
    _fake_langfuse(monkeypatch, tmp_path)
    return tmp_path


def _summaries(tmp_path):
    return {s["run_id"]: s for s in (json.loads(p.read_text()) for p in (tmp_path / "e3").glob("*_e3.json"))}


def _entries(path):
    return TradingMemoryLog({"memory_log_path": str(path)}).load_entries() if path.exists() else []


def _check_e3(summary, source):
    assert summary["status"] == "success" and summary["source"] == source
    assert summary["observed_llm_calls"] > 0
    # Calls are attributed to agents, not lost as "unattributed", with the run labels added.
    assert set(summary["per_agent"]) - {"unattributed"}
    # The failed main-model calls are recorded as errors (E1 + E3 together).
    assert summary["observed_llm_errors"] > 0


@pytest.mark.unit
def test_mcp_tool_uses_e1_e2_e3_and_its_own_memory_log(phase2_on):
    fastmcp = pytest.importorskip("fastmcp")
    from tradingagents import mcp_server

    async def call():
        async with fastmcp.Client(mcp_server.create_server()) as client:
            args = {"ticker": "NVDA", "trade_date": TRADE_DATE, "analysts": ["market", "news"]}
            first = (await client.call_tool("analyze_stock", args)).data
            second = (await client.call_tool("analyze_stock", args)).data
            return first, second

    first, second = asyncio.run(call())

    assert first["rating"] == second["rating"] == "Overweight"
    assert first["source"] == "mcp" and first["run_id"] != second["run_id"]
    assert first["model_fallbacks"] > 0                                   # E1 backup answered
    assert first["usage"]["e2_cache"]["misses"] > 0                       # E2 cold
    assert second["usage"]["e2_cache"]["misses"] == 0                     # E2 warm, this run only
    summaries = _summaries(phase2_on)                                     # E3, one per run
    assert set(summaries) == {first["run_id"], second["run_id"]}
    for summary in summaries.values():
        _check_e3(summary, "mcp")
    main_log = phase2_on / "memory" / "trading_memory.md"
    assert _entries(main_log.with_name(service.MCP_MEMORY_LOG_NAME))
    assert not _entries(main_log)


@pytest.mark.unit
def test_dashboard_page_runs_an_analysis_with_e1_e2_e3(phase2_on, monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from tradingagents.dashboard import trade
    from tradingagents.execution import alpaca_paper as ap

    for name in (*ap.KEY_ENVS, *ap.SECRET_ENVS):
        monkeypatch.delenv(name, raising=False)
    # This test is about E1/E2/E3 through the page; per-run limits have their own tests
    # (tests/test_run_budget.py). Two analysts with a failing main model exceed the 15-call default.
    monkeypatch.setenv("TRADINGAGENTS_DASHBOARD_MAX_LLM_CALLS", "off")
    monkeypatch.setenv("TRADINGAGENTS_DASHBOARD_MAX_COST_USD", "off")
    page = AppTest.from_file(str(Path(trade.__file__).with_name("app.py")), default_timeout=30).run()
    next(t for t in page.text_input if t.label == "Ticker").input("NVDA")
    page.date_input[0].set_value(datetime.date.fromisoformat(TRADE_DATE))
    page.multiselect[0].set_value(["market", "news"])
    next(b for b in page.button if b.label == "Run analysis").click()
    page.run()

    deadline = time.time() + 60
    while time.time() < deadline and not any(m.label == "Rating" for m in page.metric):
        time.sleep(0.2)
        page.run()
    assert not page.exception
    rating = next(m for m in page.metric if m.label == "Rating")
    assert rating.value == "Overweight"

    [summary] = _summaries(phase2_on).values()
    _check_e3(summary, "dashboard")
    run_dir = phase2_on / "results" / "NVDA" / TRADE_DATE
    usage = json.loads((run_dir / "token_usage.json").read_text())
    assert usage["source"] == "dashboard" and usage["run_id"] == summary["run_id"]
    assert usage["e2_cache"]["misses"] > 0
    assert "switching to backup-model" in (phase2_on / "results" / service.FALLBACK_LOG_NAME).read_text()
    # The dashboard keeps the main memory log.
    assert [e["rating"] for e in _entries(phase2_on / "memory" / "trading_memory.md")] == ["Overweight"]
    # No paper account configured: the trade tab says so and offers nothing to submit.
    assert not [b for b in page.button if b.label == "Submit paper order"]


@pytest.mark.unit
def test_dashboard_paper_order_needs_a_signed_in_user(phase2_on, monkeypatch):
    """Through the page: no sign-in, no order; signed in, one limit order with the user recorded."""
    pytest.importorskip("streamlit")
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    from tests.test_alpaca_paper import FakeData, FakeTrading
    from tradingagents.dashboard import trade
    from tradingagents.execution import alpaca_paper as ap, identity as auth

    users = phase2_on / "users.json"
    users.write_text(json.dumps({"alice": auth.hash_password("correct horse battery")}))
    monkeypatch.setenv(auth.USERS_FILE_ENV, str(users))
    monkeypatch.setenv(auth.AUTH_DB_ENV, str(phase2_on / "auth.sqlite"))
    trading = FakeTrading()
    monkeypatch.setattr(ap.AlpacaPaperBroker, "from_env",
                        classmethod(lambda cls, ledger=None: cls(trading, FakeData(price=100.0), ledger=ledger)))
    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: type("R", (), {"symbol": symbol})())
    # About sign-in and orders; per-run limits have their own tests (tests/test_run_budget.py), and this
    # environment has no pricing file, which a cost limit needs.
    monkeypatch.setenv("TRADINGAGENTS_DASHBOARD_MAX_LLM_CALLS", "off")
    monkeypatch.setenv("TRADINGAGENTS_DASHBOARD_MAX_COST_USD", "off")
    st.cache_resource.clear()

    page = AppTest.from_file(str(Path(trade.__file__).with_name("app.py")), default_timeout=30).run()
    next(t for t in page.text_input if t.label == "Ticker").input("NVDA")
    page.date_input[0].set_value(datetime.date.fromisoformat(TRADE_DATE))
    page.multiselect[0].set_value(["market"])
    next(b for b in page.button if b.label == "Run analysis").click()
    page.run()
    deadline = time.time() + 60
    while time.time() < deadline and not any(m.label == "Rating" for m in page.metric):
        time.sleep(0.2)
        page.run()
    assert next(m for m in page.metric if m.label == "Rating").value == "Overweight"

    def fill_order():
        next(n for n in page.number_input if n.label.startswith("Quantity")).set_value(2)
        page.run()
        assert not page.radio                                       # limit orders only: no choice offered
        next(c for c in page.checkbox if c.label.startswith("The analysis is for")).check()
        next(c for c in page.checkbox if c.label.startswith("I approve a paper limit order")).check()
        page.run()

    def submit_button():
        return next(b for b in page.button if b.label == "Submit paper order")

    fill_order()
    assert submit_button().disabled                                  # signed out: nothing to submit
    assert any("Sign in" in c.value for c in page.caption)

    next(t for t in page.sidebar.text_input if t.label == "Username").input("alice")
    next(t for t in page.sidebar.text_input if t.label == "Password").input("correct horse battery")
    next(b for b in page.sidebar.button if b.label == "Sign in").click()
    page.run()
    assert any("Signed in as **alice**" in s.value for s in page.sidebar.success)
    fill_order()
    assert submit_button().disabled                                  # signed in, but no step-up password yet
    stepup = next(t for t in page.text_input if t.label.startswith("Your password, to approve"))
    stepup.input("wrong password!!")
    page.run()
    submit_button().click()
    page.run()
    assert any("not right" in e.value for e in page.error) and trading.submitted == []   # wrong: nothing sent
    next(t for t in page.text_input if t.label.startswith("Your password, to approve")).input("correct horse battery")
    page.run()
    assert not submit_button().disabled
    submit_button().click()
    page.run()

    assert not page.exception
    [sent] = trading.submitted
    assert (sent.type.value, sent.limit_price, sent.qty) == ("limit", 100.0, 2.0)
    entries = ap.OrderLedger.for_results_dir(phase2_on / "results").entries()
    assert [e["event"] for e in entries] == ["approved", "submitted"]
    assert {(e["approved_by"], e["auth_method"], e["session_bound"]) for e in entries} == {("alice", "password", True)}
    assert not [b for b in page.button if b.label == "Submit paper order"]   # one order per run


@pytest.mark.unit
def test_a_completed_cli_run_record_links_its_e3_summary(phase2_on, monkeypatch, tmp_path):
    """The CLI saves its E3 summary after the report prompts, after the service wrote the run record. Before the
    fix the record named no summary (live CLI run 20261127-113820-f97deed2), so get_run_metrics had no cost or
    per-agent data for any CLI run. Through the real CLI entry, E3 on, E1 backup in use."""
    import cli.display as display
    import cli.main as cli_main
    from cli import run as cli_run
    from cli.models import AnalystType
    from tradingagents import mcp_read_tools, run_store

    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps({m: {"input_per_million_usd": 0.3, "output_per_million_usd": 2.5}
                                   for m in ("main-model", "backup-model")}))
    monkeypatch.setenv("E3_PRICING_JSON", str(pricing))
    buffer = display.MessageBuffer()
    monkeypatch.setattr(display, "message_buffer", buffer)
    monkeypatch.setattr(cli_run, "message_buffer", buffer)
    monkeypatch.setattr(cli_run, "create_layout", lambda: None)
    monkeypatch.setattr(cli_run, "update_display", lambda *a, **k: None)
    monkeypatch.setattr(cli_run, "Live", type("L", (), {"__init__": lambda s, *a, **k: None,
                                                  "__enter__": lambda s: s, "__exit__": lambda s, *a: False}))
    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags=None: {
        "ticker": "NVDA", "analysis_date": TRADE_DATE, "analysts": [AnalystType.MARKET], "asset_type": "stock"})
    monkeypatch.setattr(cli_run, "_build_run_config", lambda s, c: copy.deepcopy(service.DEFAULT_CONFIG))
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *a, **k: "N")
    writer = tmp_path / "cli_cwd"
    writer.mkdir()
    monkeypatch.chdir(writer)

    cli_run.run_analysis()

    results = Path(service.DEFAULT_CONFIG["results_dir"])
    [record_path] = list(run_store.runs_dir(results).glob("*.json"))
    record = json.loads(record_path.read_text())
    assert record["source"] == "cli" and record["status"] == "completed"
    linked = Path(record["e3_summary_path"])
    assert linked.is_absolute() and linked.exists()
    summary = json.loads(linked.read_text())
    assert summary["run_id"] == record["run_id"] and summary["source"] == "cli"
    assert summary["observed_llm_errors"] > 0                       # the main model failed; E1's backup answered
    cost = record["usage"]["e3_cost"]
    assert cost == service.e3_cost(summary)                         # derived from the summary; nothing invented
    assert record["usage"]["llm_calls"] == summary["observed_llm_calls"]

    # Read back by F2 from another working directory, as an MCP server would be.
    elsewhere = tmp_path / "mcp_cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(mcp_read_tools, "results_dir", lambda: results)
    metrics = mcp_read_tools.get_run_metrics(record["run_id"])
    assert metrics["per_agent"] == summary["per_agent"]
    assert metrics["cost"]["basis"] == cost["basis"] and metrics["cost"]["kind"] == "estimate"
    assert metrics["cost"]["estimated_cost_usd"] == cost["estimated_cost_usd"]

    # An older record (no link, or a relative link that no longer resolves) still reads, with no cost invented.
    for legacy_path in (None, "e3_results/not-here_e3.json"):
        legacy = {**record, "run_id": "legacy-" + str(bool(legacy_path)).lower(), "e3_summary_path": legacy_path,
                  "usage": {k: v for k, v in record["usage"].items() if k != "e3_cost"}}
        run_store.save(results, legacy)
        old = mcp_read_tools.get_run_metrics(legacy["run_id"])
        assert old["per_agent"] is None and old["cost"]["estimated_cost_usd"] is None
        assert old["cost"]["basis"] == "unavailable"
