"""F1 paper-trading safeguards: what a rating may become, and what a person's order must pass."""

import pytest

from tradingagents.execution import order_policy as op
from tradingagents.execution.identity import _issue
from tradingagents.execution.order_policy import OrderLimits, OrderRejected, Side

LIMITS = OrderLimits(max_notional=1000.0)

TESTER = _issue("tester", "password")   # a signed-in approver


@pytest.mark.unit
@pytest.mark.parametrize("rating, held, side", [
    ("Buy", 0, Side.BUY),
    ("Overweight", 5, Side.BUY),
    ("Sell", 5, Side.SELL),
    ("Underweight", 2.5, Side.SELL),
    ("Sell", 0, None),            # nothing held: selling would open a short
    ("Underweight", -3, None),    # a short position is never added to
    ("Hold", 10, None),
    ("REVIEW", 10, None),
    ("", 10, None),
])
def test_a_rating_allows_only_its_own_side(rating, held, side):
    proposal = op.propose_order("NVDA", rating, held)
    assert proposal.side is side
    assert proposal.allowed is (side is not None)
    assert proposal.reason


@pytest.mark.unit
def test_a_sell_without_a_long_position_says_why():
    proposal = op.propose_order("NVDA", "Sell", 0)
    assert not proposal.allowed
    assert "no long position" in proposal.reason and "short" in proposal.reason.lower()


@pytest.mark.unit
def test_hold_never_creates_an_order():
    proposal = op.propose_order("NVDA", "Hold", 100)
    with pytest.raises(OrderRejected, match="Hold"):
        op.approve(proposal, 1, 10.0, run_id="r1", buying_power=1e6, held_long_qty=100,
                   limits=LIMITS, confirmed=True, identity=TESTER, order_type="limit", limit_price=10.0)


@pytest.mark.unit
@pytest.mark.parametrize("ticker, asset", [("BTC-USD", "crypto"), ("0700.HK", "stock"),
                                           ("^GSPC", "stock"), ("GC=F", "stock")])
def test_only_us_stocks_are_paper_traded(ticker, asset):
    assert not op.propose_order(ticker, "Buy", 0, asset_type=asset).allowed


@pytest.mark.unit
def test_share_classes_are_us_stocks():
    assert op.propose_order("BRK.B", "Buy", 0).allowed


@pytest.mark.unit
@pytest.mark.parametrize("qty", [None, 0, -1, 1.5, float("nan"), float("inf"), "3", True])
def test_the_quantity_must_be_a_whole_number_the_person_entered(qty):
    proposal = op.propose_order("NVDA", "Buy", 0)
    problems = op.check_order(proposal, qty, 10.0, buying_power=1e6, held_long_qty=0, limits=LIMITS, order_type="limit", limit_price=10.0)
    assert any("whole number" in p for p in problems)


@pytest.mark.unit
def test_nothing_reads_a_quantity_from_the_decision_text():
    """The proposal has no quantity at all; only the person's number sizes an order."""
    proposal = op.propose_order("NVDA", "Buy", 0)
    assert not hasattr(proposal, "qty")
    import inspect
    assert "text" not in inspect.signature(op.propose_order).parameters
    assert "decision" not in inspect.signature(op.approve).parameters


@pytest.mark.unit
def test_the_notional_cap_applies_to_buys_and_sells():
    buy = op.propose_order("NVDA", "Buy", 0)
    assert any("limit" in p for p in op.check_order(buy, 11, 100.0, buying_power=1e6,
                                                    held_long_qty=0, limits=LIMITS, order_type="limit", limit_price=100.0))
    sell = op.propose_order("NVDA", "Sell", 50)
    assert any("limit" in p for p in op.check_order(sell, 11, 100.0, buying_power=0,
                                                    held_long_qty=50, limits=LIMITS, order_type="limit", limit_price=100.0))
    assert op.check_order(buy, 10, 100.0, buying_power=1e6, held_long_qty=0, limits=LIMITS, order_type="limit", limit_price=100.0) == []   # $1,000: at the cap


@pytest.mark.unit
def test_a_buy_stays_within_buying_power():
    buy = op.propose_order("NVDA", "Buy", 0)
    problems = op.check_order(buy, 5, 100.0, buying_power=499.0, held_long_qty=0, limits=LIMITS, order_type="limit", limit_price=100.0)
    assert any("buying power" in p for p in problems)


@pytest.mark.unit
def test_a_sell_can_close_but_never_go_short():
    sell = op.propose_order("NVDA", "Sell", 3)
    assert op.check_order(sell, 3, 10.0, buying_power=0, held_long_qty=3, limits=LIMITS, order_type="limit", limit_price=10.0) == []
    problems = op.check_order(sell, 4, 10.0, buying_power=1e6, held_long_qty=3, limits=LIMITS, order_type="limit", limit_price=10.0)
    assert any("Short selling is not supported" in p for p in problems)


@pytest.mark.unit
def test_a_missing_price_blocks_the_order():
    buy = op.propose_order("NVDA", "Buy", 0)
    assert any("price" in p for p in op.check_order(buy, 1, None, buying_power=1e6,
                                                    held_long_qty=0, limits=LIMITS, order_type="limit", limit_price=None))


@pytest.mark.unit
@pytest.mark.parametrize("confirmed", [False, None, "yes", 1])
def test_every_order_needs_explicit_approval(confirmed):
    buy = op.propose_order("NVDA", "Buy", 0)
    with pytest.raises(OrderRejected, match="approval"):
        op.approve(buy, 1, 10.0, run_id="r1", buying_power=1e6, held_long_qty=0,
                   limits=LIMITS, confirmed=confirmed, identity=TESTER, order_type="limit", limit_price=10.0)


@pytest.mark.unit
def test_an_old_decision_needs_its_own_acknowledgement():
    buy = op.propose_order("NVDA", "Buy", 0)
    kwargs = {"run_id": "r1", "buying_power": 1e6, "held_long_qty": 0, "limits": LIMITS, "confirmed": True, "identity": TESTER, "order_type": "limit", "limit_price": 10.0,
                  "trade_date": "2026-10-01", "today": "2026-11-21"}
    with pytest.raises(OrderRejected, match="not today"):
        op.approve(buy, 1, 10.0, **kwargs)
    assert op.approve(buy, 1, 10.0, stale_acknowledged=True, **kwargs).qty == 1


@pytest.mark.unit
def test_an_approved_order_is_tied_to_its_run():
    buy = op.propose_order("NVDA", "Overweight", 0)
    order = op.approve(buy, 2, 150.0, run_id="20261121-101500-ab12cd34", buying_power=1e6,
                       held_long_qty=0, limits=LIMITS, confirmed=True, identity=TESTER, order_type="limit", limit_price=150.0)
    assert (order.side, order.qty, order.notional) == (Side.BUY, 2, 300.0)   # qty x limit
    assert order.client_order_id == "ta-20261121-101500-ab12cd34-buy"
    # The same run and side always make the same ID, so the broker can refuse a repeat.
    again = op.approve(buy, 2, 150.0, run_id="20261121-101500-ab12cd34", buying_power=1e6,
                       held_long_qty=0, limits=LIMITS, confirmed=True, identity=TESTER, order_type="limit", limit_price=150.0)
    assert again.client_order_id == order.client_order_id


@pytest.mark.unit
def test_all_problems_are_reported_together():
    buy = op.propose_order("NVDA", "Buy", 0)
    with pytest.raises(OrderRejected) as caught:
        op.approve(buy, 20, 100.0, run_id="r1", buying_power=50.0, held_long_qty=0,
                   limits=LIMITS, confirmed=False, identity=TESTER, order_type="limit", limit_price=100.0)
    assert len(caught.value.problems) == 3   # over the cap, over buying power, not approved


@pytest.mark.unit
def test_the_limit_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(op.MAX_NOTIONAL_ENV, "250")
    assert OrderLimits.from_env().max_notional == 250.0
    monkeypatch.setenv(op.MAX_NOTIONAL_ENV, "")
    assert OrderLimits.from_env().max_notional == op.DEFAULT_MAX_NOTIONAL
    for bad in ("lots", "0", "-5", "nan"):
        monkeypatch.setenv(op.MAX_NOTIONAL_ENV, bad)
        with pytest.raises(ValueError):
            OrderLimits.from_env()
