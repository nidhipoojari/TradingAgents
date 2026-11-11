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
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

try:
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError:  # useful error for running outside of TradingAgents environment
    raise SystemExit("Install the TradingAgents dependencies (langchain-core), then retry.")


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


class E3MetricsCallback(BaseCallbackHandler):
    """Collect LLM-call duration, model, errors, and provider-reported token counts."""

    def __init__(self) -> None:
        self.started: dict[str, dict[str, Any]] = {}
        self.records: list[dict[str, Any]] = []
        self.lock = RLock()

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
                output_tokens: int | None = None) -> None:
        with self.lock:
            prior = self.started.pop(str(run_id), None)
            if prior is None:
                # Don't manufacture timing if start event wasn't received.
                return
            self.records.append({
                "run_id": str(run_id),
                "agent": prior["agent"],
                "model": prior["model"],
                "latency_ms": round((time.perf_counter() - prior["start"]) * 1000, 2),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "error": error,
            })

    def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        inp, out = _tokens(response)
        self._finish(run_id, input_tokens=inp, output_tokens=out)

    def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        self._finish(run_id, error=f"{type(error).__name__}: {error}")

    def summary(self, *, ticker: str, analysis_date: str, status: str,
                wall_time_seconds: float, decision: str | None = None,
                run_error: str | None = None,
                pricing: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
        with self.lock:
            observations = list(self.records)
        # Optional pricing is in USD per 1 million input/output tokens.
        # An estimate is shown only when EVERY observed call has usage and known pricing.
        cost_total = 0.0
        cost_complete = bool(observations) and bool(pricing)
        for event in observations:
            entry = (pricing or {}).get(event["model"])
            if (not entry or event["input_tokens"] is None or
                    event["output_tokens"] is None or
                    "input_per_million_usd" not in entry or
                    "output_per_million_usd" not in entry):
                cost_complete = False
                continue
            cost_total += (event["input_tokens"] * entry["input_per_million_usd"] +
                           event["output_tokens"] * entry["output_per_million_usd"]) / 1_000_000
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
                "models": sorted(set(x["model"] for x in items)),
            }
        return {
            "schema": "tradingagents-e3-summary-v1",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker,
            "analysis_date": analysis_date,
            "status": status,
            "decision": str(decision) if decision is not None else None,
            "run_error": run_error,
            "wall_time_seconds": round(wall_time_seconds, 3),
            "observed_llm_calls": len(observations),
            "observed_llm_errors": sum(x["error"] is not None for x in observations),
            "per_agent": per_agent,
            "estimated_cost_usd": round(cost_total, 6) if cost_complete else None,
            "cost_note": ("Estimate from supplied model pricing and reported tokens; does not include tools or infrastructure."
                          if cost_complete else "Not calculated: supply pricing for every model and ensure every LLM callback includes token usage."),
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
