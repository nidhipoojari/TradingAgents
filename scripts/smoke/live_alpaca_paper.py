#!/usr/bin/env python3
"""Phase 3 live smoke test 2: the Alpaca PAPER account, read-only first, then one order.

NOT part of pytest. Each level needs its own approval flag; without one, it prints
the plan and an offline pre-flight check, makes no network call, and exits 2.

    python scripts/smoke/live_alpaca_paper.py                  # plan + pre-flight only
    python scripts/smoke/live_alpaca_paper.py --approve-read   # reads only: account, clock, price
    python scripts/smoke/live_alpaca_paper.py --approve-order --username alice
        # ONE paper limit order through the full backend gate, then cancelled. Also asks you to
        # type the order back before it is sent, and for your dashboard password (sign-in).

The order level buys 1 share (default symbol F) with a limit at half the last price,
so it should rest unfilled; it is cancelled at once. Paper account: no money moves.
Pass/fail criteria are the CHECKS listed in the plan; any FAIL exits 1.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _safety import fresh_dir, mask, provenance, redact  # noqa: E402

PLAN = """\
Live smoke test 2 (Alpaca paper) -- NOT RUN (pass --approve-read, or --approve-order, to run it)

--approve-read  (no orders; a handful of read requests to paper-api.alpaca.markets and the IEX data feed)
  read.endpoint        the trading client points at https://paper-api.alpaca.markets
  read.account         account readable, status ACTIVE, not blocked from trading
  read.clock           market clock readable (is_open, next_open)
  read.price           latest IEX trade price for the symbol > 0
  read.positions       positions readable (long-only check: no short in the symbol)
  local.cap            an order over TRADINGAGENTS_MAX_ORDER_NOTIONAL is refused before any request
  local.market         a market order is refused before any request
  local.ledger         the refusals wrote nothing to the order ledger

--approve-order  (everything above, then ONE order; expected cost $0, paper account)
  order.signin         your dashboard password signs you in (no sign-in, no order)
  order.confirm        you typed the exact order back
  order.submitted      Alpaca accepted a LIMIT BUY of 1 share at ~50% of the last price
                       (status new / accepted / pending_new), client_order_id ta-<run_id>-buy
  order.ledger         paper_orders.jsonl has "approved" then "submitted", with your user name
  order.duplicate      submitting the same approval again returns the first order (duplicate=True)
                       and Alpaca holds exactly one order with that client_order_id
  order.cancelled      the order is cancelled within 30 s
  order.unfilled       filled_qty == 0 (WARN otherwise: close the position in the paper account)
"""

ACCEPTED = {"new", "accepted", "pending_new", "accepted_for_bidding"}
# Every TradingClient method that changes orders, positions or the account.
WRITE_METHODS = ("submit_order", "cancel_order_by_id", "cancel_orders", "replace_order_by_id",
                 "close_position", "close_all_positions", "exercise_options_position",
                 "create_watchlist", "add_asset_to_watchlist_by_id", "delete_watchlist_by_id",
                 "update_watchlist_by_id", "remove_asset_from_watchlist_by_id")


class ReadOnlyViolation(RuntimeError):
    """A write was attempted in --approve-read mode."""


class ReadOnlyTrading:
    """Wraps the trading client in --approve-read mode: reads pass through, every write raises."""

    def __init__(self, client):
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name):
        if name in WRITE_METHODS:
            def refuse(*args, **kwargs):
                raise ReadOnlyViolation(f"{name} refused: this run was approved for reads only")
            return refuse
        return getattr(self._client, name)


# Lookups where "not found" is a normal answer: no position held, no earlier order with this ID.
NOT_FOUND_IS_EXPECTED = frozenset({"get_open_position", "get_order_by_client_id"})
EXPECTED_NOT_FOUND = "EXPECTED_NOT_FOUND"


def request_outcome(method: str, exc: BaseException | None) -> str:
    """How one request ended: "ok", EXPECTED_NOT_FOUND (a 404 from a lookup above), or "ERROR: <type>"."""
    if exc is None:
        return "ok"
    from tradingagents.execution.alpaca_paper import _is_not_found

    if method in NOT_FOUND_IS_EXPECTED and isinstance(exc, Exception) and _is_not_found(exc):
        return EXPECTED_NOT_FOUND
    return f"ERROR: {type(exc).__name__}"


class RequestMeter:
    """Wraps a client: records each call's method name, duration and outcome (never its arguments or results).

    The call itself is untouched: results are returned and exceptions re-raised exactly as before.
    """

    def __init__(self, client, log: list, label: str):
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_log", log)
        object.__setattr__(self, "_label", label)

    def __getattr__(self, name):
        value = getattr(self._client, name)
        if not callable(value) or name.startswith("_"):
            return value

        def timed(*args, **kwargs):
            started = time.perf_counter()
            failure = None
            try:
                return value(*args, **kwargs)
            except BaseException as exc:
                failure = exc
                raise
            finally:
                self._log.append({"client": self._label, "method": name, "outcome": request_outcome(name, failure),
                                  "ms": round((time.perf_counter() - started) * 1000, 1)})
        return timed


def summarize_requests(requests: list) -> tuple[bool, str]:
    """(no unexpected errors, detail line) for the report."""
    errors = [r for r in requests if r["outcome"].startswith("ERROR")]
    expected = sum(r["outcome"] == EXPECTED_NOT_FOUND for r in requests)
    total_ms = round(sum(r["ms"] for r in requests), 1)
    detail = (f"{len(requests)} requests, {total_ms} ms total ({len(requests) - expected - len(errors)} ok, "
              f"{expected} {EXPECTED_NOT_FOUND}, {len(errors)} unexpected errors): "
              + ", ".join(f"{r['client']}.{r['method']} {r['ms']} ms"
                          + ("" if r["outcome"] == "ok" else f" [{r['outcome']}]") for r in requests))
    return bool(requests) and not errors, detail


def make_read_only(broker) -> None:
    """Refuse every write: on the client, and at the broker's own submit."""
    broker.trading = ReadOnlyTrading(broker.trading)

    def no_submit(*args, **kwargs):
        raise ReadOnlyViolation("submit refused: this run was approved for reads only")

    broker.submit = no_submit


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name, ok, detail="", warn=False):
        self.rows.append((name, "PASS" if ok else ("WARN" if warn else "FAIL"), redact(detail)))

    @property
    def failed(self):
        return any(s == "FAIL" for _, s, _ in self.rows)

    def show(self):
        width = max(len(n) for n, _, _ in self.rows)
        for name, status, detail in self.rows:
            print(f"  {status:4}  {name:{width}}  {detail}")


def preflight() -> list[str]:
    from tradingagents.execution import alpaca_paper as ap

    problems = []
    if not ap._env(ap.KEY_ENVS) or not ap._env(ap.SECRET_ENVS):
        problems.append("ALPACA_API_KEY and ALPACA_SECRET_KEY (paper keys) are not set")
    flag = ap._env(ap.PAPER_FLAG_ENVS)
    if flag and flag.lower() not in {"1", "true", "yes"}:
        problems.append("ALPACA_PAPER is set to false")
    base = ap._env(ap.BASE_URL_ENVS).rstrip("/")
    if base and base != ap.PAPER_TRADING_URL:
        problems.append(f"{base} is not the paper endpoint")
    return problems


def read_checks(report, broker, symbol, limits):
    from tradingagents.execution import alpaca_paper as ap, order_policy as op

    report.add("read.endpoint", ap._base_url(broker.trading) == ap.PAPER_TRADING_URL, ap._base_url(broker.trading))
    account = broker.account()
    report.add("read.account", account.status.upper() == "ACTIVE" and not account.trading_blocked,
               f"{account.status}, buying power ${account.buying_power:,.2f}")
    clock = broker.market_clock()
    report.add("read.clock", isinstance(clock.is_open, bool) and bool(clock.next_open),
               f"open={clock.is_open} next_open={clock.next_open}")
    price = broker.latest_price(symbol)
    report.add("read.price", bool(price) and price > 0, f"{symbol} ${price}")
    held = broker.long_position(symbol)
    positions = broker.positions()
    short = [p for p in positions if p["ticker"] == symbol and p["quantity"] < 0]
    report.add("read.positions", not short, f"{len(positions)} positions; {symbol} long {held:g}")

    entries_before = len(broker.ledger.entries())
    buy = op.propose_order(symbol, "Buy", held)
    # Local refusals: no identity needed (and none minted); the order would be refused anyway.
    too_big = int(limits.max_notional // max(price or 1, 0.01)) + 1
    try:
        op.approve(buy, too_big, price, run_id="smoke-cap", buying_power=account.buying_power,
                   held_long_qty=held, limits=limits, confirmed=True, limit_price=round(price or 1, 2))
        report.add("local.cap", False, "an over-cap order was approved")
    except op.OrderRejected as exc:
        report.add("local.cap", "limit" in str(exc), f"{too_big} shares refused")
    try:
        op.approve(buy, 1, price, run_id="smoke-mkt", buying_power=account.buying_power, held_long_qty=held,
                   limits=limits, confirmed=True, order_type="market")
        report.add("local.market", False, "a market order was approved")
    except op.OrderRejected as exc:
        report.add("local.market", "Market orders are disabled" in str(exc), "refused")
    report.add("local.ledger", len(broker.ledger.entries()) == entries_before, "no ledger lines written")
    return account, clock, price, held


TERMINAL = {"canceled", "filled", "expired", "rejected", "done_for_day", "replaced", "stopped", "suspended"}


def _status_of(order) -> tuple[str, float, float | None]:
    status = str(getattr(getattr(order, "status", ""), "value", getattr(order, "status", "")) or "")
    filled = float(getattr(order, "filled_qty", 0) or 0)
    avg = getattr(order, "filled_avg_price", None)
    return status, filled, float(avg) if avg not in (None, "") else None


def order_checks(report, broker, symbol, limits, account, clock, price, held, username, limit_fraction):
    """ONE paper limit buy of 1 share, sent once through the full backend gate, then cancelled and verified.

    Never retried, never followed by another order: a fill is reported for you to close by hand.
    """
    from tradingagents import service
    from tradingagents.execution import identity as auth, order_policy as op

    try:
        identity = auth.sign_in_with_password(username, getpass.getpass(f"Dashboard password for {username}: "),
                                              store=broker.sessions)
    except auth.AuthError as exc:
        report.add("order.signin", False, str(exc))
        return
    report.add("order.signin", True, identity.subject)
    limit = round(price * limit_fraction, 2)
    try:
        order = op.approve(op.propose_order(symbol, "Buy", held), 1, price, run_id=service.new_run_id(),
                           buying_power=account.buying_power, held_long_qty=held, limits=limits, confirmed=True,
                           identity=identity, limit_price=limit, market_open=clock.is_open)
    except op.OrderRejected as exc:
        report.add("order.approved", False, f"refused before anything was sent: {exc}")
        return
    shape_ok = (order.qty == 1 and order.side is op.Side.BUY and order.order_type is op.OrderType.LIMIT
                and order.limit_price == limit and limit <= price * 0.9
                and order.notional <= limits.max_notional and order.session_ref)
    report.add("order.parameters", shape_ok,
               f"BUY 1 {symbol} LIMIT ${limit:.2f} DAY, regular session only; value ${order.notional:.2f} "
               f"(cap ${limits.max_notional:,.2f}); {limit / price:.0%} of last ${price:.2f}; "
               f"approved by {order.approved_by} (session-bound); client order id {order.client_order_id}")
    if not shape_ok:
        return
    phrase = f"buy 1 {symbol} at {limit:.2f}"
    print(f"\nAbout to send ONE paper order to paper account {mask(account.account_number)}:\n"
          f"  BUY 1 share of {symbol}, LIMIT ${limit:.2f} (last ${price:.2f}), DAY, regular session only,\n"
          f"  value ${order.notional:.2f}, client order id {order.client_order_id}; market "
          f"{'open' if clock.is_open else 'closed (it would rest until the open)'}.\n"
          f"  It is cancelled right after it is accepted. Nothing is retried and no other order is sent.")
    typed = input(f'Type exactly "{phrase}" to send it, anything else to stop: ').strip()
    report.add("order.confirm", typed == phrase, "confirmed" if typed == phrase else "not confirmed; nothing sent")
    if typed != phrase:
        return

    order_id, cancelled_requested = None, False
    try:
        try:
            receipt = broker.submit(order, limits, source="api")
        except op.OrderRejected as exc:
            report.add("order.submitted", False, f"refused by the backend before sending: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - the request may have reached Alpaca anyway
            # No retry. Ask Alpaca once whether the order exists, so it can still be cancelled.
            try:
                existing = broker.trading.get_order_by_client_id(order.client_order_id)
            except Exception:  # noqa: BLE001
                existing = None
            if existing is None:
                report.add("order.submitted", False, f"not placed: {type(exc).__name__}: {str(exc)[:200]}")
                return
            order_id = str(existing.id)
            report.add("order.submitted", False, f"submission reported {type(exc).__name__}, but Alpaca holds order "
                                                 f"{order_id}; it is cancelled below")
        else:
            order_id = receipt.order_id
            report.add("order.submitted", receipt.status in ACCEPTED and not receipt.duplicate and bool(order_id),
                       f"{receipt.status} {order_id} {receipt.client_order_id}")
            events = [e for e in broker.ledger.entries() if e.get("client_order_id") == order.client_order_id]
            report.add("order.ledger", [e["event"] for e in events] == ["approved", "submitted"]
                       and all(e["approved_by"] == identity.subject for e in events), str([e["event"] for e in events]))
            again = broker.submit(order, limits, source="api")      # must NOT reach Alpaca: the ledger answers
            held_order = broker.trading.get_order_by_client_id(order.client_order_id)
            report.add("order.duplicate", again.duplicate and again.order_id == order_id
                       and str(held_order.id) == order_id, f"duplicate={again.duplicate}, one order {order_id}")
        try:
            cancelled_requested = True
            broker.trading.cancel_order_by_id(order_id)
            report.add("order.cancel_request", True, f"cancel sent for {order_id}")
        except Exception as exc:  # noqa: BLE001 - reported; the final status below says what happened
            report.add("order.cancel_request", False, f"cancel failed: {type(exc).__name__}: {str(exc)[:200]}")
    finally:
        if order_id and not cancelled_requested:                  # interrupted between submit and cancel
            try:
                broker.trading.cancel_order_by_id(order_id)
            except Exception:  # noqa: BLE001
                print(f"WARNING: could not cancel paper order {order_id}; cancel it in the Alpaca paper dashboard.")
    if not order_id:
        return
    status, filled, avg = "", 0.0, None
    for _ in range(30):
        try:
            status, filled, avg = _status_of(broker.trading.get_order_by_client_id(order.client_order_id))
        except Exception as exc:  # noqa: BLE001
            status = f"unknown ({type(exc).__name__})"
        if status in TERMINAL:
            break
        time.sleep(1)
    report.add("order.final_status", status == "canceled",
               f"{status}{'' if status in TERMINAL else ' (not final after 30 s: check the paper dashboard)'}")
    if filled > 0:
        kind = "fully" if filled >= 1 else "partially"
        report.add("order.unfilled", False,
                   f"ACTION REQUIRED: {kind} filled {filled:g} share(s) at ${avg}; a paper position was opened. "
                   "Nothing is sold automatically: close it in the Alpaca paper dashboard.")
    else:
        report.add("order.unfilled", True, "filled_qty=0: no position opened")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--approve-read", action="store_true", help="read the paper account (no orders)")
    parser.add_argument("--approve-order", action="store_true", help="also place and cancel ONE paper order")
    parser.add_argument("--symbol", default="F")
    parser.add_argument("--username", help="dashboard sign-in user (order level)")
    parser.add_argument("--limit-fraction", type=float, default=0.5, help="limit as a fraction of the last price")
    parser.add_argument("--out", default=str(ROOT / "results" / "phase3_smoke" / f"alpaca_{datetime.now():%Y%m%d_%H%M%S}"))
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    problems = preflight()
    if args.approve_order and not args.username:
        problems.append("--approve-order needs --username (the dashboard sign-in)")
    if not 0 < args.limit_fraction <= 0.9:
        problems.append("--limit-fraction must be above 0 and at most 0.9, so the order rests unfilled")

    if not (args.approve_read or args.approve_order):
        print(PLAN)
        print("Pre-flight (offline):", "ready" if not problems else "NOT READY")
        for problem in problems:
            print(f"  - {redact(problem)}")
        return 2
    if problems:
        print("Pre-flight failed; nothing was run:")
        for problem in problems:
            print(f"  - {redact(problem)}")
        return 1

    from tradingagents.execution import alpaca_paper as ap, order_policy as op

    out = fresh_dir(args.out)
    run_info = provenance('live_alpaca_paper.py', raw_argv)
    print("Run:", json.dumps(run_info))
    out.mkdir(parents=True, exist_ok=True)
    broker = ap.AlpacaPaperBroker.from_env(ledger=ap.OrderLedger(out / ap.LEDGER_NAME))
    if args.approve_order:
        from tradingagents.execution import identity as auth

        broker.sessions = auth.SessionStore(out / "auth.sqlite")   # the order is bound to this sign-in session
    limits = op.OrderLimits.from_env()
    report = Report()
    if not args.approve_order:
        make_read_only(broker)          # --approve-read: structurally unable to place or cancel anything
    requests: list = []
    broker.trading = RequestMeter(broker.trading, requests, "trading")
    broker.data = RequestMeter(broker.data, requests, "data")
    try:
        account, clock, price, held = read_checks(report, broker, args.symbol, limits)
        if args.approve_order and not report.failed:
            order_checks(report, broker, args.symbol, limits, account, clock, price, held,
                         args.username, args.limit_fraction)
    except Exception as exc:  # noqa: BLE001 - reported as the failure it is
        report.add("unexpected", False, f"{type(exc).__name__}: {str(exc)[:200]}")
    if not args.approve_order:
        report.add("read.no_writes", True, "read-only client: every order/position method refused")
        writes = [r for r in requests if r["method"] in WRITE_METHODS]
        report.add("read.no_write_requests", not writes,
                   f"{len(requests)} requests, none an order/position write" if not writes else f"writes attempted: {writes}")
    ok, detail = summarize_requests(requests)
    report.add("requests", ok, detail)
    print(f"Live smoke test 2 results ({out}):")
    report.show()
    (out / "smoke_report.json").write_text(json.dumps(
        {"run": {**run_info, "finished_at_local": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
                 "output_dir": str(out)},
         "checks": [{"check": n, "status": s, "detail": d} for n, s, d in report.rows]}, indent=2), encoding="utf-8")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
