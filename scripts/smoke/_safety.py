"""Shared by the live smoke scripts: keep secrets out of anything printed or saved."""

from __future__ import annotations

import os
from pathlib import Path

SECRET_ENVS = ("GOOGLE_API_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY", "ALPACA_API_KEY",
               "ALPACA_SECRET_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "TRADINGAGENTS_MCP_TOKEN")


def redact(text) -> str:
    """``text`` with the value of every secret environment variable replaced by ***."""
    text = str(text)
    for name in SECRET_ENVS:
        value = (os.environ.get(name) or "").strip()
        if len(value) >= 6:
            text = text.replace(value, "***")
    return text


def mask(value: str, keep: int = 4) -> str:
    value = str(value or "")
    return ("*" * max(len(value) - keep, 0)) + value[-keep:] if value else ""


def provenance(script: str, argv: list[str]) -> dict:
    """Who/what/when of a smoke run, for its report: start time, command (redacted), git branch and commit."""
    import shlex
    import subprocess
    import sys
    from datetime import UTC, datetime

    root = Path(__file__).resolve().parents[2]

    def git(*args):
        try:
            return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    now = datetime.now().astimezone()
    return {
        "started_at_local": now.isoformat(timespec="seconds"),
        "started_at_utc": now.astimezone(UTC).isoformat(timespec="seconds"),
        "command": redact(shlex.join([Path(sys.executable).name, f"scripts/smoke/{script}", *argv])),
        "git_branch": git("branch", "--show-current"),
        "git_commit": git("rev-parse", "HEAD"),
        "git_uncommitted_changes": bool(git("status", "--porcelain", "--untracked-files=no")),
        "secrets_note": "credentials are read from the environment and never written here; details are redacted",
    }


def fresh_dir(path) -> Path:
    """``path``, or ``path_2``, ``path_3``... if it exists: a run never writes into an earlier run's folder."""
    path, n = Path(path), 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.name}_{n}")
        n += 1
    return candidate
