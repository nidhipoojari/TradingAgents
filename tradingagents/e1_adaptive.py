"""E1 adaptive routing (Phase 3, opt-in): start a tier on its backup when recent E3 records show its main
model failing, and say why.

Off by default: ``e1_adaptive_routing`` (``TRADINGAGENTS_E1_ADAPTIVE=true``) turns it on. Then, before a run,
each tier that has a backup gets one decision from the main model's recent E3 calls (``per_call`` in the
summaries under ``E3_RESULTS_DIR``, newest first, at most ``window_calls`` within ``window_hours``):

- **prefer_backup** when at least ``min_calls`` calls are known and the share that failed with an overload
  (429, 5xx: errors that a different model can avoid) is at least ``demote_error_rate`` (default 50 %).
- **Hysteresis**: once demoted, the tier stays on its backup until the main model's error rate is at or
  below ``recover_error_rate`` (default 20 %). A demoted model gets no new calls, so after ``probe_minutes``
  (default 30) the main model is tried again (a probe); the run's circuit breaker still catches it if it fails.
- **Latency and cost are reported, never acted on**: the backup is normally the weaker model, so switching to
  it to save time or money would trade analysis quality for it. Only failures move a tier.

Every decision (including "keep") is appended with its reason and figures to
``<results_dir>/e1_adaptive_log.jsonl`` and put in the run's record (``e1_adaptive``). The routing table
itself never changes; a demotion applies to one run, through that run's circuit breaker.
"""

from __future__ import annotations

import json
import os
import statistics
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

OVERLOAD_CODES = {429, 500, 502, 503, 504}
LOG_NAME = "e1_adaptive_log.jsonl"


@dataclass(frozen=True)
class Thresholds:
    window_calls: int = 50
    window_hours: float = 6.0
    min_calls: int = 10
    demote_error_rate: float = 0.5
    recover_error_rate: float = 0.2
    probe_minutes: float = 30.0

    def __post_init__(self):
        if not 0 <= self.recover_error_rate < self.demote_error_rate <= 1:
            raise ValueError("need 0 <= recover_error_rate < demote_error_rate <= 1 (hysteresis)")
        if self.min_calls < 1 or self.window_calls < self.min_calls:
            raise ValueError("need 1 <= min_calls <= window_calls")


@dataclass
class Decision:
    tier: str
    main_model: str
    backup_model: str
    action: str                       # keep | prefer_backup
    reason: str
    calls: int = 0
    overload_errors: int = 0
    error_rate: float | None = None
    median_latency_seconds: float | None = None
    decided_at_utc: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


def recent_calls(model: str, history_dir: Path, thresholds: Thresholds, now: datetime | None = None) -> list[dict]:
    """The model's calls in the E3 summaries under ``history_dir``, newest run first, within the window."""
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=thresholds.window_hours)
    runs = []
    for path in history_dir.glob("*_e3.json"):
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(summary["created_at_utc"])
        except (OSError, ValueError, KeyError, TypeError):
            continue                   # a partial or foreign file is not evidence
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if since <= created <= now:
            runs.append((created, summary.get("per_call") or []))
    calls: list[dict] = []
    for _created, per_call in sorted(runs, key=lambda r: r[0], reverse=True):
        mine = [c for c in reversed(per_call) if c.get("model") == model]
        calls.extend(mine)
        if len(calls) >= thresholds.window_calls:
            break
    return calls[: thresholds.window_calls]


def _last_decision(log: Path, tier: str, model: str) -> dict | None:
    if not log.is_file():
        return None
    last = None
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("tier") == tier and entry.get("main_model") == model:
            last = entry
    return last


def decide(config: dict, tier: str, history_dir: Path, thresholds: Thresholds | None = None,
           now: datetime | None = None) -> Decision | None:
    """This run's decision for ``tier``, or None when it has no backup (nothing to choose between)."""
    t = thresholds or Thresholds()
    main, backup = config.get(f"{tier}_think_llm"), config.get(f"{tier}_fallback_llm")
    if not main or not backup:
        return None
    now = now or datetime.now(UTC)
    calls = recent_calls(main, history_dir, t, now)
    errors = sum(1 for c in calls if c.get("had_error") and c.get("error_code") in OVERLOAD_CODES)
    latencies = [c["latency_ms"] / 1000 for c in calls if not c.get("had_error") and c.get("latency_ms")]
    rate = errors / len(calls) if calls else None
    base = {"tier": tier, "main_model": main, "backup_model": backup, "calls": len(calls),
            "overload_errors": errors, "error_rate": None if rate is None else round(rate, 3),
            "median_latency_seconds": round(statistics.median(latencies), 2) if latencies else None}
    previous = _last_decision(Path(config["results_dir"]) / LOG_NAME, tier, main)
    demoted = previous is not None and previous.get("action") == "prefer_backup"
    if demoted:
        since = datetime.fromisoformat(previous["decided_at_utc"])
        if rate is not None and len(calls) >= t.min_calls and rate <= t.recover_error_rate:
            return Decision(action="keep", reason=f"recovered: overload error rate {rate:.0%} <= "
                            f"{t.recover_error_rate:.0%} over {len(calls)} calls", **base)
        if now - since >= timedelta(minutes=t.probe_minutes):
            return Decision(action="keep", reason=f"probe: on the backup since {previous['decided_at_utc']} "
                            f"(>= {t.probe_minutes:g} min); trying the main model again (the circuit breaker "
                            "catches it if it still fails)", **base)
        return Decision(action="prefer_backup", reason=f"still demoted (since {previous['decided_at_utc']}): "
                        f"error rate {'unknown' if rate is None else f'{rate:.0%}'} has not fallen to "
                        f"{t.recover_error_rate:.0%}", **{**base, "decided_at_utc": previous["decided_at_utc"]})
    if rate is None or len(calls) < t.min_calls:
        return Decision(action="keep", reason=f"too little evidence: {len(calls)} recent calls "
                        f"(< {t.min_calls}); fixed routing", **base)
    if rate >= t.demote_error_rate:
        return Decision(action="prefer_backup", reason=f"overload error rate {rate:.0%} >= "
                        f"{t.demote_error_rate:.0%} over the last {len(calls)} calls", **base)
    return Decision(action="keep", reason=f"overload error rate {rate:.0%} < {t.demote_error_rate:.0%} over "
                    f"{len(calls)} calls; latency and cost are reported, not used to switch", **base)


def thresholds_from(config: dict) -> Thresholds:
    given = config.get("e1_adaptive_thresholds") or {}
    return Thresholds(**{k: v for k, v in given.items() if k in Thresholds.__dataclass_fields__})


def plan(config: dict, history_dir: Path | None = None, now: datetime | None = None) -> list[Decision]:
    """Decisions for both tiers when adaptive routing is on (else []); each is logged with its reason."""
    if not config.get("e1_adaptive_routing"):
        return []
    history = history_dir or Path(config.get("e1_adaptive_history_dir") or os.getenv("E3_RESULTS_DIR", "e3_results"))
    thresholds = thresholds_from(config)
    decisions = [d for d in (decide(config, tier, history, thresholds, now) for tier in ("quick", "deep")) if d]
    log = Path(config["results_dir"]) / LOG_NAME
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        for d in decisions:
            handle.write(json.dumps(asdict(d)) + "\n")
    return decisions
