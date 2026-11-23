"""F1: analyses run in a background thread, so the page stays usable while one runs.

Streamlit reruns the whole page script on every click. An analysis run inside
the script would be interrupted by the first click during its minutes of work;
here it runs in its own thread through the shared service, and the page only
reads the job's progress. The job object is the only thing the thread and the
page share, and it is guarded by a lock. No Streamlit import, so it is tested
without it.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from tradingagents import service

MAX_LOG_LINES = 200


@dataclass
class AnalysisJob:
    request: service.AnalysisRequest
    run_id: str
    status: str = "running"            # running | done | failed | cancelled | budget_exceeded
    progress: tuple[int, int, str] = (0, 0, "Starting")
    result: service.AnalysisResult | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    budget: dict | None = None         # the per-run limit's accounting when it stopped the run
    _log: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def log(self) -> list[str]:
        with self._lock:
            return list(self._log)

    def snapshot(self) -> dict:
        with self._lock:
            return {"status": self.status, "progress": self.progress, "error": self.error,
                    "elapsed": self.elapsed}

    def _event(self, event: service.AnalysisEvent) -> None:
        lines = []
        if event.kind == "checkpoint":
            lines.append("Resuming the saved run" if event.resuming else "Starting fresh")
        for message in event.messages:
            text = _text(getattr(message, "content", None))
            for call in getattr(message, "tool_calls", None) or []:
                name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
                lines.append(f"Tool call: {name}")
            if text:
                kind = getattr(message, "kind", None) or type(message).__name__.removesuffix("Message")
                lines.append(f"{kind}: {text[:400]}")
        if event.kind == "budget":
            from tradingagents.run_budget import describe_snapshot

            lines.append(describe_snapshot(event.state))
        with self._lock:
            if event.kind == "budget":
                self.budget = dict(event.state or {})
            if event.kind == "step":
                self.progress = event.progress
            self._log.extend(lines)

    def _finish(self, status: str, result=None, error: str | None = None) -> None:
        with self._lock:
            self.status, self.result, self.error = status, result, error
            self.finished_at = time.time()
            if result is not None:
                done = len(self.request.analysts) + len(service.DECISION_STEPS)
                self.progress = (done, done, "Done")


def _text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [c.get("text", "") if isinstance(c, dict) else str(c) for c in content]
        return " ".join(p for p in parts if p).strip()
    return ""


def start_job(request: service.AnalysisRequest, *,
              run: Callable[..., service.AnalysisResult] | None = None) -> AnalysisJob:
    """Start one analysis in a daemon thread; raises AnalysisBusyError when one is already running.

    ``run`` defaults to the analysis worker process (tradingagents.worker), so a
    cancelled run releases the dashboard's slot within the grace period even when
    a model call hangs.
    """
    if run is None:
        from tradingagents.worker import analysis_runner

        run = analysis_runner()
    if service.is_busy():
        raise service.AnalysisBusyError("another analysis is running; wait for it to finish")
    run_id = request.run_id or service.new_run_id()
    request = service.AnalysisRequest(**{**request.__dict__, "run_id": run_id})
    job = AnalysisJob(request=request, run_id=run_id)

    def work():
        try:
            result = run(request, on_event=job._event, cancel=job.cancel)
        except service.AnalysisBudgetExceeded as exc:
            # Stopped by the per-run limit: reported apart from a cancel by the person or a failure.
            job.budget = exc.budget or job.budget
            job._finish("budget_exceeded", error=str(exc))
        except service.AnalysisCancelled as exc:
            # The message says how many model calls were already running and not interrupted.
            job._finish("cancelled", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - shown to the person, not swallowed
            job._finish("failed", error=f"{type(exc).__name__}: {exc}")
        else:
            job._finish("done", result=result)

    threading.Thread(target=work, name=f"analysis-{run_id}", daemon=True).start()
    return job
