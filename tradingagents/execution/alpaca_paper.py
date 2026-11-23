"""F1: the Alpaca paper-trading account, and nothing else.

The only module that imports ``alpaca`` (a layering test pins it). It refuses to
run against anything but Alpaca's paper endpoint: the trading client is always
built with ``paper=True`` and no URL override, a configuration asking for live
trading is refused, and the client's base URL is checked again before use.

Orders arrive only as ``ApprovedOrder`` (see order_policy.approve). Before one
is sent, ``submit`` checks, under a lock held across threads and processes:
1. the local audit ledger, ``<results_dir>/paper_orders.jsonl``: an order ID
   already submitted is returned, not sent again;
2. Alpaca: an order with the same client order ID is returned, not sent again;
3. the order once more against fresh buying power, position and price, since
   they may have moved between approval and submission.

Keys come from ALPACA_API_KEY / ALPACA_SECRET_KEY (or Alpaca's own
APCA_API_KEY_ID / APCA_API_SECRET_KEY). Use paper-account keys.
"""

from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradingagents.dataflows.files import locked
from tradingagents.execution.order_policy import (
    MARKET_ORDERS_DISABLED,
    ApprovedOrder,
    OrderLimits,
    OrderRejected,
    OrderType,
    Side,
    check_order,
    exceeds,
    propose_order,
    verify_approval,
    whole_shares,
    worst_case_price,
)

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
KEY_ENVS = ("ALPACA_API_KEY", "APCA_API_KEY_ID")
SECRET_ENVS = ("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")
PAPER_FLAG_ENVS = ("ALPACA_PAPER",)
BASE_URL_ENVS = ("ALPACA_BASE_URL", "APCA_API_BASE_URL")
LEDGER_NAME = "paper_orders.jsonl"

_SUBMIT_LOCK = threading.Lock()


class BrokerError(RuntimeError):
    """The paper account could not be used or refused the request."""


class BrokerNotConfigured(BrokerError):
    """No paper-account keys, or a configuration that is not paper-only."""


@dataclass(frozen=True)
class Account:
    account_number: str
    status: str
    currency: str
    buying_power: float
    cash: float
    equity: float | None
    trading_blocked: bool


@dataclass(frozen=True)
class OrderReceipt:
    client_order_id: str
    order_id: str
    status: str
    symbol: str
    side: str
    qty: float
    submitted_at: str
    duplicate: bool = False   # True: an earlier submission with this ID was returned


@dataclass(frozen=True)
class MarketClock:
    is_open: bool
    next_open: str
    next_close: str


def _env(names) -> str:
    return next((os.environ[n].strip() for n in names if (os.environ.get(n) or "").strip()), "")


def _base_url(client) -> str:
    base = getattr(client, "_base_url", None)
    return str(getattr(base, "value", base) or "").rstrip("/")


def _assert_paper(client) -> None:
    if _base_url(client) != PAPER_TRADING_URL:
        raise BrokerNotConfigured(
            f"refusing a trading client for {_base_url(client) or 'an unknown endpoint'}; "
            f"only Alpaca paper trading ({PAPER_TRADING_URL}) is supported")


def _number(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _is_not_found(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 404:
        return True
    text = str(exc).lower()
    return "not found" in text or "does not exist" in text or "40410000" in text


def _status(value) -> str:
    return str(getattr(value, "value", value) or "")


class OrderLedger:
    """Append-only JSONL record of every paper order a person approved and sent."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @classmethod
    def for_results_dir(cls, results_dir: str | Path) -> OrderLedger:
        return cls(Path(results_dir) / LEDGER_NAME)

    def entries(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def find(self, client_order_id: str) -> dict | None:
        return next((e for e in self.entries()
                     if e.get("client_order_id") == client_order_id and e.get("order_id")), None)

    def append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return locked(self.path)


def _order_request(symbol: str, qty: int, side: Side, client_order_id: str,
                   order_type: OrderType, limit_price: float | None):
    """A regular-session DAY limit order (extended hours off). Nothing else can be built."""
    if order_type is not OrderType.LIMIT:
        raise OrderRejected([MARKET_ORDERS_DISABLED])
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    return LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY if side is Side.BUY else OrderSide.SELL,
                             time_in_force=TimeInForce.DAY, limit_price=limit_price,
                             client_order_id=client_order_id, extended_hours=False)


def _latest_trade_request(symbol: str):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockLatestTradeRequest

    # IEX: the feed a free paper account may query; SIP needs a paid data plan.
    return StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)


class AlpacaPaperBroker:
    def __init__(self, trading, data=None, ledger: OrderLedger | None = None, sessions=None):
        _assert_paper(trading)
        self.trading, self.data, self.ledger = trading, data, ledger
        # identity.SessionStore: when set, an order is sent only while its approver's
        # server-side session is still active (not signed out, idle, expired or pre-restart).
        self.sessions = sessions

    @classmethod
    def from_env(cls, ledger: OrderLedger | None = None) -> AlpacaPaperBroker:
        key, secret = _env(KEY_ENVS), _env(SECRET_ENVS)
        if not key or not secret:
            raise BrokerNotConfigured(
                "set ALPACA_API_KEY and ALPACA_SECRET_KEY (paper-account keys) to enable paper trading")
        paper_flag = _env(PAPER_FLAG_ENVS)
        if paper_flag and paper_flag.lower() not in {"1", "true", "yes"}:
            raise BrokerNotConfigured("ALPACA_PAPER is set to false; live trading is not supported")
        base = _env(BASE_URL_ENVS).rstrip("/")
        if base and base != PAPER_TRADING_URL:
            raise BrokerNotConfigured(f"{base} is not the Alpaca paper endpoint; live trading is not supported")
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.trading.client import TradingClient
        except ImportError:
            raise BrokerNotConfigured('install the dashboard extra: pip install ".[dashboard]"') from None
        # paper=True and never url_override: the client can only reach the paper endpoint.
        return cls(TradingClient(key, secret, paper=True), StockHistoricalDataClient(key, secret), ledger)

    def account(self) -> Account:
        try:
            raw = self.trading.get_account()
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"could not read the paper account: {type(exc).__name__}: {exc}") from None
        return Account(
            account_number=str(getattr(raw, "account_number", "") or ""),
            status=_status(getattr(raw, "status", "")),
            currency=str(getattr(raw, "currency", "") or "USD"),
            buying_power=_number(getattr(raw, "buying_power", 0)),
            cash=_number(getattr(raw, "cash", 0)),
            equity=_number(getattr(raw, "equity", None), default=None),
            trading_blocked=bool(getattr(raw, "trading_blocked", False)
                                 or getattr(raw, "account_blocked", False)),
        )

    def long_position(self, symbol: str) -> float:
        """Shares held long in ``symbol``; 0 for none (or a short, which is never added to)."""
        try:
            position = self.trading.get_open_position(symbol)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return 0.0
            raise BrokerError(f"could not read the {symbol} position: {type(exc).__name__}: {exc}") from None
        qty = _number(getattr(position, "qty", 0))
        if _status(getattr(position, "side", "long")).lower() == "short" or qty <= 0:
            return 0.0
        return qty

    def positions(self) -> list[dict]:
        """Every open position as ``{ticker, quantity, average_price}`` (read-only)."""
        try:
            raw = self.trading.get_all_positions()
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"could not read the positions: {type(exc).__name__}: {exc}") from None
        out = []
        for p in raw or []:
            qty = _number(getattr(p, "qty", 0))
            if _status(getattr(p, "side", "long")).lower() == "short":
                qty = -abs(qty)
            out.append({"ticker": str(getattr(p, "symbol", "")), "quantity": qty,
                        "average_price": _number(getattr(p, "avg_entry_price", None), default=None)})
        return out

    def market_clock(self) -> MarketClock:
        """Whether the regular session is open now, from Alpaca's clock."""
        try:
            clock = self.trading.get_clock()
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"could not read the market clock: {type(exc).__name__}: {exc}") from None
        def stamp(value):
            return value.isoformat() if hasattr(value, "isoformat") else str(value or "")
        return MarketClock(is_open=getattr(clock, "is_open", None) is True,
                           next_open=stamp(getattr(clock, "next_open", None)),
                           next_close=stamp(getattr(clock, "next_close", None)))

    def latest_price(self, symbol: str) -> float | None:
        if self.data is None:
            return None
        try:
            trades = self.data.get_stock_latest_trade(_latest_trade_request(symbol))
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"could not read the {symbol} price: {type(exc).__name__}: {exc}") from None
        trade = trades.get(symbol) if isinstance(trades, dict) else trades
        price = _number(getattr(trade, "price", None), default=float("nan"))
        return price if math.isfinite(price) and price > 0 else None

    def _existing(self, client_order_id: str):
        try:
            return self.trading.get_order_by_client_id(client_order_id)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return None
            raise BrokerError(f"could not check for an earlier order: {type(exc).__name__}: {exc}") from None

    def submit(self, order: ApprovedOrder, limits: OrderLimits | None = None, *,
               source: str = "dashboard") -> OrderReceipt:
        """Send one approved order to the paper account, at most once per client order ID.

        Every safeguard runs here, immediately before sending, whatever a caller
        checked earlier (the dashboard's checks are for display; this is the gate):
        - the approval: signed by order_policy.approve() for exactly this order,
          naming who approved it, and not expired;
        - the cap: TRADINGAGENTS_MAX_ORDER_NOTIONAL, read now; a caller's
          ``limits`` can only lower it, never raise it;
        - the paper endpoint, the audit ledger, and no earlier order with this ID;
        - the quantity against fresh buying power, the fresh long position and
          the fresh latest price (no price, no order).
        The approval is written to the ledger before the order is sent.
        """
        if not isinstance(order, ApprovedOrder):
            raise TypeError("only an ApprovedOrder (see order_policy.approve) can be submitted")
        problems = verify_approval(order)
        if not problems and self.sessions is not None:
            if order.session_ref:
                problems = self.sessions.check_ref(order.session_ref)
            elif order.auth_method != "none":
                problems = ["The approval is not bound to a dashboard session; sign in and approve again."]
        if problems:
            raise OrderRejected(problems)
        if self.ledger is None:
            raise BrokerNotConfigured("an audit ledger is required to submit paper orders")
        _assert_paper(self.trading)
        effective = _effective_limits(limits)

        with _SUBMIT_LOCK, self.ledger.lock():
            if seen := self.ledger.find(order.client_order_id):
                return OrderReceipt(**{**_receipt_fields(seen), "duplicate": True})
            existing = self._existing(order.client_order_id)
            if existing is not None:
                receipt = self._receipt(existing, order, duplicate=True)
                self._record("submitted", order, source, effective, receipt=receipt)
                return receipt

            # Fresh numbers: buying power, position, price and the market clock may
            # have moved since approval.
            account = self.account()
            if account.trading_blocked:
                raise BrokerError("the paper account is blocked from trading")
            held = self.long_position(order.ticker)
            price = self.latest_price(order.ticker)
            clock = self.market_clock()
            if price is None:
                raise OrderRejected([f"No current price for {order.ticker}, so the order value cannot be "
                                     "checked; nothing was sent."])
            proposal = propose_order(order.ticker, order.rating, held)
            problems = ([] if proposal.side is order.side else
                        [f"the account no longer allows a {order.side.value} of {order.ticker}: {proposal.reason}"])
            problems += check_order(proposal, order.qty, price, buying_power=account.buying_power,
                                    held_long_qty=held, limits=effective, order_type=order.order_type,
                                    limit_price=order.limit_price, market_open=clock.is_open)
            if problems:
                raise OrderRejected(problems)

            request = _order_request(order.ticker, order.qty, order.side, order.client_order_id,
                                     order.order_type, order.limit_price)
            worst = _final_cap_check(request, order, price, limits)
            checked = {"checked_price": price, "checked_buying_power": account.buying_power,
                       "checked_long_qty": held, "checked_notional": round(order.qty * worst, 2),
                       "order_type": order.order_type.value, "limit_price": order.limit_price,
                       "market_open": clock.is_open, "next_open": clock.next_open}
            self._record("approved", order, source, effective, **checked)
            try:
                placed = self.trading.submit_order(order_data=request)
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                self._record("failed", order, source, effective, error=error[:500], **checked)
                raise BrokerError(f"Alpaca did not accept the order: {error}") from None
            receipt = self._receipt(placed, order, duplicate=False)
            self._record("submitted", order, source, effective, receipt=receipt, **checked)
            return receipt

    @staticmethod
    def _receipt(raw, order: ApprovedOrder, duplicate: bool) -> OrderReceipt:
        submitted = getattr(raw, "submitted_at", None)
        return OrderReceipt(
            client_order_id=str(getattr(raw, "client_order_id", order.client_order_id)),
            order_id=str(getattr(raw, "id", "")),
            status=_status(getattr(raw, "status", "")),
            symbol=str(getattr(raw, "symbol", order.ticker) or order.ticker),
            side=_status(getattr(raw, "side", order.side.value)) or order.side.value,
            qty=_number(getattr(raw, "qty", order.qty), default=float(order.qty)),
            submitted_at=submitted.isoformat() if hasattr(submitted, "isoformat") else str(submitted or ""),
            duplicate=duplicate,
        )

    def _record(self, event: str, order: ApprovedOrder, source: str, limits: OrderLimits, *,
                receipt: OrderReceipt | None = None, error: str | None = None, **checked) -> None:
        """One audit line: ``approved`` (before sending), then ``submitted`` or ``failed``."""
        self.ledger.append({
            "event": event,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "source": source,
            "approved_by": order.approved_by,
            "auth_method": order.auth_method,
            "session_bound": bool(order.session_ref),
            "approved_at": order.approved_at,
            "run_id": order.run_id,
            "rating": order.rating,
            "ticker": order.ticker,
            "requested_side": order.side.value,
            "requested_qty": order.qty,
            "approved_reference_price": order.reference_price,
            "approved_notional": order.notional,
            "max_notional": limits.max_notional,
            "client_order_id": order.client_order_id,
            "endpoint": PAPER_TRADING_URL,
            **checked,
            **({"error": error} if error else {}),
            **(asdict(receipt) if receipt is not None else {}),
        })


def _effective_limits(limits: OrderLimits | None) -> OrderLimits:
    """The configured cap, read now; a caller's limit can only make it stricter."""
    try:
        configured = OrderLimits.from_env()
    except ValueError as exc:
        raise OrderRejected([f"{exc}; no order is sent until it is fixed."]) from None
    if limits is None:
        return configured
    return OrderLimits(min(configured.max_notional, limits.max_notional))


def _final_cap_check(request, order: ApprovedOrder, price: float,
                     caller_limits: OrderLimits | None) -> float:
    """The last gate, on the request object itself, just before it goes to Alpaca.

    Independent of check_order: it re-reads the configured cap and recomputes the
    worst-case value from the quantity, side, type and limit price in the request
    that will actually be sent, so a mismatch between the approved order and the
    request (or a check skipped above) cannot send an order over the cap.
    Returns the worst-case per-share price.
    """
    cap = _effective_limits(caller_limits).max_notional
    # alpaca-py stores qty as a float (3 -> 3.0); compare it as whole shares.
    qty = whole_shares(getattr(request, "qty", None))
    side = Side(_status(getattr(request, "side", "")) or "")
    raw_type = _status(getattr(request, "type", None))
    if raw_type != OrderType.LIMIT.value or getattr(request, "limit_price", None) is None:
        raise OrderRejected([f"Final check: only limit orders are sent, not {raw_type or 'an untyped order'}; "
                             "nothing was sent."])
    kind = OrderType.LIMIT
    mismatched = (qty != order.qty or side is not order.side or kind is not order.order_type
                  or getattr(request, "limit_price", None) != order.limit_price
                  or getattr(request, "client_order_id", None) != order.client_order_id)
    worst = worst_case_price(side, kind, price, getattr(request, "limit_price", None))
    if mismatched or worst is None or qty is None:
        raise OrderRejected(["The order about to be sent does not match the approved order; nothing was sent."])
    if exceeds(qty, worst, cap):
        raise OrderRejected([f"Final check: ${qty * worst:,.2f} is over the ${cap:,.2f} limit; nothing was sent."])
    return worst


def _receipt_fields(entry: dict[str, Any]) -> dict[str, Any]:
    names = OrderReceipt.__dataclass_fields__
    return {k: entry.get(k) for k in names if k != "duplicate"}

