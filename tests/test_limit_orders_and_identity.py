"""F1 hardening: limit orders, market hours, the final cap check, and signed-in approvers.

Backend-level: these call order_policy and AlpacaPaperBroker directly with fake
Alpaca clients, as a script that bypassed the dashboard would. No network.
"""

import dataclasses
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.test_alpaca_paper import RUN, FakeData, FakeTrading
from tradingagents.execution import alpaca_paper as ap, identity as auth, order_policy as op
from tradingagents.execution.order_policy import OrderLimits, OrderRejected, OrderType, Side

LIMITS = OrderLimits(max_notional=1000.0)
TESTER = auth._issue("tester", "password")


@pytest.fixture(autouse=True)
def _fresh_sign_in():
    """A fresh signed-in approver for every test. A password confirmation lasts 5 minutes of wall-clock
    time (identity.STEP_UP_SECONDS), so one issued once at collection expires in a run that reaches this
    test late, or after a system clock step (+185.7 s on 2026-11-25 failed 4 tests in test_trade_rejection).
    Set directly, not with monkeypatch, so a test's monkeypatch.undo() cannot bring back a stale one."""
    global TESTER
    TESTER = auth._issue("tester", "password")


@pytest.fixture
def fake_requests(monkeypatch):
    sent_requests = []

    def build(symbol, qty, side, cid, order_type, limit_price):
        request = SimpleNamespace(symbol=symbol, qty=qty, side=side.value, type=order_type.value,
                                  limit_price=limit_price, client_order_id=cid)
        sent_requests.append(request)
        return request

    monkeypatch.setattr(ap, "_order_request", build)
    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: SimpleNamespace(symbol=symbol))
    return sent_requests


def _broker(tmp_path, price=100.0, **trading):
    return ap.AlpacaPaperBroker(FakeTrading(**trading), FakeData(price=price),
                                ledger=ap.OrderLedger.for_results_dir(tmp_path))


_SIGNED_IN = object()     # "the current test's TESTER", resolved at call time (identity=None means no sign-in)


def _approve(qty=2, price=100.0, *, order_type=OrderType.LIMIT, limit_price=100.0, rating="Buy", held=0.0,
             market_open=True, identity=_SIGNED_IN, limits=LIMITS, **extra):
    # Not a default of identity=TESTER: a default is bound once, when the module is imported, so it would
    # keep the collection-time sign-in, whose 5-minute confirmation a late or clock-stepped run outlives.
    identity = TESTER if identity is _SIGNED_IN else identity
    return op.approve(op.propose_order("NVDA", rating, held), qty, price, run_id=RUN, buying_power=1e6,
                      held_long_qty=held, limits=limits, confirmed=True, identity=identity,
                      order_type=order_type, limit_price=limit_price, market_open=market_open, **extra)


def _nothing_sent(broker):
    assert broker.trading.submitted == []


# ------------------------------------------------------------------ limit orders
@pytest.mark.unit
def test_a_buy_limit_is_capped_at_its_limit_price_not_the_quote():
    buy = op.propose_order("NVDA", "Buy", 0)
    kwargs = {"buying_power": 1e6, "held_long_qty": 0, "limits": LIMITS, "order_type": OrderType.LIMIT}
    assert op.check_order(buy, 9, 50.0, limit_price=111.0, **kwargs) == []          # 9 x $111 = $999
    problems = op.check_order(buy, 9, 50.0, limit_price=112.0, **kwargs)            # $1,008 at the limit
    assert any("at the limit price" in p and "over" in p for p in problems)


@pytest.mark.unit
def test_buying_power_is_checked_at_the_limit_price():
    buy = op.propose_order("NVDA", "Buy", 0)
    problems = op.check_order(buy, 5, 10.0, buying_power=400.0, held_long_qty=0, limits=LIMITS,
                              order_type=OrderType.LIMIT, limit_price=90.0)          # $450 at the limit
    assert any("buying power" in p for p in problems)


@pytest.mark.unit
def test_a_sell_limit_is_valued_at_the_higher_of_limit_and_quote():
    sell = op.propose_order("NVDA", "Sell", 20)
    kwargs = {"buying_power": 0, "held_long_qty": 20, "limits": LIMITS, "order_type": OrderType.LIMIT}
    assert any("over" in p for p in op.check_order(sell, 10, 120.0, limit_price=90.0, **kwargs))  # $1,200
    assert op.check_order(sell, 9, 100.0, limit_price=110.0, **kwargs) == []                       # $990


@pytest.mark.unit
@pytest.mark.parametrize("limit, ok", [(100.12, True), (100.123, False), (0.1234, True), (0.12345, False),
                                       (0, False), (-5, False), (float("nan"), False), (None, False),
                                       ("100", False), (True, False)])
def test_limit_prices_are_whole_cents(limit, ok):
    assert (op.limit_price_problems(limit) == []) is ok


@pytest.mark.unit
def test_market_orders_are_refused_at_every_layer(tmp_path, fake_requests, monkeypatch):
    buy = op.propose_order("NVDA", "Buy", 0)
    kwargs = {"buying_power": 1e6, "held_long_qty": 0, "limits": LIMITS}
    for market_open in (True, False, None):                     # open or closed makes no difference
        assert op.check_order(buy, 1, 100.0, order_type="market", market_open=market_open,
                              **kwargs) == [op.MARKET_ORDERS_DISABLED]
    with pytest.raises(OrderRejected, match="Market orders are disabled"):
        op.approve(buy, 1, 100.0, run_id=RUN, confirmed=True, identity=TESTER, order_type="market", **kwargs)
    # Even an order that got past approval (approval check bypassed) is not sent.
    monkeypatch.setattr(ap, "verify_approval", lambda order: [])
    forged = dataclasses.replace(_approve(), order_type=OrderType.MARKET, limit_price=None)
    broker = _broker(tmp_path)
    with pytest.raises(OrderRejected, match="Market orders are disabled"):
        broker.submit(forged, LIMITS)
    _nothing_sent(broker)
    # And the last gate refuses a market request object outright.
    market_request = SimpleNamespace(symbol="NVDA", qty=1, side="buy", type="market", limit_price=None,
                                     client_order_id="ta-x-buy")
    with pytest.raises(OrderRejected, match="only limit orders are sent"):
        ap._final_cap_check(market_request, forged, 100.0, LIMITS)


@pytest.mark.unit
def test_a_limit_order_is_allowed_while_the_market_is_closed():
    buy = op.propose_order("NVDA", "Buy", 0)
    assert op.check_order(buy, 1, 100.0, buying_power=1e6, held_long_qty=0, limits=LIMITS,
                          market_open=False, order_type=OrderType.LIMIT, limit_price=100.0) == []





@pytest.mark.unit
def test_an_edited_limit_price_voids_the_approval():
    order = _approve(limit_price=100.0)
    assert op.verify_approval(order) == []
    assert op.verify_approval(dataclasses.replace(order, limit_price=150.0))
    assert op.verify_approval(dataclasses.replace(order, order_type=OrderType.MARKET, limit_price=None))


# ------------------------------------------------------------------ the broker: clock, type, final check
@pytest.mark.unit
def test_a_limit_order_is_sent_as_a_limit_order(tmp_path, fake_requests):
    broker = _broker(tmp_path)
    receipt = broker.submit(_approve(qty=3, limit_price=99.5), LIMITS)
    assert not receipt.duplicate
    [sent] = broker.trading.submitted
    assert (sent.type, sent.limit_price, sent.qty, sent.side) == ("limit", 99.5, 3, "buy")
    approved = ap.OrderLedger.for_results_dir(tmp_path).entries()[0]
    assert (approved["order_type"], approved["limit_price"], approved["market_open"]) == ("limit", 99.5, True)
    assert approved["checked_notional"] == 298.5 and approved["auth_method"] == "password"





@pytest.mark.unit
def test_a_limit_order_placed_after_hours_waits_for_the_open(tmp_path, fake_requests):
    broker = _broker(tmp_path, market_open=False)
    broker.submit(_approve(market_open=False), LIMITS)
    assert broker.trading.submitted[0].type == "limit"
    assert ap.OrderLedger.for_results_dir(tmp_path).entries()[0]["next_open"].startswith("2026-11-23")


@pytest.mark.unit
def test_a_clock_failure_sends_nothing(tmp_path, fake_requests):
    broker = _broker(tmp_path)

    def down():
        raise RuntimeError("clock unavailable")

    broker.trading.get_clock = down
    with pytest.raises(ap.BrokerError, match="market clock"):
        broker.submit(_approve(), LIMITS)
    _nothing_sent(broker)





@pytest.mark.unit
def test_the_final_check_refuses_a_request_that_differs_from_the_approval(tmp_path, monkeypatch, fake_requests):
    broker = _broker(tmp_path)
    monkeypatch.setattr(ap, "_order_request", lambda symbol, qty, side, cid, order_type, limit_price:
                        SimpleNamespace(symbol=symbol, qty=qty * 100, side=side.value, type=order_type.value,
                                        limit_price=limit_price, client_order_id=cid))
    with pytest.raises(OrderRejected, match="does not match"):
        broker.submit(_approve(), LIMITS)
    _nothing_sent(broker)


@pytest.mark.unit
def test_the_final_cap_check_stands_on_its_own(tmp_path, monkeypatch, fake_requests):
    """With the approval and order checks out of the way, the last gate still holds the cap."""
    monkeypatch.setattr(ap, "verify_approval", lambda order: [])
    monkeypatch.setattr(ap, "check_order", lambda *a, **k: [])
    broker = _broker(tmp_path)
    oversized = dataclasses.replace(_approve(qty=2, limit_price=100.0), qty=50)   # $5,000 at the limit
    with pytest.raises(OrderRejected, match="Final check"):
        broker.submit(oversized, LIMITS)
    _nothing_sent(broker)
    assert ap.OrderLedger.for_results_dir(tmp_path).entries() == []


@pytest.mark.unit
def test_real_alpaca_requests_are_regular_session_day_limit_orders():
    pytest.importorskip("alpaca")
    from alpaca.trading.enums import TimeInForce

    limit = ap._order_request("NVDA", 3, Side.BUY, "ta-x-buy", OrderType.LIMIT, 99.5)
    assert (limit.type.value, limit.limit_price, limit.time_in_force, limit.extended_hours) == (
        "limit", 99.5, TimeInForce.DAY, False)
    with pytest.raises(OrderRejected, match="Market orders are disabled"):
        ap._order_request("NVDA", 3, Side.SELL, "ta-x-sell", OrderType.MARKET, None)
    assert ap._final_cap_check(limit, SimpleNamespace(qty=3, side=Side.BUY, order_type=OrderType.LIMIT,
                                                      limit_price=99.5, client_order_id="ta-x-buy"),
                               100.0, LIMITS) == 99.5


# ------------------------------------------------------------------ signed-in approvers
@pytest.mark.unit
def test_passwords_are_stored_as_scrypt_hashes():
    stored = auth.hash_password("correct horse battery")
    assert stored.startswith("scrypt$") and "correct" not in stored
    assert auth.check_password("correct horse battery", stored)
    assert not auth.check_password("wrong password!!", stored)
    assert not auth.check_password("x", "not-a-hash")
    with pytest.raises(ValueError):
        auth.hash_password("short")


@pytest.mark.unit
def test_a_password_sign_in_issues_a_verified_identity():
    users = {"alice": auth.hash_password("correct horse battery")}
    identity = auth.sign_in_with_password("alice", "correct horse battery", users, auth.LoginThrottle())
    assert (identity.subject, identity.method) == ("alice", "password")
    assert auth.verify_identity(identity) == []


@pytest.mark.unit
@pytest.mark.parametrize("user, password", [("alice", "wrong password!!"), ("mallory", "correct horse battery")])
def test_a_failed_sign_in_says_the_same_thing_either_way(user, password):
    users = {"alice": auth.hash_password("correct horse battery")}
    with pytest.raises(auth.AuthError, match="username or password is not right"):
        auth.sign_in_with_password(user, password, users, auth.LoginThrottle())


@pytest.mark.unit
def test_repeated_failures_lock_the_user_out():
    users = {"alice": auth.hash_password("correct horse battery")}
    throttle = auth.LoginThrottle(max_failures=3, lockout_seconds=60)
    for _ in range(3):
        with pytest.raises(auth.AuthError, match="not right"):
            auth.sign_in_with_password("alice", "nope nope nope", users, throttle)
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.sign_in_with_password("alice", "correct horse battery", users, throttle)


@pytest.mark.unit
def test_password_sign_in_needs_a_users_file(monkeypatch, tmp_path):
    monkeypatch.delenv(auth.USERS_FILE_ENV, raising=False)
    with pytest.raises(auth.AuthError, match="not configured"):
        auth.sign_in_with_password("alice", "x", throttle=auth.LoginThrottle())
    bad = tmp_path / "users.json"
    bad.write_text(json.dumps(["alice"]))
    monkeypatch.setenv(auth.USERS_FILE_ENV, str(bad))
    with pytest.raises(auth.AuthError, match="must map"):
        auth.load_users()


@pytest.mark.unit
def test_a_hand_built_or_edited_identity_is_refused(monkeypatch):
    forged = auth.Identity(subject="alice", method="password",
                           authenticated_at=auth._now().isoformat(timespec="seconds"))
    assert auth.verify_identity(forged)
    assert auth.verify_identity(dataclasses.replace(TESTER, subject="mallory"))
    assert auth.verify_identity("alice")
    later = auth._now() + timedelta(seconds=auth.SESSION_TTL_SECONDS + 1)
    monkeypatch.setattr(auth, "_now", lambda: later)
    assert any("expired" in p for p in auth.verify_identity(TESTER))


@pytest.mark.unit
def test_an_os_username_or_checkbox_is_not_an_approver():
    """Sign-in is required by default: a name alone, or no identity, approves nothing."""
    with pytest.raises(OrderRejected, match="Sign in"):
        _approve(identity=None, approved_by="sbhat")
    with pytest.raises(OrderRejected, match="Sign in"):
        _approve(identity=None)


@pytest.mark.unit
def test_with_sign_in_switched_off_the_order_is_recorded_as_unauthenticated(tmp_path, fake_requests, monkeypatch):
    monkeypatch.setenv(auth.AUTH_REQUIRED_ENV, "false")
    order = _approve(identity=None, approved_by="desk")
    assert (order.approved_by, order.auth_method) == ("desk", "none")
    broker = _broker(tmp_path)
    broker.submit(order, LIMITS)
    assert ap.OrderLedger.for_results_dir(tmp_path).entries()[-1]["auth_method"] == "none"


@pytest.mark.unit
def test_an_unauthenticated_approval_is_refused_once_sign_in_is_required(tmp_path, fake_requests, monkeypatch):
    monkeypatch.setenv(auth.AUTH_REQUIRED_ENV, "false")
    order = _approve(identity=None, approved_by="desk")
    monkeypatch.setenv(auth.AUTH_REQUIRED_ENV, "true")       # required again before submission
    broker = _broker(tmp_path)
    with pytest.raises(OrderRejected, match="signed-in approver"):
        broker.submit(order, LIMITS)
    _nothing_sent(broker)


@pytest.mark.unit
def test_the_signed_in_approver_is_recorded(tmp_path, fake_requests):
    users = {"alice": auth.hash_password("correct horse battery")}
    identity = auth.sign_in_with_password("alice", "correct horse battery", users, auth.LoginThrottle())
    broker = _broker(tmp_path)
    broker.submit(_approve(identity=identity), LIMITS)
    approved, submitted = ap.OrderLedger.for_results_dir(tmp_path).entries()
    for entry in (approved, submitted):
        assert (entry["approved_by"], entry["auth_method"]) == ("alice", "password")


@pytest.mark.unit
def test_submit_works_with_real_alpaca_request_objects(tmp_path, monkeypatch):
    """alpaca-py stores qty as a float; the final check must accept the real request it builds."""
    pytest.importorskip("alpaca")
    from alpaca.trading.requests import LimitOrderRequest

    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: SimpleNamespace(symbol=symbol))
    broker = _broker(tmp_path)
    broker.submit(_approve(qty=3, limit_price=99.5), LIMITS)
    [sent] = broker.trading.submitted
    assert isinstance(sent, LimitOrderRequest)
    assert (sent.qty, sent.limit_price, sent.client_order_id) == (3.0, 99.5, f"ta-{RUN}-buy")


# ------------------------------------------------------------------ limit is the default; the cap invariant
@pytest.mark.unit
def test_limit_is_the_default_order_type():
    buy = op.propose_order("NVDA", "Buy", 0)
    # Naming no type and no limit price fails closed: never a market order by omission.
    assert op.check_order(buy, 1, 100.0, buying_power=1e6, held_long_qty=0, limits=LIMITS) == [
        "Enter a limit price above zero."]
    with pytest.raises(OrderRejected, match="limit price"):
        op.approve(buy, 1, 100.0, run_id=RUN, buying_power=1e6, held_long_qty=0, limits=LIMITS,
                   confirmed=True, identity=TESTER)
    order = op.approve(buy, 1, 100.0, run_id=RUN, buying_power=1e6, held_long_qty=0, limits=LIMITS,
                       confirmed=True, identity=TESTER, limit_price=100.0)
    assert order.order_type is OrderType.LIMIT
    import inspect

    from tradingagents.dashboard import trade
    for fn in (op.check_order, op.approve, trade.evaluate, trade.submit):
        assert inspect.signature(fn).parameters["order_type"].default is OrderType.LIMIT


@pytest.mark.unit
@pytest.mark.parametrize("qty, limit, cap, allowed", [
    (3, 333.33, 999.99, True),     # exactly at the cap
    (3, 333.33, 999.98, False),    # one cent over
    (3, 0.1, 0.3, True),           # 3 x 0.1 is 0.30000000000000004 in binary floats; exact is 0.3
    (7, 0.0143, 0.1001, True),
    (7, 0.0143, 0.1000, False),
    (1000, 1.0, 1000.0, True),
    (1001, 1.0, 1000.0, False),
])
def test_quantity_times_limit_is_compared_exactly(qty, limit, cap, allowed):
    buy = op.propose_order("NVDA", "Buy", 0)
    problems = op.check_order(buy, qty, 1.0, buying_power=1e9, held_long_qty=0,
                              limits=OrderLimits(cap), order_type=OrderType.LIMIT, limit_price=limit)
    assert (problems == []) is allowed, problems


@pytest.mark.unit
def test_no_accepted_limit_buy_can_exceed_the_cap():
    """4,000 seeded random orders: accepted exactly when qty x limit <= cap, in exact decimals."""
    import random
    from decimal import Decimal

    rng = random.Random(20261122)
    buy = op.propose_order("NVDA", "Buy", 0)
    for _ in range(4000):
        qty = rng.randint(1, 500)
        cents = rng.randint(1, 300_000)
        limit = cents / 100
        cap = rng.choice([1000.0, 999.99, 250.0, 5000.0, float(Decimal(qty * cents) / 100)])
        accepted = op.check_order(buy, qty, limit, buying_power=1e12, held_long_qty=0,
                                  limits=OrderLimits(cap), order_type=OrderType.LIMIT,
                                  limit_price=limit) == []
        exact_value = Decimal(qty) * Decimal(cents) / 100
        assert accepted == (exact_value <= Decimal(repr(cap))), (qty, limit, cap)


@pytest.mark.unit
def test_every_limit_order_the_broker_sends_is_within_the_cap(tmp_path, fake_requests, monkeypatch):
    import random
    from decimal import Decimal

    monkeypatch.setattr(op, "_signature", lambda order: "s")      # approvals of many orders per run ID
    rng = random.Random(7)
    broker = _broker(tmp_path / "ledger", price=50.0)
    sent = 0
    for n in range(200):
        qty, cents = rng.randint(1, 60), rng.randint(500, 4000)
        order = op.ApprovedOrder(
            run_id=f"{RUN}-{n}", ticker="NVDA", rating="Buy", side=Side.BUY, qty=qty, reference_price=50.0,
            notional=qty * cents / 100, client_order_id=f"ta-{n}-buy",
            approved_at=op._now().isoformat(timespec="seconds"), approved_mono=op._monotonic(), approved_by="tester",
            order_type=OrderType.LIMIT, limit_price=cents / 100, auth_method="password", approval="s")
        try:
            broker.submit(order, LIMITS)
            sent += 1
        except OrderRejected:
            continue
    assert 0 < sent < 200
    for request in broker.trading.submitted:
        assert Decimal(request.qty) * Decimal(repr(request.limit_price)) <= Decimal("1000")
