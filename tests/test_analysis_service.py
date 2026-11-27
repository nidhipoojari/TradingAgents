"""Phase 3 shared analysis service: one run path for the CLI, the dashboard and MCP.

Offline: the scripted model and vendors from test_graph_end_to_end stand in for
the providers. These pin what every interface relies on: the decision, the files
a run writes, the run ID and source labels, the separate MCP memory log, E1/E2/E3
still working through the service, one run per process, and cancellation.
"""

from __future__ import annotations

import copy
import json
import sys
import threading
import types

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import Runnable

from tests.test_graph_end_to_end import TRADE_DATE, ScriptedModel, _Client, offline  # noqa: F401
from tradingagents import e1_routing, service
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.memory import TradingMemoryLog

CLI_USAGE_KEYS = ["llm_calls", "tool_calls", "tokens_in", "tokens_out", "llm_provider",
                  "quick_think_llm", "deep_think_llm", "elapsed_seconds", "tokens_total"]


def _config(tmp_path, **extra):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
               memory_log_path=str(tmp_path / "memory" / "trading_memory.md"), **extra)
    return cfg


def _models(monkeypatch, model):
    monkeypatch.setattr(e1_routing, "create_tier_client", lambda config, tier, **k: _Client(model))


def _request(tmp_path, source="api", **extra):
    config = service.config_for_source(source, _config(tmp_path, **extra.pop("config", {})))
    return service.AnalysisRequest(ticker="NVDA", trade_date=TRADE_DATE, source=source,
                                   config=config, **extra)


@pytest.mark.unit
def test_a_service_run_reaches_a_recorded_decision(tmp_path, monkeypatch, offline, capsys):  # noqa: F811
    _models(monkeypatch, ScriptedModel())
    events = []

    result = service.run_analysis(_request(tmp_path, source="dashboard"), on_event=events.append)

    assert result.rating == "Overweight" and not result.is_review
    assert result.source == "dashboard" and result.run_id
    assert result.decision_text.strip()
    # The same files the CLI path wrote before Phase 3, and no others from the service.
    usage = json.loads(result.usage_path.read_text())
    assert list(usage)[:len(CLI_USAGE_KEYS)] == CLI_USAGE_KEYS
    assert usage["run_id"] == result.run_id and usage["source"] == "dashboard"
    assert result.usage_path == tmp_path / "results" / "NVDA" / TRADE_DATE / "token_usage.json"
    assert list((tmp_path / "results" / "NVDA" / TRADE_DATE).iterdir()) == [result.usage_path]
    assert (tmp_path / "results" / "NVDA" / "TradingAgentsStrategy_logs"
            / f"full_states_log_{TRADE_DATE}.json").exists()
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory" / "trading_memory.md")})
    assert [e["rating"] for e in log.load_entries()] == ["Overweight"]
    # Events carry the run's progress; the last one says it is done.
    steps = [e for e in events if e.kind == "step"]
    assert steps and all(e.run_id == result.run_id for e in steps)
    done, total, label = steps[-1].progress
    assert (done, label) == (total, "Done")
    # MCP's stdio transport owns stdout: a run must print nothing there.
    assert capsys.readouterr().out == ""


@pytest.mark.unit
def test_every_run_gets_its_own_id(tmp_path, monkeypatch, offline):  # noqa: F811
    _models(monkeypatch, ScriptedModel())
    first = service.run_analysis(_request(tmp_path))
    second = service.run_analysis(_request(tmp_path))
    assert first.run_id != second.run_id
    # A repeat run on the same ticker and date replaces its usage file instead of adding one.
    assert first.usage_path == second.usage_path
    assert json.loads(second.usage_path.read_text())["run_id"] == second.run_id


@pytest.mark.unit
def test_mcp_runs_keep_a_memory_log_of_their_own(tmp_path, monkeypatch, offline):  # noqa: F811
    _models(monkeypatch, ScriptedModel())
    main_log = tmp_path / "memory" / "trading_memory.md"

    result = service.run_analysis(_request(tmp_path, source="mcp"))

    mcp_log = main_log.with_name(service.MCP_MEMORY_LOG_NAME)
    assert result.rating == "Overweight"
    assert [e["rating"] for e in TradingMemoryLog({"memory_log_path": str(mcp_log)}).load_entries()] == ["Overweight"]
    assert not main_log.exists() or not TradingMemoryLog({"memory_log_path": str(main_log)}).load_entries()


@pytest.mark.unit
def test_the_mcp_memory_log_can_be_named(tmp_path, monkeypatch):
    monkeypatch.setenv(service.MCP_MEMORY_LOG_ENV, str(tmp_path / "elsewhere.md"))
    base = _config(tmp_path)
    assert service.config_for_source("mcp", base)["memory_log_path"] == str(tmp_path / "elsewhere.md")
    # The dashboard and the CLI keep the configured log.
    assert service.config_for_source("dashboard", base)["memory_log_path"] == base["memory_log_path"]
    assert service.config_for_source("cli", base)["memory_log_path"] == base["memory_log_path"]


@pytest.mark.unit
def test_e1_backup_model_answers_through_the_service(tmp_path, monkeypatch, offline):  # noqa: F811
    class Provider(ScriptedModel):
        # Real chat models annotate bind_tools as returning a Runnable; LangChain's
        # with_fallbacks only re-binds tools on the backup for methods that say so.
        def bind_tools(self, tools, **kwargs) -> Runnable:
            return super().bind_tools(tools, **kwargs)

    class Broken(Provider):
        def _generate(self, *a, **k):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    broken, backup = Broken(), Provider()

    def create(config, tier, **kwargs):
        model = broken if config[f"{tier}_think_llm"] == "main-model" else backup
        return _Client(model.model_copy(update={"callbacks": kwargs.get("callbacks")}))

    monkeypatch.setattr(e1_routing, "create_tier_client", create)
    request = _request(tmp_path, config={
        "quick_think_llm": "main-model", "deep_think_llm": "main-model",
        "quick_fallback_llm": "backup-model", "deep_fallback_llm": "backup-model"})

    result = service.run_analysis(request)

    assert result.rating == "Overweight"
    assert result.fallback_events and all("switching to backup-model" in line
                                          for line in result.fallback_events)


@pytest.mark.unit
def test_e2_counts_are_this_runs_own(tmp_path, monkeypatch, offline):  # noqa: F811
    _models(monkeypatch, ScriptedModel())
    request = _request(tmp_path, config={"e2_cache_enabled": True})

    cold = service.run_analysis(request).usage["e2_cache"]
    warm = service.run_analysis(request).usage["e2_cache"]

    assert cold["misses"] > 0
    # The second run answers from the cache, and its numbers leave the first run's out.
    assert warm["misses"] == 0 and warm["hits"] == cold["hits"] + cold["misses"]
    assert warm["hit_rate_pct"] == 100.0


def _fake_langfuse(monkeypatch, tmp_path):
    class Client:
        flushes = 0

        def flush(self):
            Client.flushes += 1

    class Handler(BaseCallbackHandler):
        pass

    langfuse = types.ModuleType("langfuse")
    langfuse.__path__ = []
    langfuse.get_client = Client
    langchain = types.ModuleType("langfuse.langchain")
    langchain.CallbackHandler = Handler
    monkeypatch.setitem(sys.modules, "langfuse", langfuse)
    monkeypatch.setitem(sys.modules, "langfuse.langchain", langchain)
    for name, value in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "offline",
                        "LANGFUSE_SECRET_KEY": "offline", "LANGFUSE_BASE_URL": "http://localhost.invalid",
                        "E3_RESULTS_DIR": str(tmp_path / "e3")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("E3_PRICING_JSON", raising=False)

    hooks = {"registered": [], "unregistered": []}
    from tradingagents import e3_cli_hook
    monkeypatch.setattr(e3_cli_hook, "atexit", types.SimpleNamespace(
        register=hooks["registered"].append, unregister=hooks["unregistered"].append))
    return hooks


@pytest.mark.unit
def test_e3_summary_is_saved_once_with_the_run_labels(tmp_path, monkeypatch, offline, capsys):  # noqa: F811
    hooks = _fake_langfuse(monkeypatch, tmp_path)
    _models(monkeypatch, ScriptedModel())

    result = service.run_analysis(_request(tmp_path, source="mcp"))

    files = list((tmp_path / "e3").glob("*_e3.json"))
    assert files == [result.e3_summary_path]
    assert result.run_id in files[0].name
    summary = json.loads(files[0].read_text())
    assert summary["status"] == "success"
    assert (summary["run_id"], summary["source"]) == (result.run_id, "mcp")
    assert summary["observed_llm_calls"] > 0
    # Saved by the service, so the exit hook is dropped and cannot save it again.
    assert len(hooks["registered"]) == len(hooks["unregistered"]) == 1
    assert capsys.readouterr().out == ""   # the "E3 metrics saved" line goes to stderr


@pytest.mark.unit
def test_a_failed_run_saves_its_e3_summary_as_failed(tmp_path, monkeypatch, offline):  # noqa: F811
    _fake_langfuse(monkeypatch, tmp_path)
    _models(monkeypatch, ScriptedModel(fail_at=12))

    with pytest.raises(RuntimeError, match="provider unavailable"):
        service.run_analysis(_request(tmp_path))

    [summary_file] = (tmp_path / "e3").glob("*_e3.json")
    summary = json.loads(summary_file.read_text())
    assert summary["status"] == "failed"
    assert "provider unavailable" in summary["run_error"]
    assert not service.is_busy()


@pytest.mark.unit
def test_trace_metadata_carries_the_run_id_and_source(tmp_path, monkeypatch, offline):  # noqa: F811
    seen = []

    class Recorder(BaseCallbackHandler):
        def on_chain_start(self, serialized, inputs, *, metadata=None, **kwargs):
            seen.append(metadata or {})

    _models(monkeypatch, ScriptedModel())
    result = service.run_analysis(_request(tmp_path, source="dashboard"), extra_callbacks=[Recorder()])

    assert seen and seen[0]["tradingagents_run_id"] == result.run_id
    assert seen[0]["tradingagents_source"] == "dashboard"
    assert seen[0]["langfuse_session_id"] == result.run_id


@pytest.mark.unit
def test_one_run_at_a_time_per_process(tmp_path):
    assert service._RUN_LOCK.acquire()
    try:
        assert service.is_busy()
        with pytest.raises(service.AnalysisBusyError):
            service.run_analysis(_request(tmp_path))
    finally:
        service._RUN_LOCK.release()


@pytest.mark.unit
def test_a_cancelled_run_stops_and_frees_the_process(tmp_path, monkeypatch, offline):  # noqa: F811
    _models(monkeypatch, ScriptedModel())
    cancel = threading.Event()

    with pytest.raises(service.AnalysisCancelled):
        service.run_analysis(_request(tmp_path), on_event=lambda e: cancel.set(), cancel=cancel)

    assert not service.is_busy()
    assert service.run_analysis(_request(tmp_path)).rating == "Overweight"


@pytest.mark.unit
@pytest.mark.parametrize("change, message", [
    ({"ticker": "../etc"}, "ticker"),
    ({"analysts": ()}, "analysts"),
    ({"analysts": ("macro",)}, "analysts"),
    ({"source": "web"}, "source"),
    ({"asset_type": "bond"}, "asset_type"),
])
def test_a_bad_request_is_refused_before_any_work(change, message):
    fields = {"ticker": "NVDA", "trade_date": TRADE_DATE, **change}
    with pytest.raises(ValueError, match=message):
        service.AnalysisRequest(**fields)


@pytest.mark.unit
def test_a_future_date_is_refused(tmp_path):
    request = service.AnalysisRequest(ticker="NVDA", trade_date="2999-01-01", config=_config(tmp_path))
    with pytest.raises(ValueError, match="future"):
        service.run_analysis(request)
    assert not service.is_busy()


@pytest.mark.unit
def test_analysts_are_kept_in_graph_order():
    request = service.AnalysisRequest(ticker="NVDA", trade_date=TRADE_DATE, analysts=("news", "market"))
    assert request.analysts == ("market", "news")


@pytest.mark.unit
def test_progress_counts_reports_then_decisions():
    assert service.progress_of({}, ("market", "news")) == (0, 5, "Analysts")
    state = {"market_report": "m", "news_report": "n", "investment_plan": "p"}
    assert service.progress_of(state, ("market", "news")) == (3, 5, "Trader")


@pytest.mark.unit
def test_the_cli_runs_through_the_service_and_writes_what_it_did(tmp_path, monkeypatch, offline):  # noqa: F811
    """The CLI path end to end on the real graph: same files as before Phase 3."""
    import cli.display as display
    import cli.main as cli_main
    from cli import run as cli_run
    from cli.models import AnalystType

    buffer = display.MessageBuffer()
    monkeypatch.setattr(display, "message_buffer", buffer)
    monkeypatch.setattr(cli_run, "message_buffer", buffer)
    monkeypatch.setattr(cli_run, "create_layout", lambda: None)
    monkeypatch.setattr(cli_run, "update_display", lambda *a, **k: None)
    monkeypatch.setattr(cli_run, "Live", type("L", (), {"__init__": lambda s, *a, **k: None,
                                                  "__enter__": lambda s: s,
                                                  "__exit__": lambda s, *a: False}))
    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags=None: {
        "ticker": "NVDA", "analysis_date": TRADE_DATE,
        "analysts": [AnalystType.MARKET, AnalystType.NEWS], "asset_type": "stock"})
    monkeypatch.setattr(cli_run, "_build_run_config", lambda s, c: _config(tmp_path))
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *a, **k: "N")
    _models(monkeypatch, ScriptedModel())

    cli_run.run_analysis()

    run_dir = tmp_path / "results" / "NVDA" / TRADE_DATE
    usage = json.loads((run_dir / "token_usage.json").read_text())
    assert list(usage)[:len(CLI_USAGE_KEYS)] == CLI_USAGE_KEYS
    assert usage["source"] == "cli" and usage["llm_calls"] > 0
    assert (run_dir / "message_tool.log").read_text().strip()
    assert {p.name for p in (run_dir / "reports").glob("*.md")} >= {"market_report.md", "news_report.md"}
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory" / "trading_memory.md")})
    assert [e["rating"] for e in log.load_entries()] == ["Overweight"]
    assert buffer.agent_status and set(buffer.agent_status.values()) == {"completed"}


@pytest.mark.unit
def test_a_deferred_e3_summary_is_linked_into_the_run_record(tmp_path):
    """The CLI saves E3 after the record is written (defer_e3_save); the live CLI run 20261127-113820-f97deed2's
    record therefore named no E3 summary and get_run_metrics reported its cost as unavailable (found 2026-11-27)."""
    from tradingagents import run_store

    results = tmp_path / "results"
    request = service.AnalysisRequest(ticker="NVDA", trade_date="2026-01-09", source="cli", analysts=("market",),
                                      config={"results_dir": str(results)})
    record = run_store.build_record(run_id="r-cli", source="cli", status="completed", ticker="NVDA",
                                    trade_date="2026-01-09", asset_type="stock", analysts=("market",),
                                    usage={"llm_calls": 10})
    run_store.save(results, record)
    summary = tmp_path / "e3" / "r-cli_e3.json"
    summary.parent.mkdir()
    summary.write_text(json.dumps({"run_id": "r-cli", "estimated_cost_usd": 0.0205, "per_agent": {"Trader": {}}}))

    assert service.attach_e3_summary(request, "r-cli", summary) is True
    saved = run_store.load(results, "r-cli")
    assert saved["e3_summary_path"] == str(summary.absolute())
    assert saved["usage"]["e3_cost"]["estimated_cost_usd"] == 0.0205 and saved["usage"]["llm_calls"] == 10

    assert service.attach_e3_summary(request, "r-cli", None) is False            # E3 off: nothing to link
    assert service.attach_e3_summary(request, "no-such-run", summary) is False   # never raises
