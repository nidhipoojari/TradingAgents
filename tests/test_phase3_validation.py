"""Phase 3 final validation: restarts void sign-ins and approvals, localhost-only serving,
MCP defaults, and E2 kept exactly as Phase 2 defined it."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents.execution import alpaca_paper as ap, identity as auth, order_policy as op

ROOT = Path(__file__).resolve().parents[1]
RUN = "20261122-101500-ab12cd34"

# What a dashboard process signs: one sign-in and one approved limit order.
PREVIOUS_PROCESS = r"""
import json
from dataclasses import asdict
from tradingagents.execution import identity as auth, order_policy as op
users = {"alice": auth.hash_password("correct horse battery")}
who = auth.sign_in_with_password("alice", "correct horse battery", users, auth.LoginThrottle())
order = op.approve(op.propose_order("NVDA", "Buy", 0), 2, 100.0, run_id="RUN_ID", buying_power=1e6,
                   held_long_qty=0, limits=op.OrderLimits(1000.0), confirmed=True, identity=who,
                   limit_price=100.0)
assert auth.verify_identity(who) == [] and op.verify_approval(order) == []
print(json.dumps({"identity": asdict(who), "order": {k: getattr(v, "value", v) for k, v in asdict(order).items()}}))
""".replace("RUN_ID", RUN)


def _restore(data):
    who = auth.Identity(**data["identity"])
    fields = dict(data["order"])
    fields["side"], fields["order_type"] = op.Side(fields["side"]), op.OrderType(fields["order_type"])
    return who, op.ApprovedOrder(**fields)


@pytest.fixture(scope="module")
def from_before_a_restart():
    out = subprocess.run([sys.executable, "-c", PREVIOUS_PROCESS], cwd=ROOT, capture_output=True,
                         text=True, timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------ restarts
@pytest.mark.unit
def test_a_restart_voids_earlier_sign_ins(from_before_a_restart):
    who, _ = _restore(from_before_a_restart)
    assert who.subject == "alice" and who.signature           # a well-formed, signed identity...
    assert auth.verify_identity(who)                           # ...that this process does not accept


@pytest.mark.unit
def test_a_restart_voids_pending_approvals(from_before_a_restart):
    _, order = _restore(from_before_a_restart)
    assert order.approval
    assert any("valid approval" in p for p in op.verify_approval(order))


@pytest.mark.unit
def test_an_approval_from_before_a_restart_cannot_be_submitted(from_before_a_restart, tmp_path, monkeypatch):
    from tests.test_alpaca_paper import FakeData, FakeTrading

    monkeypatch.setattr(ap, "_order_request", lambda *a: pytest.fail("nothing may be sent"))
    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: SimpleNamespace(symbol=symbol))
    _, order = _restore(from_before_a_restart)
    broker = ap.AlpacaPaperBroker(FakeTrading(), FakeData(), ledger=ap.OrderLedger.for_results_dir(tmp_path))
    with pytest.raises(op.OrderRejected, match="valid approval"):
        broker.submit(order, op.OrderLimits(1000.0))
    assert broker.trading.submitted == [] and ap.OrderLedger.for_results_dir(tmp_path).entries() == []


@pytest.mark.unit
def test_the_same_values_signed_here_are_accepted():
    """Control: it is the process key that voids them, not the round trip through JSON."""
    users = {"alice": auth.hash_password("correct horse battery")}
    who = auth.sign_in_with_password("alice", "correct horse battery", users, auth.LoginThrottle())
    order = op.approve(op.propose_order("NVDA", "Buy", 0), 2, 100.0, run_id=RUN, buying_power=1e6,
                       held_long_qty=0, limits=op.OrderLimits(1000.0), confirmed=True, identity=who,
                       limit_price=100.0)
    data = {"identity": asdict(who), "order": {k: getattr(v, "value", v) for k, v in asdict(order).items()}}
    restored_who, restored_order = _restore(json.loads(json.dumps(data)))
    assert auth.verify_identity(restored_who) == [] and op.verify_approval(restored_order) == []


@pytest.mark.unit
def test_only_password_sign_in_is_accepted():
    assert {"password"} == auth.AUTHENTICATED_METHODS
    assert not hasattr(auth, "identity_from_oidc")


# ------------------------------------------------------------------ localhost-only dashboard
@pytest.fixture
def launched(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.web import cli as streamlit_cli

    seen = {}
    monkeypatch.setattr(streamlit_cli, "main", lambda: seen.setdefault("argv", list(sys.argv)) and 0)
    return seen


@pytest.mark.unit
@pytest.mark.parametrize("args, address", [
    ([], "--server.address=127.0.0.1"),
    (["--server.address=localhost"], "--server.address=localhost"),
    (["--server.address", "127.0.0.1"], "--server.address"),
])
def test_the_dashboard_serves_this_machine(launched, monkeypatch, args, address):
    from tradingagents.dashboard import launch

    monkeypatch.setattr(sys, "argv", ["tradingagents-dashboard", *args])
    with pytest.raises(SystemExit):
        launch.main()
    assert launched["argv"][:2] == ["streamlit", "run"] and address in launched["argv"]


@pytest.mark.unit
@pytest.mark.parametrize("args", [["--server.address=0.0.0.0"], ["--server.address", "192.168.1.20"],
                                  ["--server.address=::"], ["--server.address=dashboard.example.org"]])
def test_the_dashboard_refuses_other_addresses(launched, monkeypatch, args):
    from tradingagents.dashboard import launch

    monkeypatch.setattr(sys, "argv", ["tradingagents-dashboard", *args])
    with pytest.raises(SystemExit, match="serves this machine only"):
        launch.main()
    assert "argv" not in launched


# ------------------------------------------------------------------ MCP defaults
@pytest.mark.unit
def test_stdio_is_the_default_mcp_transport(monkeypatch):
    from tradingagents import mcp_server

    seen = {}

    class Server:
        def run(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.delenv(mcp_server.TOKEN_ENV, raising=False)
    monkeypatch.setattr(mcp_server, "create_server", lambda **k: seen.setdefault("auth", k.get("auth")) or Server())
    mcp_server.main([])
    assert seen["transport"] == "stdio" and seen["auth"] is None


# ------------------------------------------------------------------ E2 exactly as Phase 2 defined it
PHASE2_SCHEMA = ("CREATE TABLE cache ( tool TEXT, request TEXT, result TEXT, saved_at TEXT,"
                 " PRIMARY KEY (tool, request))")


@pytest.fixture
def e2(tmp_path, monkeypatch):
    from tradingagents import e2_cache
    from tradingagents.dataflows import config as dataflows_config

    monkeypatch.setitem(dataflows_config._config, "data_cache_dir", str(tmp_path))
    monkeypatch.setitem(dataflows_config._config, "e2_cache_enabled", True)
    return e2_cache


@pytest.mark.unit
def test_the_e2_sqlite_schema_is_unchanged(e2, tmp_path):
    e2.save("get_stock_data", ("NVDA", "2026-01-02", "2026-01-09"), {}, "rows")
    with sqlite3.connect(tmp_path / "e2_cache.sqlite") as db:
        [(sql,)] = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table'").fetchall()
    assert " ".join(sql.split()) == " ".join(PHASE2_SCHEMA.split())


@pytest.mark.unit
def test_e2_caches_only_past_dated_successful_answers(e2):
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    past = ("NVDA", "2026-01-02", "2026-01-09")
    e2.save("get_stock_data", past, {}, "rows")
    e2.save("get_stock_data", ("NVDA", yesterday, today), {}, "today's rows")       # touches today
    e2.save("get_insider_transactions", ("NVDA",), {}, "no date at all")
    e2.save("get_news", ("NVDA", "2026-01-02", "2026-01-09"), {}, "DATA_UNAVAILABLE: x")
    e2.reset_stats()
    assert e2.lookup("get_stock_data", past, {}) == "rows"
    assert e2.lookup("get_stock_data", ("NVDA", yesterday, today), {}) is None
    assert e2.lookup("get_insider_transactions", ("NVDA",), {}) is None
    assert e2.lookup("get_news", ("NVDA", "2026-01-02", "2026-01-09"), {}) is None
    # The same request with the arguments named differently is a different key: exact matching.
    assert e2.lookup("get_stock_data", (), {"symbol": "NVDA", "start_date": "2026-01-02",
                                            "end_date": "2026-01-09"}) is None
    assert e2.stats_snapshot() == {"hits": 1, "misses": 2, "skipped": 2, "hit_rate_pct": 33.3}
