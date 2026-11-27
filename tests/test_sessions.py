"""Server-side dashboard sessions, persistent sign-in limits, step-up approval and session binding."""

from __future__ import annotations

import sqlite3
import stat
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.test_alpaca_paper import RUN, FakeData, FakeTrading
from tradingagents.execution import alpaca_paper as ap, identity as auth, order_policy as op

LIMITS = op.OrderLimits(1000.0)
PASSWORD = "correct horse battery"


@pytest.fixture
def users():
    return {"alice": auth.hash_password(PASSWORD), "bob": auth.hash_password(PASSWORD)}


@pytest.fixture
def store(tmp_path):
    return auth.SessionStore(tmp_path / "auth.sqlite")


@pytest.fixture
def clock(monkeypatch):
    """Move time forward for the identity module (sessions, step-up)."""
    base = auth._now()
    state = {"offset": timedelta(0)}
    monkeypatch.setattr(auth, "_now", lambda: base + state["offset"])

    def advance(**delta):
        state["offset"] += timedelta(**delta)
    return advance


def _sign_in(users, store, who="alice"):
    return auth.sign_in_with_password(who, PASSWORD, users, auth.LoginThrottle(), store=store)


# ------------------------------------------------------------------ sessions
@pytest.mark.unit
def test_a_sign_in_opens_a_server_side_session(users, store, tmp_path):
    identity = _sign_in(users, store)
    assert identity.session and auth.verify_identity(identity, store) == []
    with sqlite3.connect(tmp_path / "auth.sqlite") as db:
        [(ref,)] = db.execute("SELECT ref FROM sessions").fetchall()
    assert ref == auth.session_ref(identity.session) and identity.session not in ref   # only the hash is kept
    assert stat.S_IMODE((tmp_path / "auth.sqlite").stat().st_mode) == 0o600


@pytest.mark.unit
def test_an_idle_session_expires_and_activity_keeps_it_alive(users, store, clock):
    identity = _sign_in(users, store)
    clock(minutes=20)
    assert auth.verify_identity(identity, store) == []          # activity at 20 min
    clock(minutes=20)
    assert auth.verify_identity(identity, store) == []          # 20 min after the last activity
    clock(minutes=31)
    assert any("idle" in p for p in auth.verify_identity(identity, store))


@pytest.mark.unit
def test_a_session_expires_after_its_absolute_lifetime_despite_activity(users, store, clock):
    identity = _sign_in(users, store)
    for _ in range(17):                                          # active every 29 min for 8 h 13 min
        clock(minutes=29)
        auth.verify_identity(identity, store)
    assert any("expired" in p for p in auth.verify_identity(identity, store))


@pytest.mark.unit
def test_sign_out_revokes_the_session(users, store):
    identity = _sign_in(users, store)
    store.revoke(identity.session)
    assert any("signed out" in p for p in auth.verify_identity(identity, store))


@pytest.mark.unit
def test_a_restart_voids_sessions_but_keeps_the_sign_in_limits(users, store, tmp_path, monkeypatch):
    identity = _sign_in(users, store)
    for _ in range(auth.MAX_FAILURES_PER_USER):
        with pytest.raises(auth.AuthError):
            auth.sign_in_with_password("bob", "wrong password!!", users, auth.LoginThrottle(), store=store)
    monkeypatch.setattr(auth, "_INSTANCE", "a-new-server-start")
    restarted = auth.SessionStore(tmp_path / "auth.sqlite")
    assert any("restarted" in p for p in auth.verify_identity(identity, restarted))
    with pytest.raises(auth.AuthError, match="Too many"):           # the lockout survived the restart
        auth.sign_in_with_password("bob", PASSWORD, users, auth.LoginThrottle(), store=restarted)


@pytest.mark.unit
def test_the_overall_limit_blunts_guessing_across_usernames(users, store):
    for n in range(auth.MAX_FAILURES_OVERALL):
        with pytest.raises(auth.AuthError):
            auth.sign_in_with_password(f"user{n}", "nope nope nope", users, auth.LoginThrottle(), store=store)
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.sign_in_with_password("alice", PASSWORD, users, auth.LoginThrottle(), store=store)


@pytest.mark.unit
def test_an_identity_without_a_session_fails_a_store_check(store):
    assert any("server-side session" in p for p in auth.verify_identity(auth._issue("alice", "password"), store))


# ------------------------------------------------------------------ step-up approval
def _approve(identity):
    return op.approve(op.propose_order("NVDA", "Buy", 0), 2, 100.0, run_id=RUN, buying_power=1e6,
                      held_long_qty=0, limits=LIMITS, confirmed=True, identity=identity, limit_price=100.0)


@pytest.mark.unit
def test_approving_needs_a_recent_password_confirmation(users, store, clock):
    identity = _sign_in(users, store)
    clock(minutes=6)                                             # signed in, but not confirmed recently
    with pytest.raises(op.OrderRejected, match="Re-enter your password"):
        _approve(identity)
    confirmed = auth.confirm_password(identity, PASSWORD, users, store)
    order = _approve(confirmed)
    assert order.session_ref == auth.session_ref(identity.session)


@pytest.mark.unit
def test_a_wrong_confirmation_approves_nothing_and_counts_as_a_failure(users, store):
    identity = _sign_in(users, store)
    with pytest.raises(auth.AuthError, match="not right"):
        auth.confirm_password(identity, "wrong password!!", users, store)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM attempts WHERE ok = 0").fetchone()[0] == 1


@pytest.mark.unit
def test_a_confirmation_cannot_revive_a_signed_out_session(users, store):
    identity = _sign_in(users, store)
    store.revoke(identity.session)
    with pytest.raises(auth.AuthError, match="signed out"):
        auth.confirm_password(identity, PASSWORD, users, store)


# ------------------------------------------------------------------ the broker re-checks the session
@pytest.fixture
def broker(tmp_path, store, monkeypatch):
    monkeypatch.setattr(ap, "_order_request", lambda symbol, qty, side, cid, order_type, limit_price: SimpleNamespace(
        symbol=symbol, qty=qty, side=side.value, type=order_type.value, limit_price=limit_price, client_order_id=cid))
    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: SimpleNamespace(symbol=symbol))
    return ap.AlpacaPaperBroker(FakeTrading(), FakeData(), ledger=ap.OrderLedger.for_results_dir(tmp_path / "l"),
                                sessions=store)


@pytest.mark.unit
def test_an_order_goes_out_while_its_session_is_active(users, store, broker):
    order = _approve(_sign_in(users, store))
    assert not broker.submit(order, LIMITS).duplicate
    assert ap.OrderLedger.for_results_dir(broker.ledger.path.parent).entries()[-1]["session_bound"] is True


@pytest.mark.unit
def test_signing_out_between_approval_and_submission_stops_the_order(users, store, broker):
    identity = _sign_in(users, store)
    order = _approve(identity)
    store.revoke(identity.session)
    with pytest.raises(op.OrderRejected, match="signed out"):
        broker.submit(order, LIMITS)
    assert broker.trading.submitted == []


@pytest.mark.unit
def test_an_approval_without_a_session_is_refused_by_a_session_aware_broker(broker):
    order = _approve(auth._issue("alice", "password"))           # signed, but bound to no session
    with pytest.raises(op.OrderRejected, match="not bound to a dashboard session"):
        broker.submit(order, LIMITS)
    assert broker.trading.submitted == []


# ------------------------------------------------------------------ creating dashboard users
@pytest.mark.unit
def test_add_user_writes_a_private_hash_file(tmp_path):
    import json as _json

    path = tmp_path / "dashboard_users.json"
    auth.add_user(path, "alice", PASSWORD)
    auth.add_user(path, "bob", "another long password")
    users = _json.loads(path.read_text())
    assert set(users) == {"alice", "bob"} and PASSWORD not in path.read_text()
    assert users["alice"].startswith("scrypt$") and auth.check_password(PASSWORD, users["alice"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    auth.add_user(path, "alice", "a new long password")               # replaced, bob kept
    users = _json.loads(path.read_text())
    assert auth.check_password("a new long password", users["alice"]) and "bob" in users
    with pytest.raises(ValueError):
        auth.add_user(path, "two words", PASSWORD)
    with pytest.raises(ValueError, match="12 characters"):
        auth.add_user(path, "carol", "short")


@pytest.mark.unit
def test_the_add_user_command_never_echoes_the_password(tmp_path, monkeypatch, capsys):
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": PASSWORD)
    path = tmp_path / "users.json"
    auth.main(["add-user", "alice", "--file", str(path)])
    out = capsys.readouterr().out
    assert PASSWORD not in out and "mode 0600" in out and auth.USERS_FILE_ENV in out
    assert auth.sign_in_with_password("alice", PASSWORD, auth.load_users(path), auth.LoginThrottle())


@pytest.mark.unit
def test_secret_and_session_files_are_ignored_by_git():
    import subprocess

    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    for path in (".env", "dashboard_users.json", "config/dashboard_users.json", "results/dashboard_auth.sqlite",
                 "results/paper_orders.jsonl", "results/phase3_smoke/x/smoke_report.json"):
        assert subprocess.run(["git", "check-ignore", "-q", path], cwd=root).returncode == 0, path
    # Evidence stays visible: E2 caches and E3 summaries are not swept up by these rules.
    for path in ("eval/report_tables.html", "results/phase2/e2_benchmark.csv", "consistency_evaluation/x_e3.json"):
        assert subprocess.run(["git", "check-ignore", "-q", path], cwd=root).returncode == 1, path
