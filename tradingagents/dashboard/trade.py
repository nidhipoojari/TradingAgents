"""F1: the paper-trade panel's logic, from a finished analysis to an approved order.

The page shows what ``load_panel`` and ``evaluate`` return and calls ``submit``
only from its submit button. These checks are for display; the broker repeats
every one of them with fresh numbers before sending (see AlpacaPaperBroker.submit).
No Streamlit import.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from tradingagents.execution import identity as auth, order_policy as op
from tradingagents.execution.alpaca_paper import (
    Account,
    AlpacaPaperBroker,
    ApprovalNotRecorded,
    BrokerError,
    MarketClock,
    OrderReceipt,
    SubmissionFailed,
)
from tradingagents.execution.decision_log import DecisionLog, new_decision_id
from tradingagents.proposal_store import ProposalStore, build as build_proposal

logger = logging.getLogger(__name__)

REJECTED_MESSAGE = "This trade proposal was rejected; reconsider it and approve it again to place an order."


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
    proposal_id: str | None = None   # ta-<run_id>-<buy|sell|none> (trade_rules.proposal_id)
    proposal_record: dict | None = None   # the stored TRADE_PROPOSAL, when a ProposalStore was given

    @property
    def can_trade(self) -> bool:
        return self.proposal.allowed and not self.account.trading_blocked and self.already_submitted is None


@dataclass(frozen=True)
class Evaluation:
    problems: list[str]
    notional: float | None           # at the worst-case price
    can_submit: bool


def load_panel(result, broker: AlpacaPaperBroker, limits: op.OrderLimits,
               today: str | None = None, proposals: ProposalStore | None = None) -> TradePanel:
    """Read the paper account and market clock (read-only) and work out what the analysis allows.

    With ``proposals``, the proposal is recorded once (rerunning the page changes nothing). A record that
    cannot be written is logged here and retried, fail-closed, before any approval (``submit``)."""
    today = today or date.today().isoformat()
    account = broker.account()
    held = broker.long_position(result.ticker) if op.is_paper_tradable(result.ticker, result.asset_type) else 0.0
    proposal = op.propose_order(result.ticker, result.rating, held, asset_type=result.asset_type)
    price = broker.latest_price(result.ticker) if proposal.allowed else None
    clock = broker.market_clock() if proposal.allowed else None
    cid = op.client_order_id(result.run_id, proposal.side) if proposal.allowed else None
    seen = broker.ledger.find(cid) if (cid and broker.ledger is not None) else None
    panel = TradePanel(
        run_id=result.run_id, trade_date=result.trade_date, proposal=proposal, account=account,
        held_long_qty=held, price=price, limits=limits, stale=result.trade_date != today,
        client_order_id=cid, already_submitted=seen, clock=clock,
        proposal_id=op.proposal_id(result.run_id, proposal.side),
    )
    if proposals is not None:
        try:
            stored, _ = record_proposal(panel, result, proposals)
            panel = _with_record(panel, stored)
        except (OSError, ValueError):
            logger.warning("could not record trade proposal %s", panel.proposal_id, exc_info=True)
    return panel


def _with_record(panel: TradePanel, stored: dict) -> TradePanel:
    from dataclasses import replace

    return replace(panel, proposal_record=stored)


def record_proposal(panel: TradePanel, result, proposals: ProposalStore) -> tuple[dict, bool]:
    """The run's TRADE_PROPOSAL, created once: (stored record, created now). Raises OSError or ValueError."""
    record = build_proposal(panel.proposal, run_id=panel.run_id, trade_date=panel.trade_date,
                            asset_type=getattr(result, "asset_type", None), source=getattr(result, "source", "dashboard"),
                            max_notional=panel.limits.max_notional, stale=panel.stale)
    return proposals.record(record)


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


def _fields(panel: TradePanel, result, qty, limit_price, identity) -> dict:
    """What the decision log records about a proposal and the person's entries (never a password)."""
    return {"run_id": panel.run_id, "proposal_id": panel.client_order_id or panel.proposal_id, "ticker": result.ticker,
            "side": panel.proposal.side.value if panel.proposal.side is not None else None,
            "qty": op.whole_shares(qty) or None, "limit_price": limit_price, "identity": identity}


def reject(panel: TradePanel, result, decisions: DecisionLog, *, identity: auth.Identity | None = None,
           qty=None, limit_price: float | None = None) -> tuple[dict, bool]:
    """Record the person's rejection of this run's proposal. Never contacts the broker.
    Returns (the standing rejection, whether this call recorded it): a repeat click records nothing."""
    return decisions.reject(**_fields(panel, result, qty, limit_price, identity))


def reconsider(panel: TradePanel, result, decisions: DecisionLog, *,
               identity: auth.Identity | None = None) -> dict | None:
    """Reopen a rejected proposal (recorded). The approval must then be given again."""
    return decisions.reconsider(**_fields(panel, result, None, None, identity))


def record_failure(decisions: DecisionLog | None, category: str, panel: TradePanel, result, *, qty=None,
                   limit_price: float | None = None, identity: auth.Identity | None = None, problems=None,
                   approval_decision_id: str | None = None) -> None:
    if decisions is not None:
        decisions.failure(category, problems=problems, approval_decision_id=approval_decision_id,
                          **_fields(panel, result, qty, limit_price, identity))


def submit(panel: TradePanel, result, broker: AlpacaPaperBroker, qty, *, confirmed: bool,
           identity: auth.Identity | None = None, approved_by: str | None = None,
           order_type: op.OrderType | str = op.OrderType.LIMIT, limit_price: float | None = None,
           stale_acknowledged: bool = False, today: str | None = None,
           decisions: DecisionLog | None = None, proposals: ProposalStore | None = None) -> OrderReceipt:
    """Approve the person's order and send it once. Raises OrderRejected or BrokerError.

    With ``decisions`` (the dashboard), a rejected proposal is refused before any check, every failed attempt
    is recorded with its category, and the approval of an order that passed every check is recorded (event
    ``approved``) by the broker under its ledger lock just before sending; its decision ID is signed into the
    order and named by the order's ledger lines. A repeat submission of a placed order records no approval.
    With ``proposals``, the proposal must be on record first: if it cannot be written, nothing is approved."""
    decision_id = ""

    def failed(category, problems):
        record_failure(decisions, category, panel, result, qty=qty, limit_price=limit_price, identity=identity,
                       problems=problems, approval_decision_id=decision_id or None)

    if decisions is not None and decisions.rejection(panel.client_order_id) is not None:
        failed("proposal_rejected", [REJECTED_MESSAGE])
        raise op.OrderRejected([REJECTED_MESSAGE])
    if proposals is not None:
        try:
            record_proposal(panel, result, proposals)
        except (OSError, ValueError) as exc:
            problem = f"The trade proposal could not be recorded ({type(exc).__name__}); nothing was approved or sent."
            failed("record_failed", [problem])
            raise op.OrderRejected([problem]) from None
    market_open = panel.clock.is_open if panel.clock is not None else None
    try:
        order = op.approve(
            panel.proposal, qty, panel.price, run_id=panel.run_id,
            buying_power=panel.account.buying_power, held_long_qty=panel.held_long_qty,
            limits=panel.limits, confirmed=confirmed, identity=identity, approved_by=approved_by,
            order_type=order_type, limit_price=limit_price, market_open=market_open,
            trade_date=panel.trade_date, today=today or date.today().isoformat(),
            stale_acknowledged=stale_acknowledged,
            decision_id=new_decision_id() if decisions is not None else "",
        )
    except op.OrderRejected as exc:
        # The order's own parameters, checked alone: any problem there is an invalid order; otherwise what
        # was missing is the approval (tick, stale-date acknowledgement, sign-in or a recent password).
        invalid = op.check_order(panel.proposal, qty, panel.price, buying_power=panel.account.buying_power,
                                 held_long_qty=panel.held_long_qty, limits=panel.limits, order_type=order_type,
                                 limit_price=limit_price, market_open=market_open)
        failed("invalid_order" if invalid else "missing_approval", exc.problems)
        raise
    # The broker re-checks everything with fresh numbers, the market clock and the configured cap.
    # Limit orders only: a market order is refused at every layer.
    on_approved = (lambda approved: decisions.approve(approved, identity=identity)) if decisions is not None else None
    try:
        receipt = broker.submit(order, panel.limits, source=result.source, on_approved=on_approved)
    except ApprovalNotRecorded as exc:
        failed("record_failed", [str(exc)])
        raise
    except SubmissionFailed as exc:
        decision_id = order.decision_id     # the approval was recorded before Alpaca refused the order
        failed("alpaca_api_error", [str(exc)])
        raise
    except op.OrderRejected as exc:
        failed("broker_validation", exc.problems)
        raise
    except BrokerError as exc:
        failed("broker_validation", [str(exc)])
        raise
    return receipt
