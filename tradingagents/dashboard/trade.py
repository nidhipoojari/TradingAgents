"""F1: the paper-trade panel's logic, from a finished analysis to an approved order.

The page shows what ``load_panel`` and ``evaluate`` return and calls ``submit``
only from its submit button. These checks are for display; the broker repeats
every one of them with fresh numbers before sending (see AlpacaPaperBroker.submit).
No Streamlit import.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from tradingagents.execution import identity as auth, order_policy as op
from tradingagents.execution.alpaca_paper import (
    Account,
    AlpacaPaperBroker,
    MarketClock,
    OrderReceipt,
)


@dataclass(frozen=True)
class TradePanel:
    run_id: str
    trade_date: str
    proposal: op.OrderProposal
    account: Account
    held_long_qty: float
    price: float | None
    limits: op.OrderLimits
    stale: bool
    client_order_id: str | None
    already_submitted: dict | None   # the ledger entry when this run's order was already sent
    clock: MarketClock | None = None

    @property
    def can_trade(self) -> bool:
        return self.proposal.allowed and not self.account.trading_blocked and self.already_submitted is None


@dataclass(frozen=True)
class Evaluation:
    problems: list[str]
    notional: float | None           # at the worst-case price
    can_submit: bool


def load_panel(result, broker: AlpacaPaperBroker, limits: op.OrderLimits,
               today: str | None = None) -> TradePanel:
    """Read the paper account and market clock (read-only) and work out what the analysis allows."""
    today = today or date.today().isoformat()
    account = broker.account()
    held = broker.long_position(result.ticker) if op.is_paper_tradable(result.ticker, result.asset_type) else 0.0
    proposal = op.propose_order(result.ticker, result.rating, held, asset_type=result.asset_type)
    price = broker.latest_price(result.ticker) if proposal.allowed else None
    clock = broker.market_clock() if proposal.allowed else None
    cid = op.client_order_id(result.run_id, proposal.side) if proposal.allowed else None
    seen = broker.ledger.find(cid) if (cid and broker.ledger is not None) else None
    return TradePanel(
        run_id=result.run_id, trade_date=result.trade_date, proposal=proposal, account=account,
        held_long_qty=held, price=price, limits=limits, stale=result.trade_date != today,
        client_order_id=cid, already_submitted=seen, clock=clock,
    )


def evaluate(panel: TradePanel, qty, confirmed: bool, stale_acknowledged: bool = False, *,
             order_type: op.OrderType | str = op.OrderType.LIMIT, limit_price: float | None = None,
             identity: auth.Identity | None = None) -> Evaluation:
    """What stands between the person's entries and a submittable order."""
    if not panel.proposal.allowed:
        return Evaluation([panel.proposal.reason], None, False)
    if panel.already_submitted is not None:
        return Evaluation(["This analysis run already placed its order; run a new analysis to trade again."],
                          None, False)
    if panel.account.trading_blocked:
        return Evaluation(["The paper account is blocked from trading."], None, False)
    market_open = panel.clock.is_open if panel.clock is not None else None
    problems = op.check_order(panel.proposal, qty, panel.price, buying_power=panel.account.buying_power,
                              held_long_qty=panel.held_long_qty, limits=panel.limits, order_type=order_type,
                              limit_price=limit_price, market_open=market_open)
    if panel.stale and not stale_acknowledged:
        problems.append(f"The analysis is for {panel.trade_date}, not today; tick the box to act on it anyway.")
    if identity is not None or auth.auth_required():
        problems += auth.verify_identity(identity)
    if confirmed is not True:
        problems.append("Tick the approval box for this exact order.")
    whole = op.whole_shares(qty)
    try:
        worst = op.worst_case_price(panel.proposal.side, op.OrderType(order_type), panel.price, limit_price)
    except ValueError:
        worst = None
    notional = round(whole * worst, 2) if (whole and worst) else None
    return Evaluation(problems, notional, not problems)


def submit(panel: TradePanel, result, broker: AlpacaPaperBroker, qty, *, confirmed: bool,
           identity: auth.Identity | None = None, approved_by: str | None = None,
           order_type: op.OrderType | str = op.OrderType.LIMIT, limit_price: float | None = None,
           stale_acknowledged: bool = False, today: str | None = None) -> OrderReceipt:
    """Approve the person's order and send it once. Raises OrderRejected or BrokerError."""
    order = op.approve(
        panel.proposal, qty, panel.price, run_id=panel.run_id,
        buying_power=panel.account.buying_power, held_long_qty=panel.held_long_qty,
        limits=panel.limits, confirmed=confirmed, identity=identity, approved_by=approved_by,
        order_type=order_type, limit_price=limit_price,
        market_open=panel.clock.is_open if panel.clock is not None else None,
        trade_date=panel.trade_date, today=today or date.today().isoformat(),
        stale_acknowledged=stale_acknowledged,
    )
    # The broker re-checks everything with fresh numbers, the market clock and the configured cap.
    # Limit orders only: a market order is refused at every layer.
    return broker.submit(order, panel.limits, source=result.source)
