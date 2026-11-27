"""Append-only JSON Lines audit files and their record IDs (Phase 3 data model).

Shared by the approval-decision log (``execution.decision_log``), the paper-order ledger
(``execution.alpaca_paper.OrderLedger``) and the MCP request log (``mcp_audit``). Standard library plus the
cross-process file lock, so the MCP path can use it without reaching the execution layer.

- ``new_id(prefix)``: ``<prefix>-<32 hex>`` from uuid4, e.g. ``dec-4f0c...``; unique without coordination.
- ``append(path, entry)``: one line, written whole under ``<path>.lock`` (threads and processes), so
  concurrent writers can never interleave or truncate each other's lines. Raises OSError on failure: the
  caller decides whether a lost line must stop what it records (approvals do) or not (MCP requests).
- ``read(path)``: every readable line; a torn or invalid line is skipped, never fatal.
- ``legacy_id(prefix, line)``: a stable ID for a line written before IDs existed (a hash of the line), so old
  entries can be referred to without rewriting the file.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from tradingagents.dataflows.files import locked

ID_HEX = 32


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def legacy_id(prefix: str, line: str) -> str:
    return f"{prefix}-legacy-{hashlib.sha256(line.encode('utf-8')).hexdigest()[:ID_HEX]}"


def encode(entry: dict[str, Any]) -> bytes:
    return (json.dumps(entry, sort_keys=True, default=str) + "\n").encode("utf-8")


def append(path: str | Path, entry: dict[str, Any], *, lock: bool = True) -> None:
    """Append one entry as a single write. ``lock=False`` when the caller already holds ``locked(path)``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = encode(entry)
    if lock:
        with locked(path):
            _write(path, data)
    else:
        _write(path, data)


def _write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        written = os.write(fd, data)
        if written != len(data):
            raise OSError(f"short write to {path.name}: {written} of {len(data)} bytes")
    finally:
        os.close(fd)


def read_lines(path: str | Path) -> list[tuple[str, dict[str, Any]]]:
    """(raw line, entry) for every line that parses as a JSON object."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append((line, entry))
    return out


def read(path: str | Path) -> list[dict[str, Any]]:
    return [entry for _, entry in read_lines(path)]
