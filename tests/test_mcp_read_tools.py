"""F2: the four read-only MCP tools (get_report, get_run_metrics, list_recent_runs, propose_trade) and the
per-run records they read (run_store). Offline: scripted models, fake Langfuse, no broker, no network."""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from tests.test_graph_end_to_end import offline  # noqa: F401
from tests.test_phase3_integration import TRADE_DATE, phase2_on  # noqa: F401
from tradingagents import mcp_read_tools as tools, mcp_server, run_store, service

SECRET = "PKSECRET" + "x" * 24


def _record(results, run_id, *, source="mcp", status="completed", rating="Overweight", ticker="NVDA",
            reports=True, error=None, error_category=None, usage=None, analysts=("market",)):
    state = {"market_report": "Market: uptrend. " * 50, "final_trade_decision": "FINAL: Overweight",
             "investment_plan": "Plan: accumulate", "trader_investment_plan": "Trader: buy on dips",
             "investment_debate_state": {"bull_history": "Bull case", "bear_history": "Bear case",
                                         "judge_decision": "Bull wins"},
             "risk_debate_state": {"aggressive_history": "Go", "conservative_history": "Wait",
                                   "neutral_history": "Some", "judge_decision": "Moderate"}} if reports else None
    record = run_store.build_record(
        run_id=run_id, source=source, status=status, ticker=ticker, trade_date=TRADE_DATE, asset_type="stock",
        analysts=analysts, rating=rating if status == "completed" else None, final_state=state,
        usage=usage or {"llm_calls": 10, "tokens_in": 1000, "tokens_out": 200, "tokens_total": 1200,
                        "elapsed_seconds": 24.5, "e2_cache": {"hits": 3, "misses": 2},
                        "e3_cost": {"estimated_cost_usd": 0.0194, "basis": "all_calls",
                                    "partial_cost_usd": None, "note": "estimate covers every LLM call"}},
        e1_events=["10:00 | quick tier | main failed (503) -> switching to backup"],
        error=error, error_category=error_category)
    assert run_store.save(results, record) is not None
    return record


@pytest.fixture
def results(tmp_path, monkeypatch):
    folder = tmp_path / "results"
    monkeypatch.setattr(tools, "results_dir", lambda: folder)
    monkeypatch.delenv(tools.SOURCES_ENV, raising=False)
    return folder


# ------------------------------------------------------------------ run_store
@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "../etc/passwd", "a/b", "..", "x" * 65, "a.b", " ", "run id", None, 7])
def test_a_run_id_can_never_name_a_path(results, bad):
    with pytest.raises(tools.ToolInputError):
        tools.get_report(bad)
    assert not run_store.is_valid_run_id(bad)


@pytest.mark.unit
def test_a_record_is_written_atomically_and_read_back(results):
    record = _record(results, "20261125-091530-1a2b3c4d")
    assert run_store.load(results, "20261125-091530-1a2b3c4d") == json.loads(json.dumps(record, default=str))
    assert not list((results / "runs").glob(".*.tmp"))                     # no temporary file left behind
    assert run_store.load(results, "20261125-000000-00000000") is None


@pytest.mark.unit
def test_a_record_that_cannot_be_saved_never_raises(tmp_path):
    blocker = tmp_path / "results"
    blocker.write_text("a file where the folder should be")
    assert run_store.save(blocker, {"run_id": "r1"}) is None
    assert run_store.save(tmp_path, {"run_id": "../escape"}) is None


# ------------------------------------------------------------------ get_report
@pytest.mark.unit
def test_get_report_returns_every_section_bounded(results, monkeypatch):
    monkeypatch.setenv(tools.REPORT_CHARS_ENV, "100")
    _record(results, "r-ok")
    answer = tools.get_report("r-ok")
    assert answer["rating"] == "Overweight" and answer["status"] == "completed"
    assert set(answer["sections"]) == {"market", "investment_debate", "investment_plan", "trader_plan",
                                       "risk_debate", "final_decision"}
    assert answer["sections"]["market"].endswith("more characters in the saved run]")
    assert answer["sections"]["investment_debate"] == {"bull": "Bull case", "bear": "Bear case",
                                                       "judge": "Bull wins"}
    assert answer["not_produced"] == ["sentiment", "news", "fundamentals"]
    assert tools.get_report("r-ok", "final_decision")["sections"] == {"final_decision": "FINAL: Overweight"}


@pytest.mark.unit
def test_get_report_explains_what_is_missing(results):
    _record(results, "r-ok")
    _record(results, "r-failed", status="failed", reports=False, error="503 UNAVAILABLE",
            error_category="provider_unavailable")
    with pytest.raises(tools.RunNotFound, match="no saved run r-missing"):
        tools.get_report("r-missing")
    with pytest.raises(tools.ReportUnavailable, match=r"ended as failed \[provider_unavailable\]"):
        tools.get_report("r-failed")
    with pytest.raises(tools.ReportUnavailable, match="no news output .analysts run: market"):
        tools.get_report("r-ok", "news")
    with pytest.raises(tools.ToolInputError, match="section must be one of"):
        tools.get_report("r-ok", "everything")


@pytest.mark.unit
def test_get_report_keeps_one_answer_under_its_size_cap(results, monkeypatch):
    monkeypatch.setenv(tools.REPORT_CHARS_ENV, "50000")
    monkeypatch.setattr(tools, "MAX_REPORT_ANSWER_CHARS", 20_000)
    _record(results, "r-big")
    run = run_store.load(results, "r-big")
    run["reports"]["final_decision"] = "D" * 30_000
    run_store.save(results, run)
    answer = tools.get_report("r-big")
    assert "omitted to keep this answer under 20,000 characters" in answer["sections"]["final_decision"]
    assert sum(len(json.dumps(v)) for v in answer["sections"].values()) < 25_000


# ------------------------------------------------------------------ get_run_metrics
@pytest.mark.unit
def test_get_run_metrics_separates_the_estimate_from_billing(results):
    _record(results, "r-ok")
    metrics = tools.get_run_metrics("r-ok")
    assert metrics["e1_routing"]["summary"]["switches"] == 1 and metrics["e1_routing"]["summary"]["backup_calls"] == 1
    assert metrics["e2_cache"] == {"hits": 3, "misses": 2}
    assert metrics["e3_usage"]["tokens_total"] == 1200 and metrics["timing"]["elapsed_seconds"] == 24.5
    cost = metrics["cost"]
    assert cost["estimated_cost_usd"] == 0.0194 and cost["kind"] == "estimate" and cost["actual_billing"] is None
    assert "not the provider's bill" in cost["billing_note"]


@pytest.mark.unit
def test_get_run_metrics_finds_the_e3_summary_from_any_working_directory(results, tmp_path, monkeypatch):
    """E3 saves under a relative folder by default ("e3_results", from the writer's working directory). The live F1
    run 20261127-111852-5a8696f8 recorded that relative path, so an MCP server started elsewhere silently lost the
    per-agent table and E3 timing (found 2026-11-27). The record must name the file absolutely."""
    writer = tmp_path / "dashboard_cwd"
    (writer / "e3_results").mkdir(parents=True)
    summary = {"run_id": "r-cwd", "per_agent": {"Market Analyst": {"llm_calls": 2}}, "timing": {"x": 1}}
    (writer / "e3_results" / "r-cwd_e3.json").write_text(json.dumps(summary))
    monkeypatch.chdir(writer)
    record = run_store.build_record(run_id="r-cwd", source="dashboard", status="completed", ticker="NVDA",
                                    trade_date=TRADE_DATE, asset_type="stock", analysts=("market",), usage={},
                                    e3_summary_path="e3_results/r-cwd_e3.json")
    assert run_store.save(results, record) is not None

    elsewhere = tmp_path / "mcp_server_cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    metrics = tools.get_run_metrics("r-cwd")
    assert metrics["per_agent"] == {"Market Analyst": {"llm_calls": 2}}


@pytest.mark.unit
def test_get_run_metrics_of_a_failed_run_keeps_its_error(results):
    _record(results, "r-failed", status="failed", reports=False, error="503 UNAVAILABLE: overloaded",
            error_category="provider_unavailable", usage={"llm_calls": 3, "llm_calls_failed": 3})
    metrics = tools.get_run_metrics("r-failed")
    assert metrics["errors"] == {"status": "failed", "error_category": "provider_unavailable",
                                 "error": "503 UNAVAILABLE: overloaded"}
    assert metrics["cost"]["basis"] == "unavailable" and metrics["cost"]["estimated_cost_usd"] is None


# ------------------------------------------------------------------ list_recent_runs
@pytest.mark.unit
def test_list_recent_runs_is_newest_first_and_capped(results):
    for i in range(55):
        _record(results, f"r-{i:02d}", reports=False)
        path = results / "runs" / f"r-{i:02d}.json"
        os.utime(path, (time.time() - 1000 + i, time.time() - 1000 + i))
    answer = tools.list_recent_runs(3)
    assert [r["run_id"] for r in answer["runs"]] == ["r-54", "r-53", "r-52"]
    assert set(answer["runs"][0]) >= {"run_id", "ticker", "trade_date", "status", "rating", "source",
                                      "estimated_cost_usd", "has_reports"}
    capped = tools.list_recent_runs(500)
    assert len(capped["runs"]) == 50 and capped["limit_applied"] == 50 and capped["limit_requested"] == 500


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0, -1, 1.5, "10", True, None])
def test_list_recent_runs_refuses_a_malformed_limit(results, bad):
    with pytest.raises(tools.ToolInputError, match="from 1 to 50"):
        tools.list_recent_runs(bad)


@pytest.mark.unit
def test_the_server_can_show_only_some_sources(results, monkeypatch):
    _record(results, "r-mcp", source="mcp")
    _record(results, "r-dash", source="dashboard")
    monkeypatch.setenv(tools.SOURCES_ENV, "mcp")
    assert [r["run_id"] for r in tools.list_recent_runs(10)["runs"]] == ["r-mcp"]
    with pytest.raises(tools.RunNotFound):
        tools.get_report("r-dash")                    # hidden, so not found: no hint that it exists
    monkeypatch.setenv(tools.SOURCES_ENV, "mcp,nonsense")
    with pytest.raises(tools.ServerSetting, match="comma-separated subset"):
        tools.list_recent_runs(10)


@pytest.mark.unit
def test_no_audit_record_or_credential_is_ever_returned(results, monkeypatch):
    monkeypatch.setenv("ALPACA_SECRET_KEY", SECRET)
    _record(results, "r-ok")
    (results / "approval_decisions.jsonl").write_text(json.dumps({"user": "alice", "event": "rejected"}) + "\n")
    (results / "paper_orders.jsonl").write_text(json.dumps({"event": "submitted", "approved_by": "alice"}) + "\n")
    everything = json.dumps([tools.get_report("r-ok"), tools.get_run_metrics("r-ok"),
                             tools.list_recent_runs(10), tools.propose_trade("r-ok", today=TRADE_DATE)])
    assert SECRET not in everything and "alice" not in everything and str(results) not in everything


# ------------------------------------------------------------------ propose_trade
@pytest.mark.unit
@pytest.mark.parametrize("rating, ticker, status, side", [
    ("Buy", "NVDA", "buy_allowed", "buy"),
    ("Overweight", "NVDA", "buy_allowed", "buy"),
    ("Sell", "NVDA", "sell_only_if_long", "sell"),
    ("Underweight", "NVDA", "sell_only_if_long", "sell"),
    ("Hold", "NVDA", "no_trade", None),
    ("REVIEW", "NVDA", "no_trade", None),
    ("Buy", "BTC-USD", "no_trade", None),          # not a US-listed stock
])
def test_propose_trade_follows_the_f1_rules(results, rating, ticker, status, side):
    _record(results, "r-1", rating=rating, ticker=ticker)
    answer = tools.propose_trade("r-1", today=TRADE_DATE)
    assert answer["proposal"]["status"] == status and answer["proposal"]["side"] == side
    assert answer["read_only"] is True and "never approves, signs, submits, cancels or executes" in answer["note"]
    if side:
        rules = answer["order_rules"]
        assert rules["client_order_id"] == f"ta-r-1-{side}" and rules["max_order_notional_usd"] == 1000.0
        assert "typed by the signed-in person" in rules["quantity"] and "qty" not in answer["proposal"]
    else:
        assert "order_rules" not in answer


@pytest.mark.unit
def test_propose_trade_uses_the_same_rules_as_the_dashboard(results, monkeypatch):
    from tradingagents.execution import order_policy

    assert order_policy.propose_order is tools.trade_rules.propose_order         # one definition, not a copy
    monkeypatch.setenv("TRADINGAGENTS_MAX_ORDER_NOTIONAL", "250")
    _record(results, "r-1", rating="Buy")
    answer = tools.propose_trade("r-1", today="2026-12-01")
    assert answer["order_rules"]["max_order_notional_usd"] == 250.0
    assert "not today (2026-12-01)" in answer["order_rules"]["stale_analysis"]
    monkeypatch.setenv("TRADINGAGENTS_MAX_ORDER_NOTIONAL", "lots")
    with pytest.raises(tools.ServerSetting):
        tools.propose_trade("r-1")


@pytest.mark.unit
def test_propose_trade_on_a_failed_run_proposes_nothing(results):
    _record(results, "r-failed", status="failed", reports=False, error_category="timeout")
    answer = tools.propose_trade("r-failed")
    assert answer["proposal"] == {"status": "no_trade", "side": None,
                                  "reason": "run r-failed ended as failed; only a completed analysis can "
                                            "propose a trade"}


# ------------------------------------------------------------------ the real service writes the records
@pytest.mark.unit
def test_a_real_mcp_run_can_be_read_back_after_it_ends(phase2_on):  # noqa: F811
    fastmcp = pytest.importorskip("fastmcp")
    import tradingagents.mcp_server as server

    async def go():
        async with fastmcp.Client(server.create_server()) as client:
            first = (await client.call_tool("analyze_stock", {"ticker": "NVDA", "trade_date": TRADE_DATE,
                                                              "analysts": ["market"]})).data
            calls = {}
            for name, args in (("get_report", {"run_id": first["run_id"]}),
                               ("get_run_metrics", {"run_id": first["run_id"]}),
                               ("list_recent_runs", {"limit": 5}),
                               ("propose_trade", {"run_id": first["run_id"]})):
                calls[name] = (await client.call_tool(name, args)).data
            return first, calls

    first, calls = asyncio.run(go())
    report, metrics = calls["get_report"], calls["get_run_metrics"]
    assert report["run_id"] == first["run_id"] and report["rating"] == first["rating"] == "Overweight"
    assert report["sections"]["market"] and report["sections"]["final_decision"]
    assert report["sections"]["risk_debate"] and report["sections"]["investment_debate"]
    assert metrics["source"] == "mcp" and metrics["status"] == "completed"
    assert metrics["e3_usage"]["llm_calls"] == first["usage"]["llm_calls"]
    assert metrics["e1_routing"]["summary"]["backup_calls"] == first["model_fallbacks"] > 0   # the fixture's main model fails
    assert metrics["e2_cache"] == first["usage"]["e2_cache"]
    assert metrics["run_budget"]["max_llm_calls"] == 15
    assert metrics["cost"]["kind"] == "estimate" and metrics["cost"]["actual_billing"] is None
    assert [r["run_id"] for r in calls["list_recent_runs"]["runs"]] == [first["run_id"]]
    assert calls["propose_trade"]["proposal"]["status"] == "buy_allowed"


@pytest.mark.unit
def test_records_outlive_the_worker_process(tmp_path, monkeypatch):
    """A worker run's record is read back after the child has exited, for a completed and a terminated run."""
    from tests.test_deadlines_and_limits import _children_gone
    from tests.test_worker import _request
    from tradingagents import worker
    from tradingagents.run_deadline import RunDeadline

    for name, value in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "offline", "LANGFUSE_SECRET_KEY": "offline",
                        "LANGFUSE_BASE_URL": "http://localhost.invalid", "E3_RESULTS_DIR": str(tmp_path / "e3"),
                        "WORKER_FAKE_MARKER": str(tmp_path / "marker")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(tools, "results_dir", lambda: tmp_path / "results")
    done = worker.run_in_worker(_request(tmp_path, analysts=("market",), source="mcp"),
                                setup="tests.worker_fakes:scripted")
    deadline = RunDeadline(analysis_seconds=120, step_seconds=1.0, grace_seconds=0.5)
    with pytest.raises(service.AnalysisDeadlineExceeded) as stopped:
        worker.run_in_worker(_request(tmp_path, analysts=("market",), source="mcp", deadline=deadline),
                             setup="tests.worker_fakes:hung_market_call")
    assert _children_gone()                                     # both workers have exited
    assert tools.get_report(done.run_id)["sections"]["final_decision"]
    stopped_id = stopped.value.analysis_failure["run_id"]
    metrics = tools.get_run_metrics(stopped_id)
    assert metrics["status"] == "deadline_exceeded" and metrics["errors"]["error_category"] == "timeout"
    assert metrics["e3_usage"]["llm_calls_in_flight"] == 1
    with pytest.raises(tools.ReportUnavailable, match="deadline_exceeded"):
        tools.get_report(stopped_id)
    assert [r["status"] for r in tools.list_recent_runs(5)["runs"]] == ["deadline_exceeded", "completed"]


@pytest.mark.unit
def test_a_budget_stop_is_recorded_too(phase2_on, monkeypatch):  # noqa: F811
    from tradingagents import run_budget

    monkeypatch.setenv(run_budget.MCP_CALLS_ENV, "3")
    with pytest.raises(service.AnalysisBudgetExceeded):
        mcp_server.analyze_stock_sync("NVDA", TRADE_DATE, ["market"], run=service.run_analysis)
    [row] = tools.list_recent_runs(5)["runs"]
    assert row["status"] == "budget_exceeded" and row["has_reports"] is False
    assert tools.get_run_metrics(row["run_id"])["run_budget"]["max_llm_calls"] == 3


# ------------------------------------------------------------------ over the protocol
def _call(name, args, results=None):
    fastmcp = pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    async def go():
        async with fastmcp.Client(mcp_server.create_server(run=_no_analysis)) as client:
            try:
                return (await client.call_tool(name, args)).data
            except ToolError as exc:
                return str(exc)
    return asyncio.run(go())


def _no_analysis(*args, **kwargs):
    raise AssertionError("a read tool must never start an analysis")


@pytest.mark.unit
@pytest.mark.parametrize("name, args, said", [
    ("get_report", {"run_id": "../../etc/passwd"}, "invalid request: run_id must be a run ID"),
    ("get_report", {"run_id": "r-missing"}, "run not found: no saved run r-missing"),
    ("get_report", {"run_id": "r-ok", "section": "nonsense"}, "invalid request: section must be one of"),
    ("get_report", {"run_id": "r-failed"}, "report unavailable: run r-failed ended as failed"),
    ("get_run_metrics", {"run_id": "r-missing"}, "run not found"),
    ("list_recent_runs", {"limit": 0}, "invalid request: limit must be a whole number from 1 to 50"),
    ("propose_trade", {"run_id": "a/b"}, "invalid request"),
])
def test_bad_requests_come_back_as_clear_tool_errors(results, name, args, said):
    _record(results, "r-ok")
    _record(results, "r-failed", status="failed", reports=False)
    text = _call(name, args)
    assert isinstance(text, str) and text.startswith(said), text
    assert "Traceback" not in text and str(results) not in text


@pytest.mark.unit
def test_the_read_tools_answer_over_the_protocol_without_running_anything(results):
    _record(results, "r-ok")
    assert _call("get_report", {"run_id": "r-ok", "section": "final_decision"})["sections"] == {
        "final_decision": "FINAL: Overweight"}
    assert _call("get_run_metrics", {"run_id": "r-ok"})["cost"]["kind"] == "estimate"
    assert _call("list_recent_runs", {})["runs"][0]["run_id"] == "r-ok"
    assert _call("propose_trade", {"run_id": "r-ok"})["proposal"]["side"] == "buy"


@pytest.mark.unit
def test_the_read_tool_schemas_take_only_what_they_need():
    fastmcp = pytest.importorskip("fastmcp")

    async def go():
        async with fastmcp.Client(mcp_server.create_server(run=_no_analysis)) as client:
            return {t.name: t for t in await client.list_tools()}

    listed = asyncio.run(go())
    assert set(listed["get_report"].input_schema["properties"]) == {"run_id", "section"}
    assert listed["get_report"].input_schema["required"] == ["run_id"]
    assert set(listed["get_run_metrics"].input_schema["properties"]) == {"run_id"}
    assert set(listed["list_recent_runs"].input_schema["properties"]) == {"limit"}
    assert listed["list_recent_runs"].input_schema["properties"]["limit"]["default"] == 10
    assert set(listed["propose_trade"].input_schema["properties"]) == {"run_id"}
    for name in ("get_report", "get_run_metrics", "list_recent_runs", "propose_trade"):
        hints = listed[name].annotations
        assert (hints.read_only_hint, hints.destructive_hint, hints.idempotent_hint, hints.open_world_hint) == \
            (True, False, True, False), name


@pytest.mark.unit
def test_every_tool_needs_the_token_over_http(results):
    pytest.importorskip("fastmcp")
    from starlette.testclient import TestClient

    _record(results, "r-ok")
    token = "s3cret-token-0123456789-abcdef"
    app = mcp_server.create_server(auth=mcp_server.api_key_verifier(token), run=_no_analysis).http_app()
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(app, base_url="http://127.0.0.1") as http:   # in-process ASGI: no socket, no network
        for name, args in (("analyze_stock", {"ticker": "NVDA"}), ("get_report", {"run_id": "r-ok"}),
                           ("get_run_metrics", {"run_id": "r-ok"}), ("list_recent_runs", {}),
                           ("propose_trade", {"run_id": "r-ok"})):
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
            for presented in (None, "Bearer wrong-token", "Basic " + token):
                sent = {**headers, **({"Authorization": presented} if presented else {})}
                response = http.post("/mcp", json=body, headers=sent)
                assert response.status_code == 401, (name, presented)
                assert "Overweight" not in response.text and "r-ok" not in response.text
            accepted = http.post("/mcp", json=body, headers={**headers, "Authorization": f"Bearer {token}"})
            assert accepted.status_code != 401                 # past authentication (then MCP's session rules)


# ------------------------------------------------------------------ list_recent_runs limit, over the protocol
def _runs(results, n):
    for i in range(n):
        _record(results, f"r-{i:02d}", reports=False)
        os.utime(results / "runs" / f"r-{i:02d}.json", (time.time() - 1000 + i, time.time() - 1000 + i))


@pytest.mark.unit
def test_an_empty_history_is_an_empty_list_not_an_error(results):
    assert not results.exists()                                   # no results folder, so no runs/ either
    answer = _call("list_recent_runs", {})
    assert isinstance(answer, dict), answer
    assert answer["runs"] == [] and (answer["limit_requested"], answer["limit_applied"]) == (10, 10)
    (results / "runs").mkdir(parents=True)                        # an empty runs/ folder: the same
    assert _call("list_recent_runs", {"limit": 5})["runs"] == []


@pytest.mark.unit
def test_an_omitted_limit_lists_the_10_most_recent(results):
    _runs(results, 15)
    answer = _call("list_recent_runs", {})
    assert [r["run_id"] for r in answer["runs"]] == [f"r-{i:02d}" for i in range(14, 4, -1)]
    assert (answer["limit_requested"], answer["limit_applied"], answer["max_limit"]) == (10, 10, 50)


@pytest.mark.unit
@pytest.mark.parametrize("asked", [50, 51, 500, 1_000_000])
def test_a_limit_above_50_is_capped_not_refused(results, asked):
    _runs(results, 55)
    answer = _call("list_recent_runs", {"limit": asked})
    assert isinstance(answer, dict), answer
    assert len(answer["runs"]) == 50 and answer["limit_applied"] == 50 and answer["limit_requested"] == asked
    assert answer["runs"][0]["run_id"] == "r-54"                  # still newest first


@pytest.mark.unit
@pytest.mark.parametrize("asked", [0, -1, -50])
def test_a_zero_or_negative_limit_is_refused(results, asked):
    _runs(results, 3)
    text = _call("list_recent_runs", {"limit": asked})
    assert text == "invalid request: limit must be a whole number from 1 to 50"


@pytest.mark.unit
@pytest.mark.parametrize("asked", ["10", "ten", True, False, 2.0, 1.5, None, [5], {"n": 5}])
def test_a_malformed_limit_is_refused_never_converted(results, asked):
    """Strict: lax validation would have turned "10" into 10, 2.0 into 2 and true into 1."""
    _runs(results, 3)
    text = _call("list_recent_runs", {"limit": asked})
    assert isinstance(text, str), f"{asked!r} was accepted as {text}"
    assert "limit" in text and "valid integer" in text and "Traceback" not in text


@pytest.mark.unit
def test_the_limit_schema_states_the_range_it_enforces():
    fastmcp = pytest.importorskip("fastmcp")

    async def go():
        async with fastmcp.Client(mcp_server.create_server(run=_no_analysis)) as client:
            return next(t for t in await client.list_tools() if t.name == "list_recent_runs")

    limit = asyncio.run(go()).input_schema["properties"]["limit"]
    assert limit == {"default": 10, "minimum": 1, "type": "integer"}   # no maximum: above 50 is capped


@pytest.mark.unit
def test_get_run_metrics_shows_how_failed_calls_were_accounted_and_that_e2_was_off(results):
    """The live F1 run 20261127-111852-5a8696f8: one 429 call reported no usage and E2 was off. The metrics showed
    the $0.035440 budget figure without the failed-call counts behind it, and e2_cache as a bare null."""
    budget = {"max_llm_calls": 24, "max_cost_usd": 0.04, "llm_calls_started": 16, "reported_usage_cost_usd": 0.034445,
              "unreported_usage_estimate_usd": 0.000995, "budget_cost_usd": 0.03544, "exhausted": False,
              "reason": None, "failed_calls": 1, "failed_calls_with_usage": 0, "calls_without_usage": 0,
              "unpriced_calls": 0, "basis": "conservative budget accounting, not the provider's bill"}
    _record(results, "r-429", usage={"llm_calls": 16, "tokens_in": 52260, "tokens_out": 7692, "run_budget": budget})
    metrics = tools.get_run_metrics("r-429")
    view = metrics["run_budget"]
    assert view["failed_calls"] == 1 and view["failed_calls_with_usage"] == 0
    assert view["unreported_usage_estimate_usd"] == 0.000995 and "not the provider's bill" in view["basis"]
    assert metrics["e2_cache"] == {"enabled": False, "note": tools.E2_OFF["note"]}
    assert metrics["e3_usage"]["tokens_in"] == 52260                    # reported tokens only; nothing invented
