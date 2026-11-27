"""F1: the approval-decision audit log, ``<results_dir>/approval_decisions.jsonl`` (append-only).

``paper_orders.jsonl`` (alpaca_paper.OrderLedger) records orders that passed every check and went to Alpaca:
``approved`` -> ``submitted`` | ``failed``. Its meaning is unchanged. This separate log records the decisions
and attempts that never become an order, so a rejected proposal can never be mistaken for a submitted one:

- ``rejected``: the person pressed "Reject trade proposal" (category ``user_rejected``). One record per
  rejection: a repeated click or a Streamlit rerun finds the proposal already rejected and writes nothing.
- ``reconsidered``: the person deliberately reopened a rejected proposal; a fresh approval is still needed.
- ``approved`` (Phase 3, category ``user_approved``): a signed-in person's approval of an exact order that passed
  every check, written inside the broker's ledger lock immediately before the ledger's own ``approved`` line and
  before anything is sent. If it cannot be written, nothing is sent. Its ``decision_id`` is signed into the
  ApprovedOrder and copied to the order's ledger lines (``approval_decision_id``).
- ``submission_failed``: Submit was pressed but nothing reached Alpaca, or Alpaca refused it, with a category:
    ``proposal_rejected``  the proposal is rejected (refused before any check)
    ``missing_approval``   the approval tick, the stale-date acknowledgement, or the sign-in is missing
    ``invalid_order``      quantity, limit price, cap, buying power or position checks failed
    ``password_failed``    the step-up password was wrong (or the sign-in is locked out)
    ``broker_validation``  the broker's own checks refused it just before sending (fresh numbers, final cap,
                           approval signature, session, paper endpoint)
    ``alpaca_api_error``   Alpaca did not accept the request (also in paper_orders.jsonl as ``failed``)
    ``record_failed``      the proposal or the approval could not be recorded, so nothing was approved or sent

Each line: its ``decision_id`` (``dec-<32 hex>``; Phase 3), time (UTC), event, category, run ID, proposal ID (the
run's client order ID, ``ta-<run_id>-<side>``), ticker, side, quantity and limit price as entered, the signed-in
user's name and sign-in method, and the reasons (credential-looking values removed). Never a password, token or
key. A failure after an approval (``broker_validation`` just before sending is not one; ``alpaca_api_error`` is)
also names the approval's ``approval_decision_id``.

Lines written before Phase 3 have no ``decision_id``. They are never rewritten: ``entries()`` gives each one a
stable ``dec-legacy-<hash of the line>`` ID and ``legacy: True``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradingagents import audit_log
from tradingagents.dataflows.files import locked

LOG_NAME = "approval_decisions.jsonl"
SCHEMA_VERSION = 2            # 2: decision_id, approvals; lines without "schema" are version 1
ID_PREFIX = "dec"
CATEGORIES = ("user_rejected", "proposal_rejected", "missing_approval", "invalid_order", "password_failed",
              "broker_validation", "alpaca_api_error", "record_failed")
APPROVED_CATEGORY = "user_approved"


def new_decision_id() -> str:
    """``dec-<32 hex>``: 36 characters."""
    return audit_log.new_id(ID_PREFIX)


def _clean(problems) -> list[str]:
    from tradingagents.service import sanitize_error  # the dashboard has it loaded already

    return [sanitize_error(str(p), 300) for p in (problems or [])][:10]


class DecisionLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @classmethod
    def for_results_dir(cls, results_dir: str | Path) -> DecisionLog:
        return cls(Path(results_dir) / LOG_NAME)

    def entries(self, proposal_id: str | None = None) -> list[dict]:
        """Every decision (oldest first), optionally for one proposal. Old lines get a derived decision ID."""
        out = []
        for line, entry in audit_log.read_lines(self.path):
            if "decision_id" not in entry:
                entry = {**entry, "decision_id": audit_log.legacy_id(ID_PREFIX, line), "legacy": True}
            if proposal_id is None or entry.get("proposal_id") == proposal_id:
                out.append(entry)
        return out

    def get(self, decision_id: str) -> dict | None:
        return next((e for e in self.entries() if e.get("decision_id") == decision_id), None)

    def approvals(self, proposal_id: str | None = None) -> list[dict]:
        return [e for e in self.entries(proposal_id) if e.get("event") == "approved"]

    def rejection(self, proposal_id: str | None) -> dict | None:
        """The standing rejection of this proposal, or None (never rejected, or reconsidered since)."""
        if not proposal_id:
            return None
        last = None
        for entry in self.entries(proposal_id):
            if entry.get("event") in ("rejected", "reconsidered"):
                last = entry
        return last if last is not None and last["event"] == "rejected" else None

    def reconsiderations(self, proposal_id: str | None) -> int:
        return sum(1 for e in self.entries(proposal_id) if e.get("event") == "reconsidered") if proposal_id else 0

    def _entry(self, event: str, category: str, *, run_id: str, proposal_id: str | None, ticker: str,
               side: str | None, qty=None, limit_price=None, identity=None, problems=None,
               decision_id: str | None = None, approval_decision_id: str | None = None) -> dict[str, Any]:
        if category not in CATEGORIES and event == "submission_failed":
            raise ValueError(f"unknown category {category!r}")
        return {
            "schema": SCHEMA_VERSION,
            "decision_id": decision_id or new_decision_id(),
            "approval_decision_id": approval_decision_id or None,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "event": event,
            "category": category,
            "source": "dashboard",
            "run_id": run_id,
            "proposal_id": proposal_id,
            "ticker": ticker,
            "side": side,
            "qty": qty if qty not in (None, "") else None,
            "limit_price": limit_price or None,
            "user": getattr(identity, "subject", None),
            "auth_method": getattr(identity, "method", None) or "none",
            "problems": _clean(problems),
        }

    def _append(self, entry: dict) -> None:
        """Under ``locked(self.path)``, held by the caller."""
        audit_log.append(self.path, entry, lock=False)

    def reject(self, **fields) -> tuple[dict, bool]:
        """Record a rejection once: (the standing rejection, True if this call wrote it)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with locked(self.path):
            existing = self.rejection(fields.get("proposal_id"))
            if existing is not None:
                return existing, False
            entry = self._entry("rejected", "user_rejected", **fields)
            self._append(entry)
            return entry, True

    def reconsider(self, **fields) -> dict | None:
        """Reopen a rejected proposal (recorded); None when it is not rejected."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with locked(self.path):
            if self.rejection(fields.get("proposal_id")) is None:
                return None
            entry = self._entry("reconsidered", "user_rejected", **fields)
            self._append(entry)
            return entry

    def approve(self, order, *, identity=None) -> dict:
        """Record the approval of ``order`` (an order_policy.ApprovedOrder that passed every check).

        Called by the broker inside its ledger lock, after the duplicate checks and the fresh checks and before
        the order is sent, so each order sent has exactly one approval decision and a repeat submission (an order
        already placed) records none. Idempotent per decision ID. Raises OSError when it cannot be written; the
        caller then sends nothing."""
        decision_id = order.decision_id or new_decision_id()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with locked(self.path):
            if existing := self.get(decision_id):
                return existing
            entry = self._entry("approved", APPROVED_CATEGORY, run_id=order.run_id,
                                proposal_id=order.client_order_id, ticker=order.ticker, side=order.side.value,
                                qty=order.qty, limit_price=order.limit_price, identity=identity,
                                decision_id=decision_id)
            entry.update({"user": order.approved_by or entry["user"],
                          "auth_method": order.auth_method or entry["auth_method"],
                          "client_order_id": order.client_order_id, "approved_at": order.approved_at,
                          "notional": order.notional, "order_type": order.order_type.value,
                          "session_bound": bool(order.session_ref)})
            self._append(entry)
            return entry

    def failure(self, category: str, **fields) -> dict:
        """Record one failed submission attempt with its category."""
        entry = self._entry("submission_failed", category, **fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with locked(self.path):
            self._append(entry)
        return entry
