"""Wall-clock steps (as WSL2's +185.7 s on 2026-11-25) must not move deadlines, shorten or extend sign-ins and
approvals, or let a paper order through. Offline.

A step changes the wall clock (time.time, datetime.now) and leaves the monotonic clock alone; these tests do
the same: they shift only the wall-clock reads of the module under test.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.test_alpaca_paper import FakeData, FakeTrading, _no_alpaca_sdk  # noqa: F401
from tradingagents import run_deadline
from tradingagents.execution import alpaca_paper as ap, identity as auth, order_policy as op
from tradingagents.run_deadline import DeadlineGuard, RunDeadline

FORWARD = 185.7          # the step seen on 2026-11-25
BACKWARD = -3600.0


class WallClock:
    """time.time() for one module, plus an offset; monotonic, sleep and perf_counter stay real."""

    def __init__(self):
        self.offset = 0.0

    def namespace(self):
        return SimpleNamespace(time=lambda: time.time() + self.offset, monotonic=time.monotonic,
                               sleep=time.sleep, perf_counter=time.perf_counter)


@pytest.fixture
def wall(monkeypatch):
    clock = WallClock()
    for module in (run_deadline,):
        monkeypatch.setattr(module, "time", clock.namespace())
    return clock


# ------------------------------------------------------------------ deadlines (in the calling process)
@pytest.mark.unit
def test_a_forward_step_does_not_end_a_run_early(wall):
    deadline = RunDeadline(analysis_seconds=180, step_seconds=60)
    guard = DeadlineGuard(deadline, threading.Event())
    guard._admit("call-1", "step-1")
    wall.offset = FORWARD                                   # the wall clock jumps past the 180 s deadline
    assert guard.check_analysis() is False and not guard.cancel.is_set()
    guard._admit("call-2", "step-1")                        # the same step: not 60 s old
    assert 175 < deadline.remaining() <= 180
    assert guard.snapshot()["elapsed_seconds"] < 5


@pytest.mark.unit
def test_a_backward_step_does_not_disable_the_deadlines(wall):
    from tradingagents.service import _CancelSignal

    deadline = RunDeadline(analysis_seconds=0.3, step_seconds=60)
    guard = DeadlineGuard(deadline, threading.Event())
    wall.offset = BACKWARD
    time.sleep(0.4)
    assert guard.check_analysis() is True and guard.which == "analysis"

    step = DeadlineGuard(RunDeadline(analysis_seconds=180, step_seconds=0.3), threading.Event())
    step._admit("call-1", "step-1")
    time.sleep(0.4)                                         # 0.4 s pass, though the wall clock went back an hour
    with pytest.raises(_CancelSignal):
        step._admit("call-2", "step-1")
    assert step.which == "step"


@pytest.mark.unit
def test_the_reported_time_against_the_target_ignores_a_step(wall):
    deadline = RunDeadline(analysis_seconds=180, target_seconds=41)
    wall.offset = FORWARD
    result = run_deadline.outcome(deadline)
    assert result["elapsed_seconds"] < 5 and result["target_met"] is True


@pytest.mark.unit
def test_a_deadline_handed_to_a_worker_keeps_its_elapsed_time():
    deadline = RunDeadline(analysis_seconds=180)
    time.sleep(0.2)
    sent = deadline.for_handoff()                           # what the parent pickles to the child
    adopted = sent.adopted()                                # the child's view, on its own monotonic clock
    assert 0.15 < adopted.elapsed() < 1.0 and adopted.started_at == deadline.started_at


# ------------------------------------------------------------------ deadlines enforced by the parent
def _worker_env(tmp_path, monkeypatch):
    for name, value in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "offline", "LANGFUSE_SECRET_KEY": "offline",
                        "LANGFUSE_BASE_URL": "http://localhost.invalid", "E3_RESULTS_DIR": str(tmp_path / "e3"),
                        "WORKER_FAKE_MARKER": str(tmp_path / "marker")}.items():
        monkeypatch.setenv(name, value)


@pytest.mark.unit
@pytest.mark.parametrize("offset", [FORWARD, BACKWARD])
def test_the_parent_enforces_the_step_deadline_through_a_clock_step(tmp_path, monkeypatch, offset):
    from tests.test_worker import _request
    from tradingagents import service, worker

    _worker_env(tmp_path, monkeypatch)
    deadline = RunDeadline(analysis_seconds=120, step_seconds=2.0, grace_seconds=1.0)
    clock = WallClock()
    clock.offset = offset                                   # the step happens right after the request
    for module in (worker, run_deadline):
        monkeypatch.setattr(module, "time", clock.namespace())
    cancel = threading.Event()
    watchdog = threading.Timer(40.0, cancel.set)            # a deadline that never fires must not hang the suite
    watchdog.start()
    started = time.monotonic()
    try:
        with pytest.raises(service.AnalysisCancelled) as caught:
            worker.run_in_worker(_request(tmp_path, analysts=("market",), deadline=deadline), cancel=cancel,
                                 setup="tests.worker_fakes:hung_market_call")
    finally:
        watchdog.cancel()
    exc = caught.value
    assert isinstance(exc, service.AnalysisDeadlineExceeded), f"not stopped by a deadline: {exc}"
    assert exc.deadline["which"] == "step" and exc.deadline["enforced_by"] == "parent"
    assert time.monotonic() - started < 30 and exc.deadline["elapsed_seconds"] < 30


# ------------------------------------------------------------------ sign-in, step-up, sessions, approvals
class Clocks:
    """Separate wall and monotonic time for identity and order_policy: real time passes on the monotonic
    clock; the wall clock shows that time plus a step."""

    def __init__(self, monkeypatch):
        self.wall0, self.mono0 = auth._now(), 1_000_000.0
        self.real = 0.0
        self.step = 0.0
        for module in (auth, op):
            monkeypatch.setattr(module, "_now", lambda: self.wall0 + timedelta(seconds=self.real + self.step))
            monkeypatch.setattr(module, "_monotonic", lambda: self.mono0 + self.real, raising=False)

    def advance(self, seconds, step=0.0):
        self.real += seconds
        self.step += step


@pytest.fixture
def clocks(monkeypatch):
    return Clocks(monkeypatch)


@pytest.mark.unit
def test_a_backward_step_never_extends_a_sign_in(clocks):
    identity = auth._issue("alice", "password")
    clocks.advance(auth.SESSION_TTL_SECONDS + 60, step=-3600)   # 8 h 1 min passed; the wall clock shows 7 h 1 min
    assert any("expired" in p for p in auth.verify_identity(identity))


@pytest.mark.unit
def test_a_forward_step_expires_a_sign_in_early_never_late(clocks):
    identity = auth._issue("alice", "password")
    clocks.advance(10, step=auth.SESSION_TTL_SECONDS)       # fail-safe: sign in again
    assert any("expired" in p for p in auth.verify_identity(identity))


@pytest.mark.unit
def test_a_backward_step_never_extends_a_password_confirmation(clocks):
    identity = auth._issue("alice", "password")
    clocks.advance(auth.STEP_UP_SECONDS + 10, step=-120)    # 5 min 10 s passed; the wall clock shows 3 min 10 s
    assert not auth.confirmed_recently(identity)
    clocks.advance(-(auth.STEP_UP_SECONDS + 10) + 60)       # (control) one real minute: still confirmed
    assert auth.confirmed_recently(auth._issue("bob", "password")) is True


@pytest.mark.unit
def test_a_backward_step_never_extends_a_server_side_session(clocks, tmp_path):
    store = auth.SessionStore(tmp_path / "auth.sqlite")
    token = store.open("alice", "password")
    clocks.advance(auth.IDLE_TIMEOUT_SECONDS + 10, step=-600)    # idle 30 min 10 s; the wall clock shows 20 min
    assert any("idle" in p for p in store.check(token))
    token = store.open("bob", "password")
    for _ in range(18):                                     # active every 29 min for 8 h 42 min
        clocks.advance(29 * 60, step=-100)
        problems = store.check(token)
    assert any("expired" in p for p in problems)


def _approve(identity):
    proposal = op.propose_order("NVDA", "Buy", 0.0)
    return op.approve(proposal, 2, 100.0, run_id="20261125-100000-abcdef12", buying_power=1e6, held_long_qty=0.0,
                      limits=op.OrderLimits(1000.0), confirmed=True, identity=identity, order_type="limit",
                      limit_price=100.0, market_open=True)


@pytest.mark.unit
def test_a_backward_step_never_extends_an_approval(clocks):
    order = _approve(auth._issue("alice", "password"))
    clocks.advance(op.APPROVAL_TTL_SECONDS + 10, step=-120)  # 5 min 10 s passed; the wall clock shows 3 min 10 s
    assert any("old, past the" in p for p in op.verify_approval(order))


@pytest.mark.unit
def test_a_forward_step_expires_an_approval_early_never_late(clocks):
    order = _approve(auth._issue("alice", "password"))
    clocks.advance(5, step=FORWARD + op.APPROVAL_TTL_SECONDS)
    assert op.verify_approval(order)


@pytest.mark.unit
def test_an_approval_stretched_by_a_backward_step_sends_nothing(clocks, tmp_path):
    broker = ap.AlpacaPaperBroker(FakeTrading(), FakeData(price=100.0), ledger=ap.OrderLedger.for_results_dir(tmp_path))
    order = _approve(auth._issue("alice", "password"))
    clocks.advance(op.APPROVAL_TTL_SECONDS + 10, step=-120)
    with pytest.raises(op.OrderRejected, match="old, past the"):
        broker.submit(order, op.OrderLimits(1000.0))
    assert broker.trading.submitted == []


@pytest.mark.unit
def test_no_clock_step_changes_a_fresh_approval(clocks):
    order = _approve(auth._issue("alice", "password"))
    clocks.advance(30)
    assert op.verify_approval(order) == []


@pytest.mark.unit
def test_a_step_between_the_click_and_the_deadline_does_not_age_it(wall):
    clicked, clicked_mono = time.time(), time.monotonic()        # the dashboard's Run click
    wall.offset = FORWARD                                       # the clock steps before the deadline is built
    deadline = run_deadline.interactive_deadline(started_at=clicked, started_mono=clicked_mono)
    assert deadline.elapsed() < 5 and not deadline.passed() and deadline.started_at == clicked
    built_during_a_step = RunDeadline(analysis_seconds=180)     # no times given: read now, no wall arithmetic
    assert built_during_a_step.elapsed() < 5 and built_during_a_step.remaining() > 175


@pytest.mark.unit
def test_the_investigation_plugins_import_and_are_opt_in(monkeypatch):
    """tests/plugins load only with -p, need no scratch path, and say which setting is missing."""
    import importlib

    from tests.plugins._settings import seconds

    for name in ("clock_step", "clock_jump", "late_start", "timeline"):
        importlib.import_module(f"tests.plugins.{name}")           # importing alone changes nothing
    monkeypatch.delenv("CLOCK_STEP_SECONDS", raising=False)
    with pytest.raises(RuntimeError, match="CLOCK_STEP_SECONDS=<seconds>"):
        seconds("CLOCK_STEP_SECONDS")
    assert time.time is not None and abs(time.time() - __import__("datetime").datetime.now().timestamp()) < 5
