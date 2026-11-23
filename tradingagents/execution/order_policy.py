"""F1: what a rating may turn into, and the checks a person's paper order must pass.

Pure functions, no broker and no network, so every safeguard is unit-tested.

The rules (Phase 3 decisions):
- Buy / Overweight may propose a buy.
- Underweight / Sell may only reduce or close an existing long position. With no
  long position there is nothing to sell: no short selling, no margin, never a
  negative position.
- Hold, REVIEW and anything else create no order.
- The quantity is always typed in by the person, in whole shares. Nothing here
  reads the decision text, so a model writing "buy 10,000 shares" cannot size an order.
- Every order stays under a configurable maximum notional value
  (TRADINGAGENTS_MAX_ORDER_NOTIONAL, USD, default 1000), measured at the order's
  worst-case price (below); a buy stays within buying power and a sell within
  the shares held.
- Each order needs its own explicit approval by a signed-in person (see
  ``identity``), and carries a client order ID derived from the analysis run, so
  one run can place at most one order per side.
- Only US-listed stocks: other symbols (crypto pairs, foreign listings, indices)
  are not paper-traded here.

Order type and market hours
- **Limit orders only.** A buy limit never fills above its limit and a sell limit
  never below it, so the cap and buying power are checked at qty x limit price:
  the cap holds whatever the fill price. Market orders are refused: their fill
  price is unbounded, so no check made before sending could keep them under the
  cap. (For an immediate fill, set the limit at or above the current price; the
  order still never pays more than its limit.) Limit prices are whole cents
  (four decimals under $1), as Alpaca requires.
- A sell limit is valued at the higher of its limit and the latest price.
- Orders are DAY orders in the regular session only (no extended hours). A limit
  order placed while the market is closed waits for the next open.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from tradingagents.execution import identity as auth

BUY_RATINGS = frozenset({"Buy", "Overweight"})
SELL_RATINGS = frozenset({"Underweight", "Sell"})
MAX_NOTIONAL_ENV = "TRADINGAGENTS_MAX_ORDER_NOTIONAL"
DEFAULT_MAX_NOTIONAL = 1000.0
# Alpaca US equity symbols: letters, optionally a share class (BRK.B).
_US_STOCK = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
_CLIENT_ORDER_ID_MAX = 48   # Alpaca's limit
# An approval is for this order, now: it lapses after a few minutes, and it is
# signed with a key that exists only in this process, so an ApprovedOrder built
# or edited anywhere but approve() is refused at submission.
APPROVAL_TTL_SECONDS = 300
_APPROVAL_KEY = secrets.token_bytes(32)


def _now() -> datetime:
    return datetime.now(UTC)


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"   # named only so a request for one gets a clear refusal
    LIMIT = "limit"


MARKET_ORDERS_DISABLED = ("Market orders are disabled: paper orders are limit orders only, so the order "
                          "value is bounded by quantity x limit price. For an immediate fill, set the limit "
                          "at or above the current price.")


class OrderRejected(ValueError):
    """The order failed a check; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class OrderProposal:
    """What the analysis allows, before any quantity: a side, or the reason there is none."""

    ticker: str
    rating: str
    side: Side | None
    reason: str
    held_long_qty: float = 0.0

    @property
    def allowed(self) -> bool:
        return self.side is not None


@dataclass(frozen=True)
class OrderLimits:
    max_notional: float = DEFAULT_MAX_NOTIONAL

    def __post_init__(self):
        if not (math.isfinite(self.max_notional) and self.max_notional > 0):
            raise ValueError(f"max_notional must be a positive number, got {self.max_notional!r}")

    @classmethod
    def from_env(cls) -> OrderLimits:
        raw = (os.environ.get(MAX_NOTIONAL_ENV) or "").strip()
        if not raw:
            return cls()
        try:
            return cls(float(raw))
        except ValueError:
            raise ValueError(f"{MAX_NOTIONAL_ENV} must be a positive number of dollars, got {raw!r}") from None


@dataclass(frozen=True)
class ApprovedOrder:
    """An order a signed-in person approved after it passed every check. Only these reach the broker."""

    run_id: str
    ticker: str
    rating: str
    side: Side
    qty: int
    reference_price: float
    notional: float                  # at the worst-case price, at approval time
    client_order_id: str
    approved_at: str
    approved_by: str
    order_type: OrderType = OrderType.LIMIT
    limit_price: float | None = None
    auth_method: str = "none"
    session_ref: str = ""                            # hash of the approver's server-side session, if any
    approval: str = field(default="", repr=False)   # signature set by approve()


def _signature(order: ApprovedOrder) -> str:
    fields = {k: (v.value if isinstance(v, StrEnum) else v) for k, v in asdict(order).items() if k != "approval"}
    payload = json.dumps(fields, sort_keys=True, default=str).encode()
    return hmac.new(_APPROVAL_KEY, payload, hashlib.sha256).hexdigest()


def verify_approval(order: ApprovedOrder) -> list[str]:
    """Why this order's approval is not valid now; empty when it is.

    Valid means: issued by approve() in this process for exactly these fields,
    by a named person (signed in, when sign-in is required), and no older than
    APPROVAL_TTL_SECONDS.
    """
    if not isinstance(order, ApprovedOrder):
        return ["Only an order approved through approve() can be submitted."]
    if not order.approval or not hmac.compare_digest(order.approval, _signature(order)):
        return ["The order does not carry a valid approval (it was not approved as it stands); "
                "approve it again."]
    problems = []
    if not (order.approved_by or "").strip():
        problems.append("The approval does not name who approved it.")
    if auth.auth_required() and order.auth_method not in auth.AUTHENTICATED_METHODS:
        problems.append("Paper orders require a signed-in approver, and this approval has none.")
    try:
        approved_at = datetime.fromisoformat(order.approved_at)
    except (TypeError, ValueError):
        return [*problems, "The approval time cannot be read; approve the order again."]
    age = _now() - approved_at
    if age > timedelta(seconds=APPROVAL_TTL_SECONDS):
        problems.append(f"The approval is {age.total_seconds():.0f}s old, past the "
                        f"{APPROVAL_TTL_SECONDS}s limit; check the order and approve it again.")
    elif age < timedelta(seconds=-5):
        problems.append("The approval time is in the future; approve the order again.")
    return problems


def is_paper_tradable(ticker: str, asset_type: str = "stock") -> bool:
    return asset_type == "stock" and bool(_US_STOCK.fullmatch(ticker or ""))


def propose_order(ticker: str, rating: str, held_long_qty: float,
                  asset_type: str = "stock") -> OrderProposal:
    """The side a rating allows given the current long position, or why there is none.

    ``held_long_qty`` is the long position only; a short or no position is 0.
    """
    held = held_long_qty if held_long_qty and held_long_qty > 0 else 0.0
    if not is_paper_tradable(ticker, asset_type):
        return OrderProposal(ticker, rating, None,
                             f"{ticker} is not a US-listed stock; paper trading here covers those only.", held)
    if rating in BUY_RATINGS:
        return OrderProposal(ticker, rating, Side.BUY,
                             f"{rating}: a buy may be placed, sized by you.", held)
    if rating in SELL_RATINGS:
        if held <= 0:
            return OrderProposal(
                ticker, rating, None,
                f"{rating}, but the paper account holds no long position in {ticker}. "
                "Selling would open a short position, which is not supported.", held)
        return OrderProposal(ticker, rating, Side.SELL,
                             f"{rating}: you may reduce or close the {held:g}-share position.", held)
    if rating == "Hold":
        return OrderProposal(ticker, rating, None, "Hold: no order is created.", held)
    return OrderProposal(ticker, rating, None,
                         f"The decision has no tradeable rating ({rating}); review it before acting.", held)


def client_order_id(run_id: str, side: Side) -> str:
    """One ID per analysis run and side: the broker refuses a second order with it."""
    value = f"ta-{run_id}-{Side(side).value}"
    if len(value) > _CLIENT_ORDER_ID_MAX or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError(f"run_id {run_id!r} does not make a valid client order ID")
    return value


def _is_price(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def limit_price_problems(limit_price) -> list[str]:
    """Alpaca takes whole cents at $1 and above, and up to four decimals below $1."""
    if not _is_price(limit_price):
        return ["Enter a limit price above zero."]
    try:
        places = -Decimal(str(limit_price)).normalize().as_tuple().exponent
    except InvalidOperation:
        return ["Enter the limit price as a plain number."]
    allowed = 2 if limit_price >= 1 else 4
    if places > allowed:
        return [f"A limit price of ${limit_price} has more than {allowed} decimal places; "
                f"use whole cents{' (four decimals under $1)' if allowed == 2 else ''}."]
    return []


def worst_case_price(side: Side, order_type: OrderType, price: float | None,
                     limit_price: float | None) -> float | None:
    """The highest per-share value a limit order can reach, for the cap and buying power.

    A buy limit never fills above its limit. A sell is valued at the higher of its
    limit and the latest price (it fills at or above its limit). None for anything
    that is not a priced limit order.
    """
    if order_type is not OrderType.LIMIT or not _is_price(limit_price):
        return None
    if side is Side.BUY:
        return float(limit_price)
    return max(float(limit_price), float(price)) if _is_price(price) else float(limit_price)


def check_order(proposal: OrderProposal, qty, price, *, buying_power: float,
                held_long_qty: float, limits: OrderLimits, order_type: OrderType | str = OrderType.LIMIT,
                limit_price: float | None = None, market_open: bool | None = None) -> list[str]:
    """Every reason this order may not be placed; empty when it may.

    ``market_open`` is recorded by the broker; a limit order is allowed either way
    (after hours it waits for the open).
    """
    if not proposal.allowed:
        return [proposal.reason]
    try:
        kind = OrderType(order_type)
    except ValueError:
        return [f"Unknown order type {order_type!r}; paper orders are limit orders."]
    if kind is not OrderType.LIMIT:
        return [MARKET_ORDERS_DISABLED]
    problems = []
    whole = whole_shares(qty)
    if whole is None:
        problems.append("Enter the quantity as a whole number of shares, 1 or more.")
    problems += limit_price_problems(limit_price)
    worst = worst_case_price(proposal.side, kind, price, limit_price)
    if problems or worst is None:
        return problems or ["The order value cannot be checked."]
    notional = whole * worst
    basis = "at the limit price" if proposal.side is Side.BUY else "at the higher of the latest and limit prices"
    if exceeds(whole, worst, limits.max_notional):
        problems.append(f"Order value ${notional:,.2f} ({basis}) is over the ${limits.max_notional:,.2f} "
                        f"limit ({MAX_NOTIONAL_ENV}).")
    if proposal.side is Side.BUY and exceeds(whole, worst, max(buying_power or 0.0, 0.0)):
        problems.append(f"Order value ${notional:,.2f} ({basis}) is more than the buying power "
                        f"${buying_power:,.2f}.")
    if proposal.side is Side.SELL:
        held = held_long_qty if held_long_qty and held_long_qty > 0 else 0.0
        if whole > held:
            problems.append(f"Selling {whole} shares would go below zero: only {held:g} are held. "
                            "Short selling is not supported.")
    return problems


def approve(proposal: OrderProposal, qty, price, *, run_id: str, buying_power: float,
            held_long_qty: float, limits: OrderLimits, confirmed: bool,
            identity: auth.Identity | None = None, approved_by: str | None = None,
            order_type: OrderType | str = OrderType.LIMIT, limit_price: float | None = None,
            market_open: bool | None = None, trade_date: str | None = None, today: str | None = None,
            stale_acknowledged: bool = False) -> ApprovedOrder:
    """Turn a person's checked order into a signed ApprovedOrder, or raise OrderRejected.

    ``confirmed`` must be literally True: the person ticked the approval for this
    exact order. ``identity`` is their verified sign-in, required unless
    TRADINGAGENTS_REQUIRE_ORDER_AUTH is false (then ``approved_by`` names them,
    recorded as unauthenticated). A decision made for an earlier date also
    needs ``stale_acknowledged``.
    """
    problems = check_order(proposal, qty, price, buying_power=buying_power, held_long_qty=held_long_qty,
                           limits=limits, order_type=order_type, limit_price=limit_price,
                           market_open=market_open)
    if trade_date and today and trade_date != today and not stale_acknowledged:
        problems.append(f"The analysis is for {trade_date}, not today ({today}); "
                        "confirm you want to act on it anyway.")
    if confirmed is not True:
        problems.append("Tick the approval box for this order before submitting.")
    session = ""
    if identity is not None or auth.auth_required():
        identity_problems = auth.verify_identity(identity)
        if not identity_problems and not auth.confirmed_recently(identity):
            identity_problems = [f"Re-enter your password to approve this order (a confirmation lasts "
                                 f"{auth.STEP_UP_SECONDS // 60} minutes)."]
        problems += identity_problems
        who, method = (identity.subject, identity.method) if not identity_problems else ("", "none")
        session = auth.session_ref(identity.session) if not identity_problems else ""
    else:
        who, method = (approved_by or "").strip(), "none"
        if not who:
            problems.append("Name the person approving the order.")
    if problems:
        raise OrderRejected(problems)
    kind = OrderType(order_type)
    whole = whole_shares(qty)
    worst = worst_case_price(proposal.side, kind, price, limit_price)
    order = ApprovedOrder(
        run_id=run_id, ticker=proposal.ticker, rating=proposal.rating, side=proposal.side,
        qty=whole, reference_price=float(price) if _is_price(price) else 0.0,
        notional=round(whole * worst, 2), client_order_id=client_order_id(run_id, proposal.side),
        approved_at=_now().isoformat(timespec="seconds"), approved_by=who,
        order_type=kind, limit_price=float(limit_price),
        auth_method=method, session_ref=session,
    )
    return replace(order, approval=_signature(order))


def exceeds(qty: int, price: float, limit: float) -> bool:
    """Whether qty x price is over ``limit``, compared exactly (no binary-float rounding)."""
    return Decimal(int(qty)) * Decimal(repr(float(price))) > Decimal(repr(float(limit)))


def whole_shares(qty) -> int | None:
    """A positive whole number of shares, or None for anything else (fractions, text, bools)."""
    if isinstance(qty, bool) or not isinstance(qty, int | float):
        return None
    if not math.isfinite(qty) or qty < 1 or qty != int(qty):
        return None
    return int(qty)
