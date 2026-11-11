"""Opt-in Langfuse + local metrics from the upstream TradingAgents CLI.

E3_TRACE_ENABLED=1 enables this. Doesn't change E1 routing/agent behavior.
Run summary is saved on process exit even if an exception escapes the CLI.
"""
from __future__ import annotations

import atexit
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from tradingagents.e3_tracing import E3MetricsCallback


class E3Session:

    def __init__(self, ticker: str, analysis_date: str, callbacks=None, client=None):
        self.ticker, self.analysis_date = ticker, analysis_date
        self.callbacks = callbacks or []
        self.client = client
        self.began = time.perf_counter()
        self._saved = False
        self.status = "process-exit (completion not established)"
        self.decision = None


    @classmethod
    def from_env(cls, ticker: str, analysis_date: str):
        from dotenv import load_dotenv
        load_dotenv()

        if os.getenv("E3_TRACE_ENABLED", "").lower() not in {"1", "true", "yes"}:
            return cls(ticker, analysis_date)
        keys = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")
        missing = [key for key in keys if not os.environ.get(key)]
        if missing:
            raise RuntimeError("E3 enabled, but missing: " + ", ".join(missing))
        from langfuse import get_client
        from langfuse.langchain import CallbackHandler
        client = get_client()
        metrics = E3MetricsCallback()
        session = cls(ticker, analysis_date, [CallbackHandler(), metrics], client)
        atexit.register(session.save)
        return session


    def mark_completed(self, decision=None):
        self.status = "success"
        self.decision = decision



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
            summary = metrics.summary(
                ticker=self.ticker,
                analysis_date=self.analysis_date,
                status=self.status,
                decision=self.decision,
                wall_time_seconds=time.perf_counter() - self.began,
                pricing=pricing,
            )

            # Save E3 metrics
            target = Path(os.getenv("E3_RESULTS_DIR", "e3_results"))
            target.mkdir(parents=True, exist_ok=True)

            dest = target / (
                f"{self.ticker}_{self.analysis_date}_"
                f"{uuid4().hex[:8]}_e3.json"
            )

            dest.write_text(
                json.dumps(summary, indent=2),
                encoding="utf-8",
            )

            self._saved = True
            print(f"\nE3 metrics saved: {dest}")

            if self.client is not None:
                self.client.flush()

        except Exception as exc:
            print(
                f"E3 metrics could not be saved: "
                f"{type(exc).__name__}: {exc}"
            )
