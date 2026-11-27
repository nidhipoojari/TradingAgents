"""Deadlines for interactive analyses (dashboard and MCP): the whole analysis, and each model step.

- ``analysis_seconds``: from the moment the request was made (so worker start-up counts) to the result.
- ``step_seconds``: one model step, i.e. a primary call and its E1 backup, including SDK retries and
  backoff. Primary and backup calls of one step share a LangChain parent run (``with_fallbacks``), which
  is how the step is recognised.
- ``target_seconds``: the performance objective (41 s, as Phase 1/2). Reported as met or missed; it never
  stops a run, so a valid result is never discarded to meet it.

Enforcement, in two layers:

1. In the process that makes the calls (``DeadlineGuard``): a model call is refused at its start once the
   analysis deadline has passed, or when its step started ``step_seconds`` ago (a backup after a slow
   primary). The refusal stops the run through the cancel path, as a deadline stop.
2. In the parent (tradingagents/worker.py), which nothing in the child can block: when the analysis
   deadline passes, or a model call has been in flight longer than its step allows, the parent cancels and
   terminates the worker after ``grace_seconds``. A result that arrives in that grace is still returned.

A deadline stop is never retried or restarted.

Clocks: every deadline is measured on the monotonic clock, so a wall-clock step (WSL2 stepped +185.7 s on
2026-11-25) neither ends a run early nor lets a hung one run on. ``started_at`` stays a wall-clock time for
records and display. The monotonic start does not cross processes: the parent sends the elapsed time with
the request (``for_handoff``) and the worker rebases it on its own clock (``adopted``).
"""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, replace
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

ANALYSIS_ENV = "TRADINGAGENTS_ANALYSIS_DEADLINE_SECONDS"
STEP_ENV = "TRADINGAGENTS_STEP_DEADLINE_SECONDS"
TARGET_ENV = "TRADINGAGENTS_DASHBOARD_TARGET_SECONDS"
GRACE_ENV = "TRADINGAGENTS_DEADLINE_GRACE_SECONDS"
DEFAULT_ANALYSIS_SECONDS = 180.0      # a market-only run takes ~25-45 s; a four-analyst run up to ~2 min
DEFAULT_STEP_SECONDS = 60.0           # > 2 x the slowest successful call seen (23.1 s) and its 30 s timeout
DEFAULT_TARGET_SECONDS = 41.0         # Phase 1 (34-43 s) and Phase 2 (37-41 s live) NVDA runs
DEFAULT_GRACE_SECONDS = 5.0
_OFF = {"0", "off", "none", "false", "no"}


@dataclass(frozen=True)
class RunDeadline:
    analysis_seconds: float | None = None
    step_seconds: float | None = None
    started_at: float | None = None          # wall clock, when the request was made (records); unset = now
    target_seconds: float | None = None
    grace_seconds: float = DEFAULT_GRACE_SECONDS
    # The same moment on this process's monotonic clock (what every deadline is measured on). Both unset:
    # read now, with no wall-clock arithmetic. Only a caller that gives a past started_at and no started_mono
    # gets it derived from started_at's age (a deadline "started 10 s ago" stays one).
    started_mono: float | None = None
    handoff_elapsed: float | None = None    # set only while the deadline travels to a worker (for_handoff)

    def __post_init__(self):
        for name in ("analysis_seconds", "step_seconds", "target_seconds"):
            value = getattr(self, name)
            if value is not None and not (value > 0 and math.isfinite(value)):
                raise ValueError(f"{name} must be a positive number of seconds, got {value!r}")
        if self.started_at is None:
            object.__setattr__(self, "started_at", time.time())
            if self.started_mono is None:
                object.__setattr__(self, "started_mono", time.monotonic())
        elif self.started_mono is None:
            age = max(0.0, time.time() - self.started_at)
            object.__setattr__(self, "started_mono", time.monotonic() - age)

    def elapsed(self) -> float:
        """Seconds since the request, on the monotonic clock."""
        return time.monotonic() - self.started_mono

    @property
    def analysis_at(self) -> float | None:
        """Wall-clock time the analysis deadline falls due (for display; enforcement uses elapsed())."""
        return self.started_at + self.analysis_seconds if self.analysis_seconds else None

    def remaining(self) -> float | None:
        return None if self.analysis_seconds is None else self.analysis_seconds - self.elapsed()

    def passed(self) -> bool:
        return self.analysis_seconds is not None and self.elapsed() >= self.analysis_seconds

    def for_handoff(self) -> RunDeadline:
        """This deadline as sent to another process: its elapsed time travels, not a monotonic reading."""
        return replace(self, handoff_elapsed=self.elapsed())

    def adopted(self) -> RunDeadline:
        """In the receiving process: the deadline rebased on this process's monotonic clock."""
        if self.handoff_elapsed is None:
            return self
        return replace(self, started_mono=time.monotonic() - self.handoff_elapsed, handoff_elapsed=None)

    def describe(self) -> str:
        parts = []
        if self.analysis_seconds:
            parts.append(f"deadline {self.analysis_seconds:g} s")
        if self.step_seconds:
            parts.append(f"{self.step_seconds:g} s per model step")
        if self.target_seconds:
            parts.append(f"target {self.target_seconds:g} s")
        return ", ".join(parts) or "no deadline"


def _seconds(name: str, default: float) -> float | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    if raw.lower() in _OFF:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number of seconds; use a number, or off") from None


def interactive_deadline(started_at: float | None = None, started_mono: float | None = None) -> RunDeadline:
    """The dashboard's and MCP server's deadline, from the environment (defaults above). The dashboard passes
    its click's wall-clock and monotonic times, so the deadline counts from the click."""
    return RunDeadline(analysis_seconds=_seconds(ANALYSIS_ENV, DEFAULT_ANALYSIS_SECONDS),
                       step_seconds=_seconds(STEP_ENV, DEFAULT_STEP_SECONDS),
                       target_seconds=_seconds(TARGET_ENV, DEFAULT_TARGET_SECONDS),
                       grace_seconds=_seconds(GRACE_ENV, DEFAULT_GRACE_SECONDS) or DEFAULT_GRACE_SECONDS,
                       started_at=started_at, started_mono=started_mono)


class DeadlineGuard(BaseCallbackHandler):
    """Refuses model calls past the analysis deadline or their step's deadline (see the module docstring)."""

    raise_error = True
    run_inline = True

    def __init__(self, deadline: RunDeadline, cancel: threading.Event, on_exhausted=None):
        super().__init__()
        self.deadline, self.cancel, self.on_exhausted = deadline, cancel, on_exhausted
        self._lock = threading.Lock()
        self._step_started: dict[str, float] = {}
        self.refused = 0
        self.exhausted: str | None = None
        self.which: str | None = None

    def trip(self, which: str, reason: str) -> None:
        with self._lock:
            if self.exhausted:
                return
            self.exhausted, self.which = reason, which
        self.cancel.set()
        if self.on_exhausted is not None:
            self.on_exhausted(self.snapshot())

    def check_analysis(self) -> bool:
        """Trips the analysis deadline if it has passed; True when the run must stop for a deadline."""
        if self.deadline.passed() and not self.exhausted:
            self.trip("analysis", f"analysis deadline reached: {self.deadline.elapsed():.1f} s of "
                                  f"{self.deadline.analysis_seconds:g} s")
        return self.exhausted is not None

    def _admit(self, run_id, parent_run_id) -> None:
        from tradingagents.service import _CancelSignal

        if self.cancel.is_set() and not self.exhausted:
            return                     # stopped for another reason; the cancel guard refuses the call
        now = time.monotonic()
        self.check_analysis()
        key = str(parent_run_id or run_id)
        with self._lock:
            first = self._step_started.setdefault(key, now)
        step = self.deadline.step_seconds
        if not self.exhausted and step is not None and now - first >= step:
            self.trip("step", f"model step deadline reached: {now - first:.1f} s of {step:g} s "
                              "(primary, fallback and retries together)")
        if self.exhausted:
            with self._lock:
                self.refused += 1
            raise _CancelSignal("before a model call")

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        self._admit(run_id, parent_run_id)

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs):
        self._admit(run_id, parent_run_id)

    def snapshot(self) -> dict[str, Any]:
        d = self.deadline
        return {"analysis_seconds": d.analysis_seconds, "step_seconds": d.step_seconds,
                "target_seconds": d.target_seconds, "elapsed_seconds": round(d.elapsed(), 1),
                "exhausted": self.exhausted is not None, "which": self.which, "reason": self.exhausted,
                "llm_calls_refused": self.refused}


def outcome(deadline: RunDeadline | None) -> dict[str, Any] | None:
    """For a completed run: how long it took from the request (monotonic), against the target and deadline."""
    if deadline is None:
        return None
    elapsed = deadline.elapsed()
    return {"elapsed_seconds": round(elapsed, 1), "target_seconds": deadline.target_seconds,
            "target_met": None if deadline.target_seconds is None else elapsed <= deadline.target_seconds,
            "analysis_seconds": deadline.analysis_seconds, "step_seconds": deadline.step_seconds}
