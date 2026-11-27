"""What an analysis rating may turn into as a paper trade: the pure F1 proposal rules (Phase 3).

Moved here unchanged from ``execution/order_policy.py`` so they have one definition that both interfaces use:
- F1 (``execution.order_policy``, which re-exports every name) adds the quantity, price, approval and broker
  checks;
- F2's read-only ``propose_trade`` tool reports the proposal without reaching the execution layer.

Standard library only: no broker, no network, no identity or signing. A proposal is never an order; it has no
quantity (a person types that in the F1 dashboard) and nothing here can approve, sign or send one.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from enum import StrEnum

BUY_RATINGS = frozenset({"Buy", "Overweight"})
SELL_RATINGS = frozenset({"Underweight", "Sell"})
MAX_NOTIONAL_ENV = "TRADINGAGENTS_MAX_ORDER_NOTIONAL"
DEFAULT_MAX_NOTIONAL = 1000.0
# Alpaca US equity symbols: letters, optionally a share class (BRK.B).
_US_STOCK = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
_CLIENT_ORDER_ID_MAX = 48   # Alpaca's limit


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


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


PROPOSAL_NO_SIDE = "none"
# "ta-" + a run ID (run_store.RUN_ID_PATTERN: up to 64 characters) + "-buy" | "-sell" | "-none"
_PROPOSAL_ID = re.compile(r"ta-[A-Za-z0-9][A-Za-z0-9_-]{0,63}-(buy|sell|none)")
PROPOSAL_ID_MAX = 72


def proposal_id(run_id: str, side: Side | str | None) -> str:
    """A trade proposal's stable ID: ``ta-<run_id>-<side>``, or ``ta-<run_id>-none`` when no trade is allowed.

    For a side it is exactly the order's ``client_order_id`` (when the run ID is short enough for Alpaca), so a
    proposal, its approval decisions and its paper order share one key. Raises ValueError for a run ID that is
    not a stored run's ID.
    """
    label = Side(side).value if side else PROPOSAL_NO_SIDE
    value = f"ta-{run_id}-{label}"
    if not isinstance(run_id, str) or not _PROPOSAL_ID.fullmatch(value):
        raise ValueError(f"run_id {run_id!r} does not make a valid proposal ID")
    return value


def is_valid_proposal_id(value) -> bool:
    return isinstance(value, str) and _PROPOSAL_ID.fullmatch(value) is not None


def client_order_id(run_id: str, side: Side) -> str:
    """One ID per analysis run and side: the broker refuses a second order with it."""
    value = f"ta-{run_id}-{Side(side).value}"
    if len(value) > _CLIENT_ORDER_ID_MAX or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError(f"run_id {run_id!r} does not make a valid client order ID")
    return value
