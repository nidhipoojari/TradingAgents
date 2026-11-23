"""Opt-in Langfuse + local metrics from the upstream TradingAgents CLI.

E3_TRACE_ENABLED=1 enables this. Doesn't change E1 routing/agent behavior.
Run summary is saved on process exit even if an exception escapes the CLI.

Phase 3 (shared analysis service) additions, all optional:
- ``run_id`` / ``source`` label the summary (and its file name) so a run from the
  CLI, the dashboard or the MCP server can be matched to its token_usage.json,
  its Langfuse session and its paper-order audit line.
- ``mark_failed()`` records a failed run's error in the summary.
- An explicit ``save()`` unregisters the exit hook, so a long-lived process
  (dashboard, MCP server) does not pile up one hook per run or save twice.
- Status lines go to stderr: under the MCP stdio transport stdout carries the protocol.
- The Langfuse flush is bounded (``E3_FLUSH_TIMEOUT_SECONDS``, default 10) and runs
  before the summary is written, which records its outcome (``langfuse_flush``), so a
  slow or unreachable Langfuse cannot stall a run's shutdown.
- ``mark_analysis_started/finished()`` split the wall time into
  ``pre_analysis_seconds`` + ``analysis_seconds`` + ``post_analysis_seconds``
  (new summary fields; existing ones keep their names and meaning). See
  TIMING_BOUNDARIES and PHASE3_CHANGES.md for the exact boundaries.
"""
from __future__ import annotations

import atexit
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

from tradingagents.e3_tracing import E3MetricsCallback

FLUSH_TIMEOUT_ENV = "E3_FLUSH_TIMEOUT_SECONDS"


def bounded_flush(client, timeout: float) -> str:
    """Flush a Langfuse client, waiting at most ``timeout`` seconds: "ok", "timed_out" or "failed: ..."."""
    import threading

    outcome = {}

    def run():
        try:
            client.flush()
            outcome["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            outcome["status"] = f"failed: {type(exc).__name__}"

    thread = threading.Thread(target=run, daemon=True, name="tradingagents-langfuse-flush")
    thread.start()
    thread.join(timeout)
    return outcome.get("status", "timed_out")


def flush_timeout() -> float:
    try:
        value = float(os.environ.get(FLUSH_TIMEOUT_ENV) or 10)
    except ValueError:
        return 10.0
    return value if value > 0 else 10.0

# Written into every summary so a reader can tell what each number spans.
TIMING_BOUNDARIES = {
    "wall_time_seconds": "E3 session start (after the run's setup questions, before the graph is built) "
                         "to the summary save: after the report prompts for the CLI, as in Phase 2; at "
                         "the end of the analysis for the dashboard and MCP, which have no prompts",
    "pre_analysis_seconds": "E3 session start to the start of graph construction",
    "analysis_seconds": "graph construction start to the decision recorded and the checkpoint closed",
    "post_analysis_seconds": "analysis end to the summary save (CLI: report prompts and saving)",
}


class E3Session:

    def __init__(self, ticker: str, analysis_date: str, callbacks=None, client=None,
                 run_id: str | None = None, source: str | None = None):
        self.ticker, self.analysis_date = ticker, analysis_date
        self.callbacks = callbacks or []
        self.client = client
        self.run_id, self.source = run_id, source
        self.began = time.perf_counter()
        self._saved = False
        self.status = "process-exit (completion not established)"
        self.decision = None
        self.run_error = None
        self.saved_path: Path | None = None
        self.analysis_started: float | None = None
        self.analysis_finished: float | None = None


    @classmethod
    def from_env(cls, ticker: str, analysis_date: str, run_id: str | None = None,
                 source: str | None = None):
        from dotenv import load_dotenv
        load_dotenv()

        if os.getenv("E3_TRACE_ENABLED", "").lower() not in {"1", "true", "yes"}:
            return cls(ticker, analysis_date, run_id=run_id, source=source)
        keys = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")
        missing = [key for key in keys if not os.environ.get(key)]
        if missing:
            raise RuntimeError("E3 enabled, but missing: " + ", ".join(missing))
        from langfuse import get_client
        from langfuse.langchain import CallbackHandler
        client = get_client()
        metrics = E3MetricsCallback()
        session = cls(ticker, analysis_date, [CallbackHandler(), metrics], client,
                      run_id=run_id, source=source)
        atexit.register(session.save)
        return session


    def mark_completed(self, decision=None):
        self.status = "success"
        self.decision = decision

    def mark_analysis_started(self):
        self.analysis_started = time.perf_counter()

    def mark_analysis_finished(self):
        self.analysis_finished = time.perf_counter()

    def mark_cancelled(self, reason: str):
        """Phase 3: a run stopped on request; not counted as a failure."""
        self.status = "cancelled"
        self.run_error = reason[:500]

    def mark_failed(self, error: BaseException | str):
        """Phase 3: record a run that raised, so its summary says why."""
        self.status = "failed"
        text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        self.run_error = text[:500]


    def _phases(self, ended: float) -> dict:
        """Phase 3: the wall time split at the analysis boundaries (None where not marked)."""
        start, finish = self.analysis_started, self.analysis_finished
        end_of_analysis = finish if finish is not None else (ended if start is not None else None)

        def span(a, b):
            return round(b - a, 3) if a is not None and b is not None else None

        return {
            "pre_analysis_seconds": span(self.began, start),
            "analysis_seconds": span(start, end_of_analysis),
            "post_analysis_seconds": span(finish, ended),
            "timing_boundaries": TIMING_BOUNDARIES,
        }

    def save(self):
        if self._saved or not self.callbacks:
            return

        try:
            metrics = next(
                x for x in self.callbacks
                if isinstance(x, E3MetricsCallback)
            )

            # Load optional pricing configuration
            pricing = None
            pricing_path = os.getenv("E3_PRICING_JSON")

            if pricing_path:
                pricing = json.loads(
                    Path(pricing_path).read_text(encoding="utf-8")
                )

            # Build the E3 summary
            ended = time.perf_counter()
            summary = metrics.summary(
                ticker=self.ticker,
                analysis_date=self.analysis_date,
                status=self.status,
                decision=self.decision,
                wall_time_seconds=ended - self.began,
                run_error=self.run_error,
                pricing=pricing,
            )
            summary.update(self._phases(ended))
            if self.run_id:
                summary["run_id"] = self.run_id
            if self.source:
                summary["source"] = self.source
            # Langfuse first, bounded, so the summary can say whether the traces went out.
            if self.client is not None:
                summary["langfuse_flush"] = bounded_flush(self.client, flush_timeout())

            # Save E3 metrics
            target = Path(os.getenv("E3_RESULTS_DIR", "e3_results"))
            target.mkdir(parents=True, exist_ok=True)

            dest = target / (
                f"{self.ticker}_{self.analysis_date}_"
                f"{self.run_id or uuid4().hex[:8]}_e3.json"
            )

            dest.write_text(
                json.dumps(summary, indent=2),
                encoding="utf-8",
            )

            self._saved = True
            self.saved_path = dest
            # Saved now; the exit hook has nothing left to do.
            atexit.unregister(self.save)
            print(f"\nE3 metrics saved: {dest}", file=sys.stderr)

        except Exception as exc:
            print(
                f"E3 metrics could not be saved: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
