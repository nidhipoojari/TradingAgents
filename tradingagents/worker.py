"""Phase 3: run an analysis in a separate process, so a cancelled run always releases its slot.

The shared service cancels cooperatively: no new step, model call or tool call
starts after a cancel, but a provider call already in flight cannot be
interrupted from Python (the agent nodes are synchronous, so neither asyncio
cancellation nor LangGraph can stop them), and the server's one analysis slot
stays taken until that call returns, which with a hung provider can take until
its request timeout (600 s by default for Gemini).

Here the analysis runs in a spawned child process. On cancel the parent first
asks the child to stop cooperatively (the usual path: the child finishes its
in-flight calls, saves its own E3 summary and exits). If it has not exited
within ``grace`` seconds, the parent terminates it: the OS reclaims its threads,
sockets and file locks, and the slot is released. Nothing in the server process
is touched by the cancelled run, so its state cannot be left half-updated.

Reliability:
- readiness: a worker reports "ready" (with its start-up time) once its imports
  are done and before it takes a request; the parent waits at most
  ``TRADINGAGENTS_WORKER_READY_TIMEOUT`` (default 60 s), replaces a worker that does
  not report, retries once, and logs every replacement;
- watchdog: during a run the child sends a heartbeat every second; a child silent for
  ``TRADINGAGENTS_WORKER_HEARTBEAT_TIMEOUT`` (default 30 s) is terminated;
- one warm worker is kept ready; a dead or unready spare is replaced on use.
Starting a worker imports code only: no model client is built and no API is
called until a request arrives.

What termination costs, and how it is recorded (E3):
- every model call's start and end is streamed to the parent as it happens, so
  completed calls keep their model, agent, latency and provider-reported tokens;
- a call in flight is abandoned, not undone: the provider may still bill it. It is
  recorded as incomplete (``complete: false``, no tokens: nothing is estimated);
- on cancel the child flushes Langfuse at once, within a bound, so the traces of
  completed calls are exported before any termination; the summary records
  whether that flush finished (``langfuse_flush``) and that the trace is incomplete.

Used by the dashboard and the MCP server. The CLI keeps the in-process service:
there Ctrl-C ends the whole process anyway.
``TRADINGAGENTS_ANALYSIS_WORKER=inline`` runs in-process instead (the test suite
does; worker tests use process mode with offline fakes installed in the child).

The child is started with the "spawn" method (forking a threaded server is
unsafe), which re-imports the parent's ``__main__`` module: a script that calls
``run_in_worker`` must keep its top-level code under ``if __name__ == "__main__":``.
The console scripts, ``python -m tradingagents.mcp_server`` and Streamlit's
runner all do.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import multiprocessing as mp
import os
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from tradingagents import service
from tradingagents.e3_cli_hook import bounded_flush

logger = logging.getLogger(__name__)

WORKER_ENV = "TRADINGAGENTS_ANALYSIS_WORKER"
GRACE_ENV = "TRADINGAGENTS_CANCEL_GRACE_SECONDS"
READY_ENV = "TRADINGAGENTS_WORKER_READY_TIMEOUT"
HEARTBEAT_ENV = "TRADINGAGENTS_WORKER_HEARTBEAT_TIMEOUT"
DEFAULT_GRACE_SECONDS = 10.0
DEFAULT_READY_SECONDS = 60.0
DEFAULT_HEARTBEAT_SECONDS = 30.0
HEARTBEAT_INTERVAL = 1.0
_CTX = mp.get_context("spawn")      # never fork a threaded server process
_SHUTTING_DOWN = threading.Event()


class WorkerStartupError(RuntimeError):
    """No analysis worker became ready in time."""


class WorkerUnresponsive(RuntimeError):
    """A worker stopped sending heartbeats during a run and was terminated."""


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def worker_mode() -> str:
    return "inline" if (os.environ.get(WORKER_ENV) or "process").strip().lower() == "inline" else "process"


def grace_seconds() -> float:
    return _env_seconds(GRACE_ENV, DEFAULT_GRACE_SECONDS)


@dataclass(frozen=True)
class PortableMessage:
    """What crosses the process boundary for an agent message: its kind, text and tool names."""

    kind: str
    content: str
    tool_calls: tuple = ()


def _portable_messages(messages) -> tuple:
    out = []
    for message in messages or ():
        content = getattr(message, "content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
        calls = tuple({"name": c.get("name") if isinstance(c, dict) else getattr(c, "name", "")}
                      for c in getattr(message, "tool_calls", None) or ())
        out.append(PortableMessage(type(message).__name__.removesuffix("Message"), str(content or "")[:2000], calls))
    return tuple(out)


# ------------------------------------------------------------------ the child
def _child(conn, setup: str | None) -> None:
    """Child process: import everything, report ready, take one request, run it, report back."""
    send_lock = threading.Lock()

    def send(*payload):
        with send_lock, contextlib.suppress(BrokenPipeError, OSError):
            conn.send(payload)

    began = time.perf_counter()
    try:
        if setup:                               # tests: install offline fakes inside the child
            module, _, function = setup.partition(":")
            import importlib

            getattr(importlib.import_module(module), function)()
        from langchain_core.callbacks import BaseCallbackHandler

        from tradingagents.graph import trading_graph  # noqa: F401 - warm the imports; builds no model
    except BaseException as exc:  # noqa: BLE001
        send("startup_error", f"{type(exc).__name__}: {exc}"[:500])
        return
    send("ready", os.getpid(), round(time.perf_counter() - began, 3))

    try:
        message = conn.recv()
    except (EOFError, OSError):             # a warm worker closed before it got a request
        return
    if message[0] != "run":
        return
    request, environment = message[1], message[2]
    # A warm worker was spawned before this request: adopt the parent's environment as
    # it is now (E3/Langfuse switches, keys, paths), so it matches a freshly spawned one.
    os.environ.clear()
    os.environ.update(environment)
    cancel = threading.Event()
    finished = threading.Event()

    def heartbeat():
        while not finished.wait(HEARTBEAT_INTERVAL):
            send("heartbeat")

    def flush_now(timeout: float):
        """On cancel: export the traces of the calls completed so far, before any termination."""
        if (os.environ.get("E3_TRACE_ENABLED") or "").lower() not in {"1", "true", "yes"}:
            return
        try:
            from langfuse import get_client

            status = bounded_flush(get_client(), timeout)
        except Exception as exc:  # noqa: BLE001
            status = f"failed: {type(exc).__name__}"
        send("flushed", status)

    def listen():
        while True:
            try:
                incoming = conn.recv()
            except (EOFError, OSError):
                return
            if incoming and incoming[0] == "cancel":
                cancel.set()
                grace = incoming[1] if len(incoming) > 1 else DEFAULT_GRACE_SECONDS
                threading.Thread(target=flush_now, args=(max(0.5, grace * 0.5),), daemon=True).start()

    threading.Thread(target=listen, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()

    class CallRecorder(BaseCallbackHandler):
        """Streams each model call to the parent, so a terminated run can still be accounted for."""

        def _start(self, run_id, metadata):
            meta = metadata or {}
            send("call_start", str(run_id), meta.get("langgraph_node") or "unattributed",
                 meta.get("ls_model_name") or "unknown", time.time())

        def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
            self._start(run_id, metadata)

        def on_llm_start(self, serialized, prompts, *, run_id, metadata=None, **kwargs):
            self._start(run_id, metadata)

        def on_llm_end(self, response, *, run_id, **kwargs):
            from tradingagents.e3_tracing import _tokens

            tokens_in, tokens_out = _tokens(response)
            send("call_end", str(run_id), time.time(), tokens_in, tokens_out, None)

        def on_llm_error(self, error, *, run_id, **kwargs):
            send("call_end", str(run_id), time.time(), None, None, f"{type(error).__name__}: {error}"[:300])

    def on_event(event):
        if event.kind == "budget":          # the per-run limit tripped: the parent starts its grace timer
            send("budget", dict(event.state or {}))
            return
        send("event", event.kind, event.progress, event.resuming, _portable_messages(event.messages))

    try:
        result = service.run_analysis(request, on_event=on_event, cancel=cancel, extra_callbacks=[CallRecorder()])
        state = {k: v for k, v in result.final_state.items() if k != "messages"}
        send("result", replace(result, final_state=state, graph=None, e3=None))
    except service.AnalysisCancelled as exc:
        send("cancelled", {"stage": exc.stage, "in_flight": exc.in_flight_model_calls,
                           "prevented": exc.prevented_calls, "released_after": exc.released_after_seconds,
                           "message": str(exc), "budget": getattr(exc, "budget", None)})
    except BaseException as exc:  # noqa: BLE001 - reported to the parent, which re-raises it
        # With what the run recorded before failing (service._record_failure), for the parent's caller.
        send("error", type(exc).__name__, service.sanitize_error(str(exc), 2000),
             getattr(exc, "analysis_failure", None))
    finally:
        finished.set()
        with contextlib.suppress(OSError):
            conn.close()


# ------------------------------------------------------------------ the parent
class _Worker:
    def __init__(self, setup: str | None):
        self.conn, child_conn = _CTX.Pipe()
        self.started = time.monotonic()
        self.process = _CTX.Process(target=_child, args=(child_conn, setup), daemon=True,
                                    name="tradingagents-analysis")
        self.process.start()
        child_conn.close()
        self.setup = setup
        self.ready = False
        self.startup_seconds: float | None = None
        self.problem = ""

    def wait_ready(self, timeout: float) -> bool:
        """Wait for the child's "ready"; False (with ``problem`` set) if it fails or times out."""
        if self.ready:
            return True
        deadline = self.started + timeout
        while time.monotonic() < deadline:
            try:
                if self.conn.poll(0.05):
                    message = self.conn.recv()
                    if message[0] == "ready":
                        self.ready, self.startup_seconds = True, message[2]
                        return True
                    if message[0] == "startup_error":
                        self.problem = message[1]
                        return False
            except (EOFError, OSError):
                self.problem = f"exited during start-up (exit code {self.process.exitcode})"
                return False
            if not self.process.is_alive() and not self.conn.poll(0):
                self.problem = f"exited during start-up (exit code {self.process.exitcode})"
                return False
        self.problem = f"not ready after {timeout:g}s"
        return False

    def stop(self) -> None:
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(5)
        with contextlib.suppress(OSError):
            self.conn.close()


class WorkerPool:
    """Keeps one warm worker ready; each worker serves exactly one analysis."""

    def __init__(self, setup: str | None = None):
        self.setup = setup
        self._lock = threading.Lock()
        self._ready: _Worker | None = None
        self.replacements = 0
        self._closed = False

    def take(self, ready_timeout: float | None = None, attempts: int = 2) -> _Worker:
        """A worker that has reported ready; replaces unready ones, then gives up."""
        timeout = _env_seconds(READY_ENV, DEFAULT_READY_SECONDS) if ready_timeout is None else ready_timeout
        with self._lock:
            worker, self._ready = self._ready, None
        problems = []
        for _ in range(attempts):
            if worker is None or not worker.process.is_alive():
                if worker is not None:
                    logger.warning("analysis worker %s died while waiting as a spare (exit code %s); replacing it",
                                   worker.process.pid, worker.process.exitcode)
                    self.replacements += 1
                worker = _Worker(self.setup)
            # A spare had its own start-up time already; it gets the full timeout from now too.
            budget = timeout + max(0.0, time.monotonic() - worker.started)
            if worker.wait_ready(budget):
                return worker
            logger.warning("analysis worker %s failed to start: %s; replacing it", worker.process.pid, worker.problem)
            problems.append(worker.problem)
            self.replacements += 1
            worker.stop()
            worker = None
        raise WorkerStartupError("no analysis worker became ready: " + "; ".join(problems))

    def replenish(self) -> None:
        def start():
            if _SHUTTING_DOWN.is_set() or self._closed:
                return
            fresh = _Worker(self.setup)
            with self._lock:
                # A pool closed while this spare was starting must not keep it.
                if self._ready is None and not _SHUTTING_DOWN.is_set() and not self._closed:
                    self._ready = fresh
                    return
            fresh.stop()
        if not _SHUTTING_DOWN.is_set() and not self._closed:
            threading.Thread(target=start, daemon=True, name="tradingagents-worker-prestart").start()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            worker, self._ready = self._ready, None
        if worker is not None:
            worker.stop()


_POOLS: dict[str | None, WorkerPool] = {}


@atexit.register
def _shutdown() -> None:
    """No new workers once the interpreter is exiting; stop the warm ones."""
    _SHUTTING_DOWN.set()
    for pool in list(_POOLS.values()):
        pool.close()


def pool_for(setup: str | None = None) -> WorkerPool:
    if setup not in _POOLS:
        _POOLS[setup] = WorkerPool(setup)
    return _POOLS[setup]


def _reap(worker: _Worker) -> None:
    """Let a finished child exit on its own; stop it if teardown takes unreasonably long."""
    worker.process.join(30)
    if worker.process.is_alive():
        worker.stop()
    with contextlib.suppress(OSError):
        worker.conn.close()


@dataclass
class _Tally:
    calls: dict = field(default_factory=dict)       # run_id -> record, in start order

    def start(self, run_id, agent, model, at):
        self.calls[run_id] = {"agent": agent, "model": model, "start": at, "end": None,
                              "input_tokens": None, "output_tokens": None, "error": None}

    def end(self, run_id, at, tokens_in, tokens_out, error):
        record = self.calls.get(run_id)
        if record is not None:
            record.update(end=at, input_tokens=tokens_in, output_tokens=tokens_out, error=error)

    @property
    def in_flight(self) -> list[dict]:
        return [r for r in self.calls.values() if r["end"] is None]


def _write_partial_usage(request, tally: _Tally, began: float, reason: str, e3_path: Path | None,
                         budget_info: dict | None) -> tuple[dict | None, Path | None]:
    """token_usage.json for a run whose worker failed, from the calls the child streamed.

    Only what the parent saw: completed calls with the tokens they reported; calls still running
    have none. E2 counters lived in the child and are unknown. Never raises.
    """
    try:
        calls = list(tally.calls.values())
        finished = [r for r in calls if r["end"] is not None]
        tokens_in = sum(r["input_tokens"] or 0 for r in finished)
        tokens_out = sum(r["output_tokens"] or 0 for r in finished)
        summary = None
        if e3_path is not None:
            with contextlib.suppress(OSError, ValueError):
                summary = json.loads(Path(e3_path).read_text(encoding="utf-8"))
        cost = service.e3_cost(summary)
        usage = {
            "llm_calls": len(calls), "tool_calls": None, "tokens_in": tokens_in, "tokens_out": tokens_out,
            "tokens_total": tokens_in + tokens_out, "elapsed_seconds": round(time.time() - began, 1),
            "run_id": request.run_id, "source": request.source, "status": "failed",
            "error": service.sanitize_error(reason),
            "llm_calls_failed": sum(1 for r in finished if r["error"]),
            "llm_calls_succeeded": sum(1 for r in finished if not r["error"]),
            "llm_calls_in_flight": len(calls) - len(finished),
            "tokens_note": ("from the calls the worker reported before it failed; calls in flight reported "
                            "none; tool calls and E2 counters were not reported"),
            "e2_cache": None,
            "e3_cost": {"estimated_cost_usd": cost["estimated_cost_usd"], "basis": cost["basis"],
                        "partial_cost_usd": cost["partial_cost_usd"], "note": cost["note"]},
        }
        if budget_info:
            usage["run_budget"] = budget_info
        if e3_path is not None:
            usage["e3_summary_path"] = str(e3_path)
        config = request.config if request.config is not None else service.config_for_source(request.source)
        target = service.run_directory(config, request.ticker, request.trade_date)
        target.mkdir(parents=True, exist_ok=True)
        path = target / "token_usage.json"
        path.write_text(json.dumps(usage, indent=2), encoding="utf-8")
        return usage, path
    except Exception:  # noqa: BLE001
        logger.warning("could not record the failed run %s", request.run_id, exc_info=True)
        return None, None


def _write_terminated_e3(request, tally: _Tally, began: float, reason: str, status: str,
                         flush_status: str | None) -> Path | None:
    """The E3 summary of a terminated run, from the call records the child streamed.

    Completed calls keep their reported tokens. Calls in flight have no end, no
    tokens and are marked ``complete: false``; nothing is estimated for them.
    """
    if (os.environ.get("E3_TRACE_ENABLED") or "").lower() not in {"1", "true", "yes"}:
        return None
    from tradingagents.e3_tracing import E3MetricsCallback

    callback = E3MetricsCallback()
    now = time.time()
    first = min((r["start"] for r in tally.calls.values()), default=now)
    records = list(tally.calls.items())
    callback.records = [{
        "run_id": run_id, "agent": r["agent"], "model": r["model"],
        "start_offset_ms": round((r["start"] - first) * 1000, 2),
        "latency_ms": round(((r["end"] or now) - r["start"]) * 1000, 2),
        "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
        "error": r["error"] or ("in flight when the worker was terminated" if r["end"] is None else None),
    } for run_id, r in records]
    pricing = None
    if os.environ.get("E3_PRICING_JSON"):
        try:
            pricing = json.loads(Path(os.environ["E3_PRICING_JSON"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pricing = None
    summary = callback.summary(ticker=request.ticker, analysis_date=request.trade_date, status=status,
                               wall_time_seconds=now - began, run_error=reason, pricing=pricing)
    for entry, (_, r) in zip(summary["per_call"], records, strict=True):
        entry["complete"] = r["end"] is not None
    in_flight = tally.in_flight
    summary.update(
        run_id=request.run_id, source=request.source, terminated=True,
        trace_complete=False, incomplete_calls=len(in_flight),
        in_flight_at_termination=[{"agent": r["agent"], "model": r["model"]} for r in in_flight],
        langfuse_flush=flush_status or "not attempted",
        langfuse_flushed=flush_status == "ok",
        billing_note=("calls in flight when the worker was terminated were abandoned, not undone: the provider "
                      "may still bill them; their tokens are unknown and not estimated"),
    )
    target = Path(os.getenv("E3_RESULTS_DIR", "e3_results"))
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{request.ticker}_{request.trade_date}_{request.run_id}_e3.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return path


def run_in_worker(request: service.AnalysisRequest, *, on_event=None, cancel: threading.Event | None = None,
                  grace: float | None = None, setup: str | None = None, ready_timeout: float | None = None,
                  heartbeat_timeout: float | None = None, record_calls: list | None = None,
                  **_ignored) -> service.AnalysisResult:
    """Run one analysis in a child process; same contract as ``service.run_analysis``.

    Raises AnalysisBusyError when a run holds this process's slot, AnalysisCancelled
    (``terminated`` set when the child had to be stopped), WorkerStartupError when no
    worker became ready, WorkerUnresponsive when one stopped sending heartbeats, and
    other failures as RuntimeError with the child's error type and message.
    """
    if worker_mode() == "inline" and setup is None:
        return service.run_analysis(request, on_event=on_event, cancel=cancel)
    if not service._RUN_LOCK.acquire(blocking=False):
        raise service.AnalysisBusyError("another analysis is running in this process; try again when it finishes")
    grace = grace_seconds() if grace is None else grace
    silence_limit = _env_seconds(HEARTBEAT_ENV, DEFAULT_HEARTBEAT_SECONDS) if heartbeat_timeout is None \
        else heartbeat_timeout
    # Resolve the configuration here, in the server: the child must run with the settings the
    # request was made under, not with whatever its own defaults resolve to.
    request = replace(request, run_id=request.run_id or service.new_run_id(),
                      config=request.config if request.config is not None
                      else service.config_for_source(request.source))
    pool = pool_for(setup)
    tally, began = _Tally(), time.time()
    cancel_sent_at = None
    flush_status = None
    budget_info = None              # set when the child reports that the run's limit tripped
    outcome = None
    worker = None
    try:
        worker = pool.take(ready_timeout)
        worker.conn.send(("run", request, dict(os.environ)))
        last_heard = time.monotonic()
        while outcome is None:
            if cancel is not None and cancel.is_set() and cancel_sent_at is None:
                cancel_sent_at = time.monotonic()
                with contextlib.suppress(BrokenPipeError, OSError):
                    worker.conn.send(("cancel", grace))
            if cancel_sent_at is not None and time.monotonic() - cancel_sent_at > grace:
                worker.stop()
                outcome = ("terminated",)
                break
            if time.monotonic() - last_heard > silence_limit:
                logger.warning("analysis worker %s sent nothing for %.0fs; terminating it",
                               worker.process.pid, silence_limit)
                worker.stop()
                outcome = ("unresponsive",)
                break
            try:
                ready = worker.conn.poll(0.05)
            except (EOFError, OSError):
                ready = False
            if not ready:
                if not worker.process.is_alive():
                    try:
                        if worker.conn.poll(0):
                            continue
                    except (EOFError, OSError):
                        pass
                    outcome = ("died", worker.process.exitcode)
                continue
            try:
                message = worker.conn.recv()
            except (EOFError, OSError):
                # The pipe is closed and every message has been read. A finished child sends its outcome
                # before closing it, so a child that is gone now died mid-run: say so at once, instead of
                # spinning on the closed pipe until the heartbeat watchdog misreports it as unresponsive.
                worker.process.join(2)
                if not worker.process.is_alive():
                    outcome = ("died", worker.process.exitcode)
                continue
            last_heard = time.monotonic()
            kind = message[0]
            if kind == "call_start":
                tally.start(*message[1:])
            elif kind == "call_end":
                tally.end(*message[1:])
            elif kind == "flushed":
                flush_status = message[1]
            elif kind == "budget":
                # The child is stopping itself; if a model call hangs, terminate it after the grace
                # period exactly as for a cancel by the person.
                budget_info = message[1]
                if cancel_sent_at is None:
                    cancel_sent_at = time.monotonic()
                    with contextlib.suppress(BrokenPipeError, OSError):
                        worker.conn.send(("cancel", grace))
            elif kind == "event" and on_event is not None:
                _, event_kind, progress, resuming, messages = message
                on_event(service.AnalysisEvent(request.run_id, event_kind, messages, None, resuming, progress))
            elif kind in ("result", "cancelled", "error"):
                outcome = message
    finally:
        if worker is not None and outcome is not None and outcome[0] in ("result", "cancelled", "error"):
            # The child has finished its work and reported (E3 saved before the result is sent);
            # what remains is interpreter teardown (~1 s), so it exits in the background.
            threading.Thread(target=_reap, args=(worker,), daemon=True, name="tradingagents-worker-reap").start()
        elif worker is not None and (outcome is None or outcome[0] not in ("terminated", "unresponsive")):
            worker.process.join(5)
            if worker.process.is_alive():
                worker.stop()
        service._RUN_LOCK.release()
        pool.replenish()
        if record_calls is not None:        # tests: the parent's view of every model call
            record_calls.extend(dict(r) for r in tally.calls.values())

    kind = outcome[0]
    if kind == "result":
        return outcome[1]
    if kind == "cancelled":
        info = outcome[1]
        if info.get("budget"):
            exc = service.AnalysisBudgetExceeded(request.run_id, info["stage"], info["in_flight"],
                                                 info["prevented"], budget=info["budget"])
        else:
            exc = service.AnalysisCancelled(request.run_id, info["stage"], info["in_flight"], info["prevented"])
        exc.released_after_seconds = info["released_after"]
        exc.args = (info["message"],)
        exc.terminated = False
        raise exc
    if kind in ("terminated", "unresponsive"):
        in_flight = len(tally.in_flight)
        if kind == "terminated":
            waited = round(time.monotonic() - cancel_sent_at, 3)
            stopped = (f"was stopped by its per-run limit ({budget_info.get('reason')})" if budget_info
                       else "was cancelled")
            reason = (f"analysis {request.run_id} {stopped}; the worker did not stop within {grace:g}s "
                      f"and was terminated ({in_flight} model call(s) in flight were abandoned, not undone: "
                      "the provider may still bill them)")
        else:
            reason = (f"analysis {request.run_id} failed: the worker sent no heartbeat for {silence_limit:g}s "
                      f"and was terminated ({in_flight} model call(s) in flight were abandoned, not undone: "
                      "the provider may still bill them)")
        status = ("budget_exceeded" if budget_info else "cancelled") if kind == "terminated" else "failed"
        e3_path = _write_terminated_e3(request, tally, began, reason, status, flush_status)
        if kind == "unresponsive":
            error = WorkerUnresponsive(reason)
            error.e3_summary_path = e3_path
            error.analysis_failure, error.usage_path = _write_partial_usage(
                request, tally, began, reason, e3_path, budget_info)
            raise error
        if budget_info:
            exc = service.AnalysisBudgetExceeded(request.run_id, "by terminating the worker", in_flight, 0,
                                                 budget=budget_info)
        else:
            exc = service.AnalysisCancelled(request.run_id, "by terminating the worker", in_flight, 0)
        exc.released_after_seconds = waited
        exc.args = (reason,)
        exc.terminated = True
        exc.e3_summary_path = e3_path
        exc.langfuse_flush = flush_status or "not attempted"
        raise exc
    if kind == "error":
        _, name, text, *rest = outcome
        if name == "AnalysisBusyError":
            raise service.AnalysisBusyError(text)
        error = ValueError(text) if name in ("ValueError",) else RuntimeError(f"{name}: {text}")
        partial = rest[0] if rest else None
        if partial:                          # written by the child (service._record_failure)
            error.analysis_failure = partial
            error.e3_summary_path = Path(partial["e3_summary_path"]) if partial.get("e3_summary_path") else None
            error.usage_path = service.run_directory(request.config, request.ticker, request.trade_date) \
                / "token_usage.json"
        raise error
    reason = f"analysis {request.run_id} failed: the analysis worker exited unexpectedly (exit code {outcome[1]})"
    e3_path = _write_terminated_e3(request, tally, began, reason, "failed", flush_status)
    error = RuntimeError(reason)
    error.e3_summary_path = e3_path
    error.analysis_failure, error.usage_path = _write_partial_usage(request, tally, began, reason, e3_path,
                                                                    budget_info)
    raise error


def analysis_runner():
    """The runner the dashboard and MCP server use: a worker process, unless configured inline."""
    return run_in_worker if worker_mode() == "process" else service.run_analysis
