"""Phase 3 data model: ANALYSIS_RUN, TRADE_PROPOSAL, APPROVAL_DECISION, PAPER_ORDER and MCP_REQUEST records.

Offline: fake Alpaca (tests.test_alpaca_paper), scripted models (phase2_on), in-process MCP clients. Checks each
entity's serialization and retrieval, identifier uniqueness, the logical references between records, old records
written before these fields existed, repeats and concurrent writers, and that nothing here can send an order.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tests.test_alpaca_paper import FakeData, FakeTrading, _no_alpaca_sdk  # noqa: F401
from tests.test_dashboard_logic import LIMITS, TODAY, _broker, _result
from tests.test_graph_end_to_end import offline  # noqa: F401
from tests.test_phase3_integration import TRADE_DATE, phase2_on  # noqa: F401
from tradingagents import (
    audit_log,
    mcp_audit,
    mcp_read_tools as tools,
    mcp_server,
    run_store,
    service,
    trade_rules,
)
from tradingagents.dashboard import trade
from tradingagents.execution import alpaca_paper as ap, order_policy as op
from tradingagents.execution.decision_log import LOG_NAME as DECISIONS, DecisionLog
from tradingagents.execution.identity import _issue
from tradingagents.proposal_store import ProposalStore

RUN = "20261121-101500-ab12cd34"          # tests.test_dashboard_logic._result's run ID


@pytest.fixture(autouse=True)
def _signed_in():
    global TESTER
    TESTER = _issue("tester", "password")


def _setup(tmp_path, rating="Buy", **trading):
    broker = _broker(tmp_path, **trading)
    result = _result(rating)
    proposals = ProposalStore.for_results_dir(tmp_path)
    panel = trade.load_panel(result, broker, LIMITS, today=TODAY, proposals=proposals)
    return broker, result, panel, DecisionLog.for_results_dir(tmp_path), proposals


def _submit(panel, result, broker, decisions, proposals=None, qty=2, **kw):
    args = {"confirmed": True, "identity": TESTER, "today": TODAY, "order_type": "limit", "limit_price": 100.0,
            "decisions": decisions, "proposals": proposals, **kw}
    return trade.submit(panel, result, broker, qty, **args)


# ================================================================== ANALYSIS_RUN
@pytest.mark.unit
def test_a_run_record_states_when_it_started_and_its_schema(tmp_path):
    request = SimpleNamespace(source="dashboard", ticker="NVDA", trade_date=TRADE_DATE, asset_type="stock",
                              analysts=("market",), deadline=SimpleNamespace(started_at=1_795_000_000.0))
    config = {"results_dir": str(tmp_path)}
    service._store_run(config, request, "20261126-090000-aa11bb22", "failed", {"llm_calls": 1}, error="x",
                       error_category="provider_unavailable")
    record = run_store.load(tmp_path, "20261126-090000-aa11bb22")
    assert record["schema"] == run_store.SCHEMA_VERSION == 2
    clicked = datetime.fromtimestamp(1_795_000_000.0, UTC).isoformat(timespec="seconds")
    assert record["started_at"] == clicked                       # from the request's deadline (the click)
    service._store_run(config, replace_ns(request, deadline=None), "20261126-090000-cc33dd44", "completed", {},
                       started_at="2026-11-26T09:00:00+00:00")
    assert run_store.load(tmp_path, "20261126-090000-cc33dd44")["started_at"] == "2026-11-26T09:00:00+00:00"


def replace_ns(ns, **changes):
    return SimpleNamespace(**{**vars(ns), **changes})


@pytest.mark.unit
def test_a_version_1_run_record_is_still_read_by_every_tool(tmp_path, monkeypatch):
    """A record saved before started_at existed (schema 1) is never rewritten and still answers."""
    folder = tmp_path / "results"
    monkeypatch.setattr(tools, "results_dir", lambda: folder)
    monkeypatch.delenv(tools.SOURCES_ENV, raising=False)
    legacy = {"schema": 1, "run_id": "20261124-120000-0a0b0c0d", "source": "mcp", "status": "completed",
              "recorded_at": "2026-11-24T12:01:00+00:00", "ticker": "NVDA", "trade_date": TRADE_DATE,
              "asset_type": "stock", "analysts": ["market"], "rating": "Buy", "needs_review": False, "error": None,
              "error_category": None, "e1_events": [], "e3_summary_path": None,
              "usage": {"llm_calls": 3, "tokens_in": 10, "tokens_out": 5, "tokens_total": 15},
              "reports": {"analyst_reports": {"market": "up"}, "final_decision": "FINAL: Buy"}}
    path = folder / "runs" / f"{legacy['run_id']}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy))
    before = path.read_bytes()
    assert run_store.load(folder, legacy["run_id"]) == legacy and "started_at" not in legacy
    assert tools.get_report(legacy["run_id"])["rating"] == "Buy"
    assert tools.get_run_metrics(legacy["run_id"])["status"] == "completed"
    assert [r["run_id"] for r in tools.list_recent_runs(5)["runs"]] == [legacy["run_id"]]
    answer = tools.propose_trade(legacy["run_id"], today=TRADE_DATE)
    assert answer["proposal_id"] == f"ta-{legacy['run_id']}-buy" and answer["read_only"] is True
    assert path.read_bytes() == before


# ================================================================== TRADE_PROPOSAL
@pytest.mark.unit
def test_a_proposal_is_recorded_once_with_its_run_and_no_quantity(tmp_path):
    broker, result, panel, _, proposals = _setup(tmp_path)
    stored = proposals.load(f"ta-{RUN}-buy")
    assert panel.proposal_record == stored and panel.proposal_id == panel.client_order_id == stored["proposal_id"]
    assert {k: stored[k] for k in ("run_id", "ticker", "rating", "side", "eligible", "held_long_qty",
                                   "client_order_id", "source", "trade_date", "max_notional", "stale")} == {
        "run_id": RUN, "ticker": "NVDA", "rating": "Buy", "side": "buy", "eligible": True, "held_long_qty": 0.0,
        "client_order_id": f"ta-{RUN}-buy", "source": "dashboard", "trade_date": TODAY, "max_notional": 1000.0,
        "stale": False}
    assert stored["created_at"].endswith("+00:00") and stored["reason"]
    assert not {"qty", "quantity", "limit_price", "approval", "order_id"} & set(stored)   # never an order
    for _ in range(3):                                                      # page reruns
        again = trade.load_panel(result, broker, LIMITS, today=TODAY, proposals=proposals)
        assert again.proposal_record == stored
    assert [p.name for p in proposals.folder.iterdir()] == [f"ta-{RUN}-buy.json"]
    assert broker.trading.submitted == [] and ap.OrderLedger.for_results_dir(tmp_path).entries() == []


@pytest.mark.unit
@pytest.mark.parametrize("rating, held, side, eligible", [
    ("Hold", 0.0, None, False), ("Sell", 0.0, None, False), ("Sell", 5.0, "sell", True), ("Overweight", 0, "buy", True)])
def test_a_no_trade_proposal_is_recorded_with_its_reason(tmp_path, rating, held, side, eligible):
    _, _, panel, _, proposals = _setup(tmp_path, rating, held=held)
    [stored] = proposals.for_run(RUN)
    assert (stored["side"], stored["eligible"], stored["held_long_qty"]) == (side, eligible, float(held))
    assert stored["proposal_id"] == f"ta-{RUN}-{side or 'none'}" == panel.proposal_id
    assert stored["client_order_id"] == (f"ta-{RUN}-{side}" if side else None)
    assert stored["reason"] == panel.proposal.reason


@pytest.mark.unit
def test_concurrent_writers_create_one_proposal(tmp_path):
    _, result, panel, _, _ = _setup(tmp_path / "first")
    store = ProposalStore.for_results_dir(tmp_path / "shared")
    created = []
    threads = [threading.Thread(target=lambda: created.append(trade.record_proposal(panel, result, store)[1]))
               for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert created.count(True) == 1 and len(created) == 16
    assert [p.name for p in store.folder.iterdir()] == [f"ta-{RUN}-buy.json"]     # no temporary files left


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["../x", "ta-../../etc-buy", "ta-r-short", "ta--buy", "ta-a/b-buy", "", None, 5])
def test_a_proposal_id_can_never_name_a_path(tmp_path, bad):
    assert not trade_rules.is_valid_proposal_id(bad)
    with pytest.raises(ValueError):
        ProposalStore(tmp_path).load(bad)


@pytest.mark.unit
def test_an_unrecorded_proposal_cannot_be_approved(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "proposals").write_text("a file where the folder should be")
    broker = _broker(tmp_path)
    result = _result("Buy")
    decisions, proposals = DecisionLog.for_results_dir(tmp_path), ProposalStore.for_results_dir(blocked)
    panel = trade.load_panel(result, broker, LIMITS, today=TODAY, proposals=proposals)   # the page still shows
    assert panel.proposal_record is None
    with pytest.raises(op.OrderRejected, match="could not be recorded"):
        _submit(panel, result, broker, decisions, proposals)
    assert broker.trading.submitted == [] and ap.OrderLedger.for_results_dir(tmp_path).entries() == []
    assert [(e["event"], e["category"]) for e in decisions.entries()] == [("submission_failed", "record_failed")]


@pytest.mark.unit
def test_the_mcp_proposal_is_a_preview_that_records_nothing(tmp_path, monkeypatch):
    folder = tmp_path / "results"
    monkeypatch.setattr(tools, "results_dir", lambda: folder)
    from tests.test_mcp_read_tools import _record

    _record(folder, "r-ok")
    answer = tools.propose_trade("r-ok", today=TRADE_DATE)
    assert answer["proposal_id"] == "ta-r-ok-buy" == answer["order_rules"]["client_order_id"]
    assert answer["read_only"] is True and not (folder / "proposals").exists()
    assert sorted(p.name for p in folder.iterdir()) == ["runs"]


# ================================================================== APPROVAL_DECISION
@pytest.mark.unit
def test_every_decision_has_a_unique_id_and_names_its_run_and_proposal(tmp_path):
    broker, result, panel, decisions, proposals = _setup(tmp_path)
    trade.reject(panel, result, decisions, identity=TESTER)
    trade.reconsider(panel, result, decisions, identity=TESTER)
    with pytest.raises(op.OrderRejected):
        _submit(panel, result, broker, decisions, proposals, confirmed=False)
    _submit(panel, result, broker, decisions, proposals)
    entries = decisions.entries()
    assert [e["event"] for e in entries] == ["rejected", "reconsidered", "submission_failed", "approved"]
    ids = [e["decision_id"] for e in entries]
    assert len(set(ids)) == 4 and all(i.startswith("dec-") and len(i) == 36 for i in ids)
    stored = proposals.load(panel.proposal_id)
    assert {e["proposal_id"] for e in entries} == {stored["proposal_id"]}            # logical FK -> proposal
    assert {e["run_id"] for e in entries} == {stored["run_id"]} == {RUN}              # logical FK -> run
    assert decisions.get(ids[-1]) == entries[-1] and decisions.approvals(panel.proposal_id) == [entries[-1]]
    assert all(e["schema"] == 2 for e in entries)


@pytest.mark.unit
def test_old_decision_lines_are_kept_byte_for_byte_and_get_stable_ids(tmp_path):
    old = [{"recorded_at": "2026-11-23T10:00:00+00:00", "event": "rejected", "category": "user_rejected",
            "source": "dashboard", "run_id": RUN, "proposal_id": f"ta-{RUN}-buy", "ticker": "NVDA", "side": "buy",
            "qty": 2, "limit_price": 100.0, "user": "tester", "auth_method": "password", "problems": []},
           {"recorded_at": "2026-11-23T10:01:00+00:00", "event": "reconsidered", "category": "user_rejected",
            "source": "dashboard", "run_id": RUN, "proposal_id": f"ta-{RUN}-buy", "ticker": "NVDA", "side": "buy",
            "qty": None, "limit_price": None, "user": "tester", "auth_method": "password", "problems": []}]
    path = tmp_path / DECISIONS
    path.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in old) + "{not json\n")
    before = path.read_bytes()
    broker, result, panel, decisions, proposals = _setup(tmp_path)
    first = decisions.entries()
    assert [e.get("legacy") for e in first] == [True, True]
    assert [e["decision_id"] for e in first] == [e["decision_id"] for e in decisions.entries()]   # stable
    assert len({e["decision_id"] for e in first}) == 2 and first[0]["decision_id"].startswith("dec-legacy-")
    assert decisions.rejection(panel.client_order_id) is None            # the old reconsideration still counts
    _submit(panel, result, broker, decisions, proposals)
    assert path.read_bytes().startswith(before)                          # appended to, never rewritten
    assert [e["event"] for e in decisions.entries()] == ["rejected", "reconsidered", "approved"]


@pytest.mark.unit
def test_concurrent_decisions_never_corrupt_the_log(tmp_path):
    _, result, panel, decisions, _ = _setup(tmp_path)

    def write(n):
        for _ in range(25):
            trade.record_failure(decisions, "invalid_order", panel, result, qty=n, identity=TESTER, problems=["x"])

    threads = [threading.Thread(target=write, args=(n,)) for n in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / DECISIONS).read_text().splitlines()
    assert len(lines) == 200 and all(json.loads(line)["category"] == "invalid_order" for line in lines)
    assert len({json.loads(line)["decision_id"] for line in lines}) == 200


@pytest.mark.unit
def test_an_approval_that_cannot_be_recorded_sends_nothing(tmp_path, monkeypatch):
    broker, result, panel, decisions, proposals = _setup(tmp_path)

    def disk_full(order, identity=None):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(decisions, "approve", disk_full)
    with pytest.raises(ap.ApprovalNotRecorded, match="nothing was sent"):
        _submit(panel, result, broker, decisions, proposals)
    assert broker.trading.submitted == [] and ap.OrderLedger.for_results_dir(tmp_path).entries() == []
    assert [e["category"] for e in decisions.entries()] == ["record_failed"]


@pytest.mark.unit
def test_the_decision_id_is_signed_into_the_approved_order(tmp_path):
    broker, result, panel, decisions, _ = _setup(tmp_path)
    order = op.approve(panel.proposal, 2, 100.0, run_id=RUN, buying_power=1e6, held_long_qty=0.0, limits=LIMITS,
                       confirmed=True, identity=TESTER, order_type="limit", limit_price=100.0, market_open=True,
                       decision_id="dec-" + "a" * 32)
    assert op.verify_approval(order) == [] and order.proposal_id == f"ta-{RUN}-buy"
    forged = replace(order, decision_id="dec-" + "b" * 32)              # pointing the order at another decision
    with pytest.raises(op.OrderRejected, match="valid approval"):
        broker.submit(forged, LIMITS, on_approved=lambda o: decisions.approve(o))
    assert broker.trading.submitted == [] and decisions.entries() == []


# ================================================================== PAPER_ORDER
@pytest.mark.unit
def test_each_ledger_event_has_its_own_id_and_names_proposal_and_approval(tmp_path):
    broker, result, panel, decisions, proposals = _setup(tmp_path)
    receipt = _submit(panel, result, broker, decisions, proposals)
    approved, submitted = ap.OrderLedger.for_results_dir(tmp_path).entries()
    [approval] = decisions.approvals()
    assert (approved["event"], submitted["event"]) == ("approved", "submitted")
    assert approved["event_id"] != submitted["event_id"] and all(
        e["event_id"].startswith("evt-") and len(e["event_id"]) == 36 for e in (approved, submitted))
    for line in (approved, submitted):
        assert line["client_order_id"] == line["proposal_id"] == panel.proposal_id == proposals.load(
            panel.proposal_id)["client_order_id"]
        assert line["approval_decision_id"] == approval["decision_id"] and line["run_id"] == RUN
        assert line["schema"] == ap.LEDGER_SCHEMA
    assert submitted["order_id"] == receipt.order_id and "order_id" not in approved
    assert broker.ledger.for_order(panel.client_order_id) == [approved, submitted]


@pytest.mark.unit
def test_a_failed_submission_can_be_retried_and_still_places_one_order(tmp_path):
    broker, result, panel, decisions, proposals = _setup(tmp_path)
    real_submit = broker.trading.submit_order
    broker.trading.submit_order = lambda order_data: (_ for _ in ()).throw(RuntimeError("503 unavailable"))
    with pytest.raises(ap.SubmissionFailed):
        _submit(panel, result, broker, decisions, proposals)
    broker.trading.submit_order = real_submit
    _submit(panel, result, broker, decisions, proposals)
    assert _submit(panel, result, broker, decisions, proposals).duplicate        # a repeat: nothing new
    ledger = broker.ledger.entries()
    assert [e["event"] for e in ledger] == ["approved", "failed", "approved", "submitted"]
    first, second = decisions.approvals()
    assert [e["approval_decision_id"] for e in ledger] == [first["decision_id"]] * 2 + [second["decision_id"]] * 2
    failure = next(e for e in decisions.entries() if e["event"] == "submission_failed")
    assert failure["category"] == "alpaca_api_error" and failure["approval_decision_id"] == first["decision_id"]
    assert len(broker.trading.submitted) == 1 and sum(1 for e in ledger if e.get("order_id")) == 1
    assert len({e["event_id"] for e in ledger}) == 4


@pytest.mark.unit
def test_an_old_ledger_line_still_blocks_a_second_order(tmp_path):
    cid = f"ta-{RUN}-buy"
    old = {"event": "submitted", "recorded_at": "2026-11-22T23:02:20+00:00", "run_id": RUN, "ticker": "NVDA",
           "client_order_id": cid, "order_id": "11111111-2222-3333-4444-555555555555", "status": "accepted",
           "symbol": "NVDA", "side": "buy", "qty": 2.0, "submitted_at": "2026-11-22T23:02:20+00:00"}
    path = tmp_path / ap.LEDGER_NAME
    path.write_text(json.dumps(old, sort_keys=True) + "\n")
    before = path.read_bytes()
    broker, result, panel, decisions, proposals = _setup(tmp_path)
    assert panel.already_submitted is not None
    receipt = _submit(panel, result, broker, decisions, proposals)
    assert receipt.duplicate and receipt.order_id == old["order_id"] and broker.trading.submitted == []
    assert decisions.approvals() == [] and path.read_bytes() == before       # no approval, nothing appended
    [entry] = broker.ledger.entries()
    assert entry["legacy"] is True and entry["event_id"].startswith("evt-legacy-")
    assert entry["event_id"] == broker.ledger.entries()[0]["event_id"]      # stable across reads


@pytest.mark.unit
def test_concurrent_submissions_place_one_order_and_record_one_approval(tmp_path):
    broker, result, panel, decisions, proposals = _setup(tmp_path)
    receipts, errors = [], []

    def go():
        try:
            receipts.append(_submit(panel, result, broker, decisions, proposals))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and sum(not r.duplicate for r in receipts) == 1 and len(broker.trading.submitted) == 1
    assert len(decisions.approvals()) == 1
    assert [e["event"] for e in broker.ledger.entries()] == ["approved", "submitted"]


@pytest.mark.unit
def test_only_the_paper_endpoint_is_used_and_a_refusal_records_no_approval(tmp_path):
    broker, result, panel, decisions, proposals = _setup(tmp_path)
    broker.trading._base_url = "https://api.alpaca.markets"           # a live-trading client
    with pytest.raises(ap.BrokerNotConfigured, match="only Alpaca paper trading"):
        _submit(panel, result, broker, decisions, proposals)
    assert broker.trading.submitted == [] and decisions.approvals() == [] and broker.ledger.entries() == []


@pytest.mark.unit
def test_an_unsigned_in_approver_records_no_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_REQUIRE_ORDER_AUTH", "true")
    broker, result, panel, decisions, proposals = _setup(tmp_path)
    with pytest.raises(op.OrderRejected):
        _submit(panel, result, broker, decisions, proposals, identity=None)
    assert decisions.approvals() == [] and broker.trading.submitted == []
    assert [e["category"] for e in decisions.entries()] == ["missing_approval"]


# ================================================================== MCP_REQUEST
@pytest.fixture
def mcp_results(tmp_path, monkeypatch):
    folder = tmp_path / "results"
    monkeypatch.setattr(tools, "results_dir", lambda: folder)
    monkeypatch.delenv(tools.SOURCES_ENV, raising=False)
    from tests.test_mcp_read_tools import _record

    _record(folder, "r-ok")
    _record(folder, "r-failed", status="failed", reports=False)
    return folder


def _calls(calls, **server):
    fastmcp = pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    async def go():
        out = []
        async with fastmcp.Client(mcp_server.create_server(**server)) as client:
            for name, args in calls:
                try:
                    out.append((await client.call_tool(name, args)).data)
                except ToolError as exc:
                    out.append(str(exc))
        return out
    return asyncio.run(go())


def _no_analysis(*args, **kwargs):
    raise AssertionError("a read tool must never start an analysis")


@pytest.mark.unit
def test_every_read_tool_call_is_audited_with_its_outcome(mcp_results):
    _calls([("get_report", {"run_id": "r-ok"}), ("get_run_metrics", {"run_id": "r-ok"}),
            ("list_recent_runs", {"limit": 5}), ("propose_trade", {"run_id": "r-ok"}),
            ("get_report", {"run_id": "r-missing"}), ("get_report", {"run_id": "r-failed"}),
            ("list_recent_runs", {"limit": 0}), ("propose_trade", {"run_id": "../../etc/passwd"}),
            ("get_report", {"run_id": "r-ok", "section": "PKSECRET-not-a-section"})], run=_no_analysis)
    lines = mcp_audit.entries(mcp_results)
    got = [(e["tool"], e["status"], e["error_category"], e["run_id"]) for e in lines]
    assert got == [
        ("get_report", "ok", None, "r-ok"), ("get_run_metrics", "ok", None, "r-ok"),
        ("list_recent_runs", "ok", None, None), ("propose_trade", "ok", None, "r-ok"),
        ("get_report", "error", "run_not_found", None), ("get_report", "error", "report_unavailable", "r-failed"),
        ("list_recent_runs", "error", "invalid_request", None), ("propose_trade", "error", "invalid_request", None),
        ("get_report", "error", "invalid_request", "r-ok")]
    assert len({e["request_id"] for e in lines}) == len(lines)
    assert all(e["request_id"].startswith("mcp-") and len(e["request_id"]) == 36 for e in lines)
    assert all(e["elapsed_seconds"] >= 0 and e["started_at"] <= e["finished_at"] for e in lines)
    assert lines[2]["arguments"] == {"limit": 5} and lines[0]["arguments"] == {"run_id": "r-ok", "section": None}
    assert lines[-1]["arguments"] == {"run_id": "<str>", "section": "<str>"}       # a refused call keeps types
    text = (mcp_results / mcp_audit.LOG_NAME).read_text()
    assert "PKSECRET" not in text and "passwd" not in text and "Traceback" not in text
    for entry in lines:                                                  # logical FK -> ANALYSIS_RUN
        assert entry["run_id"] is None or run_store.load(mcp_results, entry["run_id"]) is not None
    assert sorted(p.name for p in mcp_results.iterdir()) == [mcp_audit.LOG_NAME, f"{mcp_audit.LOG_NAME}.lock", "runs"]


@pytest.mark.unit
def test_analyze_stock_is_audited_with_its_run_and_every_failure_kind(mcp_results):
    def slow(request, on_event=None, cancel=None):
        cancel.wait(10)
        raise service.AnalysisCancelled(request.run_id, "at the next step")

    def broken(request, on_event=None, cancel=None):
        raise RuntimeError("provider exploded")

    timed_out = _calls([("analyze_stock", {"ticker": "NVDA", "trade_date": TRADE_DATE})], run=slow, timeout=0.3)
    failed = _calls([("analyze_stock", {"ticker": "NVDA", "trade_date": TRADE_DATE})], run=broken)
    refused = _calls([("analyze_stock", {"ticker": "PKSECRET/../x", "trade_date": TRADE_DATE})], run=_no_analysis)
    assert "timed out" in timed_out[0] and "invalid request" in refused[0] and isinstance(failed[0], str)
    timeout, failure, invalid = mcp_audit.entries(mcp_results)
    assert (timeout["status"], timeout["error_category"]) == ("timeout", "timeout")
    assert timeout["run_id"] and timeout["run_id"] in timed_out[0] and 0.25 < timeout["elapsed_seconds"] < 10
    assert (failure["status"], failure["error_category"]) == ("error", service.error_category(RuntimeError("x")))
    assert failure["run_id"] and failure["run_id"] != timeout["run_id"]
    assert (invalid["status"], invalid["error_category"], invalid["run_id"]) == ("error", "invalid_request", None)
    assert invalid["arguments"] == {"ticker": "<str>", "trade_date": "<str>", "analysts": None,
                                    "include_reports": "<bool>"}
    assert "PKSECRET" not in (mcp_results / mcp_audit.LOG_NAME).read_text()


@pytest.mark.unit
def test_a_real_analysis_and_its_reads_are_audited_against_its_run(phase2_on):  # noqa: F811
    [first] = _calls([("analyze_stock", {"ticker": "NVDA", "trade_date": TRADE_DATE, "analysts": ["market"]})])
    run_id = first["run_id"]
    _calls([("get_report", {"run_id": run_id}), ("get_run_metrics", {"run_id": run_id}),
            ("list_recent_runs", {}), ("propose_trade", {"run_id": run_id})])
    lines = mcp_audit.entries()
    assert [e["tool"] for e in lines] == list(mcp_audit.TOOLS)          # all five tools
    assert all(e["status"] == "ok" for e in lines)
    assert [e["run_id"] for e in lines] == [run_id, run_id, run_id, None, run_id]
    assert lines[0]["arguments"] == {"ticker": "NVDA", "trade_date": TRADE_DATE, "analysts": ["market"],
                                     "include_reports": False}
    record = run_store.load(tools.results_dir(), run_id)
    assert record["source"] == "mcp" and record["schema"] == 2 and record["started_at"] <= record["recorded_at"]
    results = tools.results_dir()
    assert not (results / "proposals").exists() and not (results / DECISIONS).exists()
    assert not (results / ap.LEDGER_NAME).exists()                       # nothing trade-related written


@pytest.mark.unit
def test_an_audit_log_that_cannot_be_written_changes_no_answer(mcp_results):
    (mcp_results / mcp_audit.LOG_NAME).mkdir()                           # a folder where the log should be
    report, metrics = _calls([("get_report", {"run_id": "r-ok"}), ("get_run_metrics", {"run_id": "r-ok"})],
                             run=_no_analysis)
    assert report["rating"] == "Overweight" and metrics["status"] == "completed"


@pytest.mark.unit
def test_concurrent_calls_each_get_one_whole_line(mcp_results):
    fastmcp = pytest.importorskip("fastmcp")

    async def go():
        async with fastmcp.Client(mcp_server.create_server(run=_no_analysis)) as client:
            await asyncio.gather(*[client.call_tool("get_run_metrics", {"run_id": "r-ok"}) for _ in range(20)])

    asyncio.run(go())
    lines = (mcp_results / mcp_audit.LOG_NAME).read_text().splitlines()
    assert len(lines) == 20 and len({json.loads(line)["request_id"] for line in lines}) == 20


@pytest.mark.unit
def test_auditing_adds_no_material_latency(tmp_path):
    started = time.perf_counter()
    for _ in range(200):
        mcp_audit.Request("get_run_metrics", {"run_id": "r-ok"}, run_id="r-ok", results_dir=tmp_path).finish("ok")
    per_call = (time.perf_counter() - started) / 200
    assert per_call < 0.02, f"{per_call * 1000:.1f} ms per audit line"
    assert len(audit_log.read(tmp_path / mcp_audit.LOG_NAME)) == 200


@pytest.mark.unit
def test_a_request_is_recorded_once(tmp_path):
    request = mcp_audit.Request("list_recent_runs", {"limit": 10}, results_dir=tmp_path)
    assert request.finish("ok") is not None and request.finish("error", "internal_error") is None
    assert len(mcp_audit.entries(tmp_path)) == 1


@pytest.mark.unit
def test_rejected_http_calls_record_nothing_and_never_the_token(mcp_results):
    pytest.importorskip("fastmcp")
    from starlette.testclient import TestClient

    token = "s3cret-token-0123456789-abcdef"
    app = mcp_server.create_server(auth=mcp_server.api_key_verifier(token), run=_no_analysis).http_app()
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_run_metrics", "arguments": {"run_id": "r-ok"}}}
    with TestClient(app, base_url="http://127.0.0.1") as http:
        for presented in (None, "Bearer wrong-token"):
            sent = {**headers, **({"Authorization": presented} if presented else {})}
            assert http.post("/mcp", json=body, headers=sent).status_code == 401
    assert mcp_audit.entries(mcp_results) == []                         # refused before any tool ran
    assert not (mcp_results / mcp_audit.LOG_NAME).exists() or token not in (
        mcp_results / mcp_audit.LOG_NAME).read_text()
