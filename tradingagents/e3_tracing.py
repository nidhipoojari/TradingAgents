"""E3: Langfuse tracing plus local, best-effort LLM metrics capture.

This module intentionally does not change model routing or retry/fallback behavior.
Statistics are grouped by LangGraph node only when the callbacks carry node metadata;
otherwise observations are marked 'unattributed'. Costs require a separately supplied
pricing model, so no dollar amounts are fabricated here.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

try:
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError as err:  # useful error for running outside of TradingAgents environment
    raise SystemExit("Install the TradingAgents dependencies (langchain-core), then retry.") from err


def _model_name(serialized: dict[str, Any] | None, metadata: dict[str, Any] | None) -> str:
    meta = metadata or {}
    for key in ("ls_model_name", "model_name", "model"):
        if meta.get(key):
            return str(meta[key])
    ser = serialized or {}
    kwargs = ser.get("kwargs", {}) if isinstance(ser, dict) else {}
    for key in ("model", "model_name"):
        if isinstance(kwargs, dict) and kwargs.get(key):
            return str(kwargs[key])
    return "unknown"


def error_code(error: BaseException | None) -> int | None:
    """The HTTP status of a provider error, when the error carries one (itself or its cause).

    google-genai errors and LangChain's Google wrappers carry ``code``; others may carry
    ``status_code``. None when no status is attached; nothing is guessed from the text.
    """
    seen = 0
    while error is not None and seen < 4:
        for attribute in ("code", "status_code"):
            value = getattr(error, attribute, None)
            if isinstance(value, int) and 100 <= value <= 599:
                return value
        error, seen = error.__cause__ or error.__context__, seen + 1
    return None


def _agent_name(metadata: dict[str, Any] | None) -> str:
    meta = metadata or {}
    for key in ("langgraph_node", "agent_name", "agent", "node"):
        if meta.get(key):
            return str(meta[key])
    return "unattributed"


def _tokens(result: Any) -> tuple[int | None, int | None]:
    """Read reported usage without inventing tokens when a provider omits it."""
    usage = getattr(result, "llm_output", None) or {}
    if isinstance(usage, dict):
        usage = usage.get("token_usage") or usage.get("usage") or {}
    if not usage:
        generations = getattr(result, "generations", []) or []
        for generation_group in generations:
            for generation in generation_group:
                message = getattr(generation, "message", None)
                info = getattr(message, "usage_metadata", None) if message else None
                if info:
                    usage = info
                    break
            if usage:
                break
    if not isinstance(usage, dict):
        return None, None
    inp = usage.get("input_tokens", usage.get("prompt_tokens"))
    out = usage.get("output_tokens", usage.get("completion_tokens"))
    return (int(inp) if inp is not None else None,
            int(out) if out is not None else None)


def _token_details(result: Any) -> tuple[int | None, int | None]:
    """Provider-reported cached input and reasoning tokens (Phase 3), or None when not reported.

    LangChain's standard ``usage_metadata`` carries them as ``input_token_details.cache_read`` and
    ``output_token_details.reasoning`` (Gemini: cached_content_token_count / thoughts_token_count, the
    latter already included in output_tokens); OpenAI's raw ``token_usage`` as ``prompt_tokens_details.
    cached_tokens`` / ``completion_tokens_details.reasoning_tokens``.
    """
    if result is None:
        return None, None
    cached = reasoning = None
    usage = getattr(result, "llm_output", None) or {}
    if isinstance(usage, dict):
        raw = usage.get("token_usage") or usage.get("usage") or {}
        if isinstance(raw, dict):
            cached = (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
            reasoning = (raw.get("completion_tokens_details") or {}).get("reasoning_tokens")
    for generation_group in getattr(result, "generations", []) or []:
        for generation in generation_group:
            info = getattr(getattr(generation, "message", None), "usage_metadata", None)
            if isinstance(info, dict):
                cached = cached if cached is not None else (info.get("input_token_details") or {}).get("cache_read")
                reasoning = (reasoning if reasoning is not None
                             else (info.get("output_token_details") or {}).get("reasoning"))
                break
        break
    return (int(cached) if cached is not None else None, int(reasoning) if reasoning is not None else None)


def _call_cost(event: dict, entry: dict) -> float:
    """USD for one call. Cached input is priced at ``cached_input_per_million_usd`` when the pricing file
    gives it, else at the full input price (conservative: never below the provider's charge for tokens)."""
    cached = event.get("cached_input_tokens") or 0
    if "cached_input_per_million_usd" in entry and 0 < cached <= event["input_tokens"]:
        input_cost = ((event["input_tokens"] - cached) * entry["input_per_million_usd"]
                      + cached * entry["cached_input_per_million_usd"])
    else:
        input_cost = event["input_tokens"] * entry["input_per_million_usd"]
    return (input_cost + event["output_tokens"] * entry["output_per_million_usd"]) / 1_000_000


def _timeline(observations: list[dict[str, Any]], wall_time_seconds: float) -> dict[str, Any] | None:
    """Where the wall time went: inside LLM calls, before the first, after the last.

    Overlapping calls are merged, so ``llm_busy_seconds`` is the time at least one
    call was in flight (equal to the summed latency when calls run one at a time).
    Offsets are relative to the callback's creation, which E3Session does at the
    start of its wall clock. None when no call carries an offset.
    """
    spans = sorted(
        (e["start_offset_ms"], e["start_offset_ms"] + e["latency_ms"])
        for e in observations
        if e.get("start_offset_ms") is not None
    )
    if not spans:
        return None
    busy_ms = 0.0
    run_start, run_end = spans[0]
    for start, end in spans[1:]:
        if start > run_end:
            busy_ms += run_end - run_start
            run_start, run_end = start, end
        else:
            run_end = max(run_end, end)
    busy_ms += run_end - run_start
    first_start = spans[0][0] / 1000
    last_end = max(end for _, end in spans) / 1000
    busy = busy_ms / 1000
    return {
        "llm_busy_seconds": round(busy, 3),
        "summed_llm_latency_seconds": round(sum(e - s for s, e in spans) / 1000, 3),
        "before_first_llm_seconds": round(first_start, 3),
        "between_llm_calls_seconds": round(last_end - first_start - busy, 3),
        "after_last_llm_seconds": round(wall_time_seconds - last_end, 3),
    }


def _incomplete_note(observed: bool, pricing, no_usage_calls: int, no_price_calls: int) -> str:
    """Why estimated_cost_usd is None, naming the actual cause (a call without usage is not a missing price)."""
    if not observed:
        return "Not calculated: no LLM call was observed."
    if not pricing:
        return "Not calculated: no model pricing was supplied (E3_PRICING_JSON)."
    reasons = []
    if no_usage_calls:
        reasons.append(f"{no_usage_calls} call(s) reported no token usage (no tokens are assumed for them)")
    if no_price_calls:
        reasons.append(f"{no_price_calls} call(s) used a model with no price in the pricing file")
    return "Not calculated: " + "; ".join(reasons) + "."


class E3MetricsCallback(BaseCallbackHandler):
    """Collect LLM-call duration, model, errors, and provider-reported token counts."""

    def __init__(self) -> None:
        self.started: dict[str, dict[str, Any]] = {}
        self.records: list[dict[str, Any]] = []
        self.lock = RLock()
        # Reference point for per-call start offsets (E3 timeline).
        self.created = time.perf_counter()

    def _start(self, serialized: dict[str, Any], run_id: Any, metadata: dict | None) -> None:
        with self.lock:
            self.started[str(run_id)] = {
                "start": time.perf_counter(),
                "model": _model_name(serialized, metadata),
                "agent": _agent_name(metadata),
            }

    def on_llm_start(self, serialized: dict[str, Any], prompts: list[str], *, run_id: Any,
                     metadata: dict | None = None, **kwargs: Any) -> None:
        self._start(serialized, run_id, metadata)

    def on_chat_model_start(self, serialized: dict[str, Any], messages: list, *, run_id: Any,
                            metadata: dict | None = None, **kwargs: Any) -> None:
        self._start(serialized, run_id, metadata)

    def _finish(self, run_id: Any, *, error: str | None = None,
                input_tokens: int | None = None,
                output_tokens: int | None = None, code: int | None = None,
                details: tuple[int | None, int | None] = (None, None)) -> None:
        with self.lock:
            prior = self.started.pop(str(run_id), None)
            if prior is None:
                # Don't manufacture timing if start event wasn't received.
                return
            self.records.append({
                "run_id": str(run_id),
                "agent": prior["agent"],
                "model": prior["model"],
                "start_offset_ms": round((prior["start"] - self.created) * 1000, 2),
                "latency_ms": round((time.perf_counter() - prior["start"]) * 1000, 2),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "error": error,
                "error_code": code,
                "cached_input_tokens": details[0],
                "reasoning_tokens": details[1],
            })

    def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        inp, out = _tokens(response)
        self._finish(run_id, input_tokens=inp, output_tokens=out, details=_token_details(response))

    def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        # Phase 3: LangChain passes what was generated before the error (``response``); a streamed
        # call that failed mid-way can carry the provider's usage there. Recorded when present,
        # otherwise left None: an unreported usage is not zero.
        inp, out = _tokens(kwargs.get("response")) if kwargs.get("response") is not None else (None, None)
        self._finish(run_id, error=f"{type(error).__name__}: {error}", input_tokens=inp, output_tokens=out,
                     code=error_code(error))

    def summary(self, *, ticker: str, analysis_date: str, status: str,
                wall_time_seconds: float, decision: str | None = None,
                run_error: str | None = None,
                pricing: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
        with self.lock:
            observations = list(self.records)
        # Optional pricing is in USD per 1 million input/output tokens.
        # An estimate is shown only when EVERY observed call has usage and known pricing.
        # Calculate costs for calls with reported token usage.
        cost_total = 0.0
        cost_complete = bool(observations) and bool(pricing)
        priced_calls = 0
        unpriced_calls = 0          # Phase 2 field: calls not priced, for either reason below
        no_usage_calls = 0          # the provider reported no tokens (e.g. a failed 429)
        no_price_calls = 0          # tokens reported, but the model has no price

        for event in observations:
            entry = (pricing or {}).get(event["model"])

            if (
                not entry
                or event["input_tokens"] is None
                or event["output_tokens"] is None
                or "input_per_million_usd" not in entry
                or "output_per_million_usd" not in entry
            ):
                cost_complete = False
                unpriced_calls += 1
                if event["input_tokens"] is None or event["output_tokens"] is None:
                    no_usage_calls += 1
                else:
                    no_price_calls += 1
                continue

            cost_total += _call_cost(event, entry)

            priced_calls += 1

        # Phase 3 (additive): calls that failed without reporting usage, typically an
        # E1 main-model 429 before the backup answered. They make estimated_cost_usd
        # None above (it stays strict: a failed call can still be billed, e.g. a
        # timeout after the provider generated output). This second estimate covers
        # the calls that succeeded, so a run with an E1 fallback still has a figure.
        def _failed_without_usage(event):
            return event["error"] is not None and (event["input_tokens"] is None or event["output_tokens"] is None)

        failed_without_usage = sum(1 for e in observations if _failed_without_usage(e))
        successful = [e for e in observations if not _failed_without_usage(e)]
        successful_cost, successful_complete = 0.0, bool(successful) and bool(pricing)
        for event in successful:
            entry = (pricing or {}).get(event["model"]) or {}
            if (event["input_tokens"] is None or event["output_tokens"] is None
                    or "input_per_million_usd" not in entry or "output_per_million_usd" not in entry):
                successful_complete = False
                continue
            successful_cost += _call_cost(event, entry)

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in observations:
            grouped[record["agent"]].append(record)
        per_agent = {}
        for agent, items in sorted(grouped.items()):
            per_agent[agent] = {
                "llm_calls": len(items),
                "errors": sum(x["error"] is not None for x in items),
                "observed_llm_latency_ms": round(sum(x["latency_ms"] for x in items), 2),
                "input_tokens_reported": sum(x["input_tokens"] or 0 for x in items),
                "output_tokens_reported": sum(x["output_tokens"] or 0 for x in items),
                "missing_usage_events": sum(x["input_tokens"] is None or x["output_tokens"] is None for x in items),
                "models": sorted({x["model"] for x in items}),
            }
        return {
            "schema": "tradingagents-e3-summary-v1",
            "created_at_utc": datetime.now(UTC).isoformat(),
            "ticker": ticker,
            "analysis_date": analysis_date,
            "status": status,
            "decision": str(decision) if decision is not None else None,
            "run_error": run_error,
            "wall_time_seconds": round(wall_time_seconds, 3),
            "observed_llm_calls": len(observations),
            "observed_llm_errors": sum(x["error"] is not None for x in observations),
            "timing": _timeline(observations, wall_time_seconds),
            "per_call": [
                {
                    "agent": event["agent"],
                    "model": event["model"],
                    "start_offset_ms": event.get("start_offset_ms"),
                    "latency_ms": event["latency_ms"],
                    "input_tokens": event["input_tokens"],
                    "output_tokens": event["output_tokens"],
                    "had_error": event["error"] is not None,
                    # Phase 3 (additive): whether the provider reported usage; None tokens mean "not reported".
                    "usage_reported": event["input_tokens"] is not None and event["output_tokens"] is not None,
                    "error_code": event.get("error_code"),
                    # Phase 3 (additive): provider-reported detail; None = not reported.
                    "cached_input_tokens": event.get("cached_input_tokens"),
                    "reasoning_tokens": event.get("reasoning_tokens"),
                }
                for event in observations
            ],
            "per_agent": per_agent,
            "estimated_cost_usd": round(cost_total, 6) if cost_complete else None,
            "known_usage_cost_usd": round(cost_total, 6) if priced_calls else None,
            "cost_complete": cost_complete,
            "priced_llm_calls": priced_calls,
            "unpriced_llm_calls": unpriced_calls,
            "cost_note": ("Estimate from supplied model pricing and reported tokens; does not include tools or infrastructure."
                          if cost_complete else _incomplete_note(bool(observations), pricing, no_usage_calls,
                                                                  no_price_calls)),
            "failed_calls_without_usage": failed_without_usage,
            "failed_calls_with_usage": sum(1 for e in observations if e["error"] is not None
                                           and e["input_tokens"] is not None and e["output_tokens"] is not None),
            "estimated_cost_usd_successful_calls": round(successful_cost, 6) if successful_complete else None,
            # Phase 3 (additive): what each cost figure is. An estimate is never labelled a charge.
            "tokens_reported": {
                "input": sum(e["input_tokens"] or 0 for e in observations),
                "output": sum(e["output_tokens"] or 0 for e in observations),
                "cached_input": sum(e.get("cached_input_tokens") or 0 for e in observations),
                "reasoning": sum(e.get("reasoning_tokens") or 0 for e in observations),
                "calls_without_usage": sum(1 for e in observations
                                           if e["input_tokens"] is None or e["output_tokens"] is None),
            },
            "cost_basis": {
                "estimated_cost_usd": ("estimate: provider-reported tokens x the prices in E3_PRICING_JSON; "
                                       "not a charge"),
                "cached_input": ("priced at cached_input_per_million_usd where the pricing file gives it, "
                                 "otherwise at the full input price (conservative)"),
                "actual_billing_usd": None,
                "actual_billing_status": ("not verified: providers do not report a charge per request; compare "
                                          "with your billing export (scripts/reconcile_billing.py)"),
            },
            "limitations": [
                "Unattributed events may appear when the agent/node is absent from callback metadata.",
                "Tokens are counted only when reported by the underlying provider.",
                "Callback LLM latency is not the same as end-to-end run latency.",
                "Langfuse tracing records observations; it does not itself change E1 model routing or trigger fallback.",
            ],
        }


def run(ticker: str, analysis_date: str, output_dir: str = "e3_results") -> Path:
    """Run TradingAgents programmatically with opt-in Langfuse tracing and JSON metrics."""
    from dotenv import load_dotenv
    load_dotenv()
    required = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Set Langfuse environment variables before running: {', '.join(missing)}")

    from langfuse import get_client
    from langfuse.langchain import CallbackHandler

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    client = get_client()
    langfuse_handler = CallbackHandler()
    metrics = E3MetricsCallback()
    config = DEFAULT_CONFIG.copy()
    # Optional local override only. Keep credentials in .env and never copy them to JSON.
    # Adjust other model settings here to match the project configuration before testing.
    graph = TradingAgentsGraph(
        selected_analysts=("market", "social", "news", "fundamentals"),
        config=config,
        callbacks=[langfuse_handler, metrics],
    )
    began = time.perf_counter()
    status, decision, failure = "success", None, None
    try:
        _state, decision = graph.propagate(ticker, analysis_date)
    except Exception as exc:
        status = "failed"
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        # Ensure buffered Langfuse observations are sent before the process ends.
        client.flush()

    pricing_path = os.getenv("E3_PRICING_JSON")
    pricing = json.loads(Path(pricing_path).read_text(encoding="utf-8")) if pricing_path else None
    report = metrics.summary(ticker=ticker, analysis_date=analysis_date,
                             status=status, decision=decision,
                             wall_time_seconds=time.perf_counter() - began,
                             run_error=failure, pricing=pricing)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{ticker}_{analysis_date}_{uuid4().hex[:8]}_e3.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"E3 summary: {path}")
    if failure:
        raise RuntimeError(f"Analysis failed. E3 summary saved: {path}. {failure}")
    return path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run TradingAgents with Langfuse E3 tracing")
    parser.add_argument("--ticker", default="NVDA")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD, not in future")
    parser.add_argument("--out", default="e3_results")
    args = parser.parse_args()
    run(args.ticker, args.date, args.out)
