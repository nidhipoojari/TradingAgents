"""Trade proposals as persistent records: ``<results_dir>/proposals/<proposal_id>.json`` (Phase 3, F1).

A proposal is what a completed analysis allows before any quantity: a side (buy, or sell to reduce a long
position), or the reason there is no trade. ``trade_rules.propose_order`` decides it; this module records it once.

- ID: ``trade_rules.proposal_id(run_id, side)`` = ``ta-<run_id>-<buy|sell|none>`` (up to 72 characters). For a
  side it equals the order's ``client_order_id``, the key the approval-decision log and the order ledger already
  use. One run has at most one eligible proposal (its rating fixes the side); a Sell-rated run may also have a
  ``none`` proposal, made while no long position was held.
- Written by the F1 dashboard when it shows a run's trade panel (the paper position is known there), and checked
  again before an approval. Each record is created once (exclusive create): a page rerun, a second tab or another
  process finds the existing record and changes nothing. The first record keeps its facts (e.g. the held quantity).
- Never an order: no quantity, no price, no approval and nothing that can be sent. The MCP ``propose_trade`` tool
  writes no proposal record (it cannot see the paper account, so its view is a preview).

Standard library and ``trade_rules`` only.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradingagents import trade_rules

DIRNAME = "proposals"
SCHEMA_VERSION = 1


def proposals_dir(results_dir: str | Path) -> Path:
    return Path(results_dir) / DIRNAME


def build(proposal: trade_rules.OrderProposal, *, run_id: str, trade_date: str | None, asset_type: str | None,
          source: str, max_notional: float | None = None, stale: bool | None = None) -> dict[str, Any]:
    side = proposal.side.value if proposal.side is not None else None
    try:
        order_id = trade_rules.client_order_id(run_id, proposal.side) if proposal.side is not None else None
    except ValueError:        # a caller-chosen run ID too long for Alpaca: the proposal exists, no order can
        order_id = None
    return {
        "schema": SCHEMA_VERSION,
        "proposal_id": trade_rules.proposal_id(run_id, proposal.side),
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": source,
        "ticker": proposal.ticker,
        "trade_date": trade_date,
        "asset_type": asset_type or "stock",
        "rating": proposal.rating,
        "side": side,
        "eligible": proposal.allowed,
        "reason": proposal.reason,
        "held_long_qty": float(proposal.held_long_qty or 0.0),
        "client_order_id": order_id,
        "max_notional": max_notional,
        "stale": stale,
    }


class ProposalStore:
    def __init__(self, results_dir: str | Path):
        self.folder = proposals_dir(results_dir)

    @classmethod
    def for_results_dir(cls, results_dir: str | Path) -> ProposalStore:
        return cls(results_dir)

    def _path(self, proposal_id: str) -> Path:
        if not trade_rules.is_valid_proposal_id(proposal_id):
            raise ValueError(f"{proposal_id!r} is not a proposal ID")
        return self.folder / f"{proposal_id}.json"

    def record(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Create the record once: (the stored record, True if this call created it). Raises OSError.

        Written to a temporary file and linked into place: the link fails if the record exists, so two
        writers cannot both create it and a reader never sees a partial file."""
        target = self._path(record["proposal_id"])
        if (existing := self.load(record["proposal_id"])) is not None:
            return existing, False
        self.folder.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.folder, prefix=f".{record['proposal_id']}.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(record, out, indent=1, sort_keys=True)
            try:
                os.link(temporary, target)
            except FileExistsError:
                return self.load(record["proposal_id"]) or record, False
            return record, True
        finally:
            with contextlib.suppress(OSError):
                os.unlink(temporary)

    def load(self, proposal_id: str) -> dict[str, Any] | None:
        path = self._path(proposal_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ValueError(f"the record of proposal {proposal_id} could not be read ({type(exc).__name__})") \
                from None
        return record if isinstance(record, dict) and record.get("proposal_id") == proposal_id else None

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        out = []
        for label in ("buy", "sell", trade_rules.PROPOSAL_NO_SIDE):
            try:
                record = self.load(f"ta-{run_id}-{label}")
            except ValueError:
                continue
            if record is not None:
                out.append(record)
        return out
