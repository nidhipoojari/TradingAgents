"""F1 Alpaca adapter: paper endpoint only, one submission per client order ID, fresh checks.

A fake trading client stands in for Alpaca; nothing here reaches the network.
"""

import json
import threading
from types import SimpleNamespace

import pytest

from tradingagents.execution import alpaca_paper as ap, order_policy as op
from tradingagents.execution.identity import _issue
from tradingagents.execution.order_policy import OrderLimits, OrderRejected

LIMITS = OrderLimits(max_notional=1000.0)
RUN = "20261121-101500-ab12cd34"

TESTER = _issue("tester", "password")   # a signed-in approver


class NotFound(Exception):
    status_code = 404


class FakeTrading:
    _base_url = ap.PAPER_TRADING_URL

    def __init__(self, buying_power=10_000.0, held=0.0, blocked=False, market_open=True):
        self.buying_power, self.held, self.blocked = buying_power, held, blocked
        self.market_open = market_open
        self.orders = {}
        self.submitted = []

    def get_account(self):
        return SimpleNamespace(account_number="PA123", status=SimpleNamespace(value="ACTIVE"),
                               currency="USD", buying_power=str(self.buying_power), cash="5000",
                               equity="10000", trading_blocked=self.blocked, account_blocked=False)

    def get_clock(self):
        return SimpleNamespace(is_open=self.market_open, next_open="2026-11-23T14:30:00+00:00",
                               next_close="2026-11-23T21:00:00+00:00")

    def get_open_position(self, symbol):
        if self.held <= 0:
            raise NotFound("position does not exist")
        return SimpleNamespace(qty=str(self.held), side=SimpleNamespace(value="long"))

    def get_order_by_client_id(self, client_id):
        if client_id not in self.orders:
            raise NotFound("order not found")
        return self.orders[client_id]

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        order = SimpleNamespace(id=f"order-{len(self.submitted)}", client_order_id=order_data.client_order_id,
                                status=SimpleNamespace(value="accepted"), symbol=order_data.symbol,
                                side=SimpleNamespace(value=order_data.side), qty=str(order_data.qty),
                                submitted_at=None)
        self.orders[order_data.client_order_id] = order
        return order


class FakeData:
    def __init__(self, price=100.0):
        self.price = price

    def get_stock_latest_trade(self, request):
        return {request.symbol: SimpleNamespace(price=self.price)}


@pytest.fixture(autouse=True)
def _no_alpaca_sdk(monkeypatch):
    """Requests are plain objects here; the SDK is an optional install."""
    monkeypatch.setattr(ap, "_order_request", lambda symbol, qty, side, cid, order_type, limit_price: SimpleNamespace(
        symbol=symbol, qty=qty, side=side.value, type=order_type.value, limit_price=limit_price,
        client_order_id=cid))
    monkeypatch.setattr(ap, "_latest_trade_request", lambda symbol: SimpleNamespace(symbol=symbol))


def _broker(tmp_path, **trading):
    return ap.AlpacaPaperBroker(FakeTrading(**trading), FakeData(),
                                ledger=ap.OrderLedger.for_results_dir(tmp_path))


def _approved(rating="Buy", qty=2, price=100.0, held=0.0, run_id=RUN):
    proposal = op.propose_order("NVDA", rating, held)
    return op.approve(proposal, qty, price, run_id=run_id, buying_power=10_000.0,
                      held_long_qty=held, limits=LIMITS, confirmed=True, identity=TESTER, order_type="limit", limit_price=price)


@pytest.mark.unit
def test_only_the_paper_endpoint_is_accepted():
    live = FakeTrading()
    live._base_url = "https://api.alpaca.markets"
    with pytest.raises(ap.BrokerNotConfigured, match="paper"):
        ap.AlpacaPaperBroker(live)
    unknown = FakeTrading()
    unknown._base_url = None
    with pytest.raises(ap.BrokerNotConfigured):
        ap.AlpacaPaperBroker(unknown)


@pytest.mark.unit
def test_an_enum_base_url_is_read_by_value():
    client = FakeTrading()
    client._base_url = SimpleNamespace(value=ap.PAPER_TRADING_URL)
    ap.AlpacaPaperBroker(client)


@pytest.mark.unit
@pytest.mark.parametrize("env, message", [
    ({}, "ALPACA_API_KEY"),
    ({"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ALPACA_PAPER": "false"}, "live trading"),
    ({"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "APCA_API_BASE_URL": "https://api.alpaca.markets"},
     "live trading"),
])
def test_configuration_must_be_paper_only(monkeypatch, env, message):
    for name in (*ap.KEY_ENVS, *ap.SECRET_ENVS, *ap.PAPER_FLAG_ENVS, *ap.BASE_URL_ENVS):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ap.BrokerNotConfigured, match=message):
        ap.AlpacaPaperBroker.from_env()


@pytest.mark.unit
def test_account_position_and_price_are_read(tmp_path):
    broker = _broker(tmp_path, held=4)
    account = broker.account()
    assert (account.buying_power, account.status, account.trading_blocked) == (10_000.0, "ACTIVE", False)
    assert broker.long_position("NVDA") == 4.0
    assert broker.latest_price("NVDA") == 100.0
    assert _broker(tmp_path).long_position("NVDA") == 0.0   # no position: 404 means none


@pytest.mark.unit
def test_a_short_position_counts_as_nothing_held(tmp_path):
    broker = _broker(tmp_path)
    broker.trading.get_open_position = lambda s: SimpleNamespace(qty="-5", side=SimpleNamespace(value="short"))
    assert broker.long_position("NVDA") == 0.0


@pytest.mark.unit
def test_only_an_approved_order_can_be_submitted(tmp_path):
    with pytest.raises(TypeError):
        _broker(tmp_path).submit({"ticker": "NVDA", "qty": 1, "side": "buy"}, LIMITS)


@pytest.mark.unit
def test_an_approved_order_is_sent_once_and_recorded(tmp_path):
    broker = _broker(tmp_path)
    order = _approved()

    receipt = broker.submit(order, LIMITS)

    assert not receipt.duplicate and receipt.order_id == "order-1"
    assert len(broker.trading.submitted) == 1
    sent = broker.trading.submitted[0]
    assert (sent.symbol, sent.qty, sent.side, sent.client_order_id) == ("NVDA", 2, "buy", order.client_order_id)
    approved, entry = [json.loads(line) for line in (tmp_path / ap.LEDGER_NAME).read_text().splitlines()]
    # The approval is on record before the order is sent.
    assert (approved["event"], entry["event"]) == ("approved", "submitted")
    assert "order_id" not in approved and entry["order_id"] == "order-1"
    assert entry["run_id"] == RUN and entry["approved_by"] == "tester"
    assert entry["endpoint"] == ap.PAPER_TRADING_URL and entry["source"] == "dashboard"


@pytest.mark.unit
def test_a_repeat_submission_is_not_sent_again(tmp_path):
    broker = _broker(tmp_path)
    order = _approved()
    broker.submit(order, LIMITS)

    again = broker.submit(order, LIMITS)

    assert again.duplicate and again.order_id == "order-1"
    assert len(broker.trading.submitted) == 1


@pytest.mark.unit
def test_an_order_alpaca_already_has_is_not_sent_again(tmp_path):
    """The local ledger lost (another machine, a deleted file): Alpaca's own ID check still holds."""
    first = _broker(tmp_path / "a")
    order = _approved()
    first.submit(order, LIMITS)
    second = ap.AlpacaPaperBroker(first.trading, FakeData(), ledger=ap.OrderLedger.for_results_dir(tmp_path / "b"))

    receipt = second.submit(order, LIMITS)

    assert receipt.duplicate and len(first.trading.submitted) == 1


@pytest.mark.unit
def test_simultaneous_submissions_send_one_order(tmp_path):
    broker = _broker(tmp_path)
    order = _approved()
    receipts = []
    threads = [threading.Thread(target=lambda: receipts.append(broker.submit(order, LIMITS)))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(broker.trading.submitted) == 1
    assert sum(not r.duplicate for r in receipts) == 1


@pytest.mark.unit
def test_the_order_is_checked_again_against_fresh_numbers(tmp_path):
    broker = _broker(tmp_path, buying_power=150.0)   # spent since the approval
    with pytest.raises(OrderRejected, match="buying power"):
        broker.submit(_approved(qty=2), LIMITS)
    assert broker.trading.submitted == []
    assert not (tmp_path / ap.LEDGER_NAME).exists()


@pytest.mark.unit
def test_a_sell_is_refused_when_the_position_is_gone(tmp_path):
    order = _approved(rating="Sell", qty=2, held=5)
    broker = _broker(tmp_path, held=0)   # closed elsewhere since the approval
    with pytest.raises(OrderRejected, match="short"):
        broker.submit(order, LIMITS)
    assert broker.trading.submitted == []


@pytest.mark.unit
def test_a_sell_limit_is_refused_when_the_quote_rises_past_the_cap(tmp_path):
    """A sell fills at or above its limit, so it is valued at the fresh quote when that is higher."""
    broker = _broker(tmp_path, held=50)
    order = _approved(rating="Sell", qty=9, held=50)          # 9 x $100 limit = $900
    broker.data = FakeData(price=120.0)                        # 9 x $120 = $1,080 since the approval
    with pytest.raises(OrderRejected, match="limit"):
        broker.submit(order, LIMITS)
    assert broker.trading.submitted == []


@pytest.mark.unit
def test_a_blocked_account_places_nothing(tmp_path):
    broker = _broker(tmp_path, blocked=True)
    with pytest.raises(ap.BrokerError, match="blocked"):
        broker.submit(_approved(), LIMITS)
    assert broker.trading.submitted == []


@pytest.mark.unit
def test_a_broker_failure_is_reported_and_not_recorded(tmp_path):
    broker = _broker(tmp_path)

    def refuse(order_data):
        raise RuntimeError("market closed")

    broker.trading.submit_order = refuse
    order = _approved()
    with pytest.raises(ap.BrokerError, match="market closed"):
        broker.submit(order, LIMITS)
    ledger = ap.OrderLedger.for_results_dir(tmp_path)
    assert [e["event"] for e in ledger.entries()] == ["approved", "failed"]
    assert ledger.find(order.client_order_id) is None   # not counted as placed; a retry may send it


# ------------------------------------------------------------------ the backend gate, UI bypassed
# These call AlpacaPaperBroker.submit directly, as a script that skipped the dashboard would.

def _forged(qty=2, price=100.0, side=op.Side.BUY, rating="Buy", **extra):
    """An ApprovedOrder built by hand instead of through approve()."""
    return op.ApprovedOrder(
        run_id=RUN, ticker="NVDA", rating=rating, side=side, qty=qty, reference_price=price,
        notional=qty * price if isinstance(qty, int | float) else 0.0,
        client_order_id=op.client_order_id(RUN, side),
        approved_at=op._now().isoformat(timespec="seconds"), approved_by="script",
        order_type=op.OrderType.LIMIT, limit_price=price, **extra)


def _nothing_sent(broker, tmp_path):
    assert broker.trading.submitted == []
    assert [e for e in ap.OrderLedger.for_results_dir(tmp_path).entries() if e.get("event") != "failed"] == []


@pytest.mark.unit
def test_a_caller_cannot_raise_the_configured_cap(tmp_path, monkeypatch):
    monkeypatch.delenv(op.MAX_NOTIONAL_ENV, raising=False)           # the default $1,000 cap
    broker = _broker(tmp_path)
    huge = OrderLimits(max_notional=1e9)
    order = op.approve(op.propose_order("NVDA", "Buy", 0), 50, 100.0, run_id=RUN, buying_power=1e9,
                       held_long_qty=0, limits=huge, confirmed=True, identity=TESTER, order_type="limit", limit_price=100.0)
    with pytest.raises(OrderRejected, match=r"\$5,000.00 .*is over the \$1,000.00 limit"):
        broker.submit(order, huge)
    with pytest.raises(OrderRejected, match="limit"):
        broker.submit(order)                                         # no limits given at all
    _nothing_sent(broker, tmp_path)


@pytest.mark.unit
def test_the_cap_is_read_at_submission(tmp_path, monkeypatch):
    broker = _broker(tmp_path)
    order = _approved(qty=2)                                         # $200, approved under $1,000
    monkeypatch.setenv(op.MAX_NOTIONAL_ENV, "150")                   # lowered since the approval
    with pytest.raises(OrderRejected, match=r"over the \$150.00 limit"):
        broker.submit(order, LIMITS)
    _nothing_sent(broker, tmp_path)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["lots", "0", "-1", "nan"])
def test_a_broken_cap_setting_sends_nothing(tmp_path, monkeypatch, bad):
    broker = _broker(tmp_path)
    order = _approved()
    monkeypatch.setenv(op.MAX_NOTIONAL_ENV, bad)
    with pytest.raises(OrderRejected, match=op.MAX_NOTIONAL_ENV):
        broker.submit(order, LIMITS)
    _nothing_sent(broker, tmp_path)


@pytest.mark.unit
def test_an_order_built_without_approve_is_refused(tmp_path):
    broker = _broker(tmp_path)
    with pytest.raises(OrderRejected, match="valid approval"):
        broker.submit(_forged(), LIMITS)
    _nothing_sent(broker, tmp_path)


@pytest.mark.unit
@pytest.mark.parametrize("change", [{"qty": 9}, {"side": op.Side.SELL}, {"ticker": "AAPL"},
                                    {"notional": 1.0}, {"approved_by": "someone else"}])
def test_an_approved_order_edited_afterwards_is_refused(tmp_path, change):
    import dataclasses

    broker = _broker(tmp_path, held=50)
    with pytest.raises(OrderRejected, match="valid approval"):
        broker.submit(dataclasses.replace(_approved(), **change), LIMITS)
    _nothing_sent(broker, tmp_path)


@pytest.mark.unit
def test_an_expired_approval_is_refused(tmp_path, monkeypatch):
    from datetime import timedelta

    broker = _broker(tmp_path)
    order = _approved()
    later = op._now() + timedelta(seconds=op.APPROVAL_TTL_SECONDS + 1)
    monkeypatch.setattr(op, "_now", lambda: later)
    with pytest.raises(OrderRejected, match="past the"):
        broker.submit(order, LIMITS)
    _nothing_sent(broker, tmp_path)


@pytest.mark.unit
@pytest.mark.parametrize("who", ["", "   ", None])
def test_an_approval_must_name_the_person(who, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_REQUIRE_ORDER_AUTH", "false")
    with pytest.raises(OrderRejected, match="Name the person"):
        op.approve(op.propose_order("NVDA", "Buy", 0), 1, 10.0, run_id=RUN, buying_power=1e6,
                   held_long_qty=0, limits=LIMITS, confirmed=True, approved_by=who, order_type="limit", limit_price=10.0)


@pytest.mark.unit
@pytest.mark.parametrize("qty", [0, -3, 1.5, float("nan"), "2", True])
def test_the_backend_checks_the_quantity_itself(tmp_path, monkeypatch, qty):
    """Defense in depth: even with the approval check out of the way, a bad quantity is not sent."""
    monkeypatch.setattr(ap, "verify_approval", lambda order: [])
    broker = _broker(tmp_path)
    with pytest.raises(OrderRejected, match="whole number"):
        broker.submit(_forged(qty=qty), LIMITS)
    _nothing_sent(broker, tmp_path)


@pytest.mark.unit
def test_the_backend_checks_buying_power_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "verify_approval", lambda order: [])
    broker = _broker(tmp_path, buying_power=100.0)
    with pytest.raises(OrderRejected, match="buying power"):
        broker.submit(_forged(qty=5), LIMITS)                        # $500 against $100
    _nothing_sent(broker, tmp_path)


@pytest.mark.unit
def test_the_backend_checks_holdings_for_a_sell_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "verify_approval", lambda order: [])
    broker = _broker(tmp_path, held=1)
    with pytest.raises(OrderRejected, match="only 1 are held"):
        broker.submit(_forged(qty=3, side=op.Side.SELL, rating="Sell"), LIMITS)
    _nothing_sent(broker, tmp_path)


@pytest.mark.unit
def test_a_sell_with_no_position_at_all_is_refused(tmp_path):
    broker = _broker(tmp_path, held=5)
    order = _approved(rating="Sell", qty=2, held=5)
    broker.trading.held = 0
    with pytest.raises(OrderRejected, match="short"):
        broker.submit(order, LIMITS)
    _nothing_sent(broker, tmp_path)


@pytest.mark.unit
@pytest.mark.parametrize("data", [None, FakeData(price=None), FakeData(price=0.0)])
def test_no_fresh_price_means_no_order(tmp_path, data):
    """The approval-time price is never used in place of a fresh one."""
    broker = ap.AlpacaPaperBroker(FakeTrading(), data, ledger=ap.OrderLedger.for_results_dir(tmp_path))
    with pytest.raises(OrderRejected, match="No current price"):
        broker.submit(_approved(), LIMITS)
    _nothing_sent(broker, tmp_path)


@pytest.mark.unit
def test_a_buy_limit_stays_capped_at_its_limit_when_the_quote_jumps(tmp_path):
    """A buy limit never fills above its limit, so a jump in the quote cannot push it past the cap."""
    broker = _broker(tmp_path)
    broker.data = FakeData(price=501.0)
    broker.submit(_approved(qty=2, price=100.0), LIMITS)       # 2 x $100 limit = $200
    [sent] = broker.trading.submitted
    assert (sent.type, sent.limit_price) == ("limit", 100.0)


@pytest.mark.unit
def test_a_client_repointed_at_live_trading_sends_nothing(tmp_path):
    broker = _broker(tmp_path)
    order = _approved()
    broker.trading._base_url = "https://api.alpaca.markets"
    with pytest.raises(ap.BrokerNotConfigured, match="paper"):
        broker.submit(order, LIMITS)
    _nothing_sent(broker, tmp_path)


@pytest.mark.unit
def test_no_ledger_means_no_order(tmp_path):
    broker = ap.AlpacaPaperBroker(FakeTrading(), FakeData())
    with pytest.raises(ap.BrokerNotConfigured, match="ledger"):
        broker.submit(_approved(), LIMITS)
    assert broker.trading.submitted == []


@pytest.mark.unit
def test_the_ledger_records_what_was_checked(tmp_path):
    broker = _broker(tmp_path, buying_power=5000.0)
    broker.data = FakeData(price=101.0)
    order = _approved(qty=2)
    broker.submit(order, LIMITS)
    approved = ap.OrderLedger.for_results_dir(tmp_path).entries()[0]
    assert approved["event"] == "approved" and approved["approved_by"] == "tester"
    assert (approved["checked_price"], approved["checked_buying_power"], approved["checked_notional"],
            approved["max_notional"]) == (101.0, 5000.0, 200.0, 1000.0)
