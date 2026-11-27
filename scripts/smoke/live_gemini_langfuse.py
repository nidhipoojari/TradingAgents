#!/usr/bin/env python3
"""Phase 3 live smoke test 1: one real Gemini analysis, checked against E1, E2, E3 and Langfuse.

NOT part of pytest. Without ``--approve-live-llm`` it only prints the plan and an
offline pre-flight check, makes no network call, and exits 2.

    python scripts/smoke/live_gemini_langfuse.py                     # plan + pre-flight only
    python scripts/smoke/live_gemini_langfuse.py --approve-live-llm  # ONE live analysis
        [--warm-run]        # a second analysis on the same cache (doubles the cost) to check E2 warm
        [--force-fallback]  # main quick model set to a missing name, as in Phase 2's forced_failure run

What one approved run does: the market analyst only, 1 debate and 1 risk round,
through the shared analysis service (source "api"), with E2 on (a fresh cache, so
cold) and E3 + Langfuse on. Everything it writes goes under --out.

Expected (Phase 2 reference: eval/tracing_31/01_NVDA, the same shape on gemini-3.1-flash-lite:
10 LLM calls, 28,354 tokens, $0.0136, 89.9 s):
  LLM calls 9-14 (more with --force-fallback), tokens 15k-45k, cost about $0.01-$0.03
  (fail above --max-cost, default $0.05), wall time 30-180 s (warn above 300 s).
Pass/fail criteria are the CHECKS listed in the plan; any FAIL exits 1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _safety import fresh_dir, provenance, redact  # noqa: E402

PLAN = """\
Live smoke test 1 (Gemini + Langfuse) -- NOT RUN (pass --approve-live-llm to run it)

One approved run makes about 10 Gemini calls (~$0.01-$0.03) and sends traces to your
Langfuse project. --warm-run doubles that; --force-fallback adds failing calls to a model
that does not exist (not billed by the provider).

HARD BOUNDS per analysis: at most --max-calls (default 30) model calls can start (a call past
the budget is refused at its own start, also under parallel analysts); once the priced cost
reaches --max-cost (default $0.05) no further call starts and the run is cancelled. Calls
already in flight finish, so the cost worst case is the ceiling plus the calls in flight
(one per parallel analyst, ~$0.003 each). Tool rounds are
capped at 3 and SDK retries at 2. Langfuse is polled at most 25 times (120 s).

CHECKS (PASS/FAIL unless marked WARN):
  run.completed            the analysis finished (no exception)
  run.rating               a 5-tier rating (WARN on REVIEW: LLM output, not a code fault)
  files.token_usage        token_usage.json exists with this run_id and source "api"
  e3.summary               E3 JSON exists, status success, same run_id, source "api"
  e3.attribution           every call attributed to an agent (no "unattributed")
  e3.calls_match           E3 observed_llm_calls == token_usage llm_calls
  e3.tokens_match          E3 input/output tokens == token_usage tokens_in/out, both > 0
  e3.cost                  estimated_cost_usd_successful_calls is set and <= --max-cost
  e3.timing                pre + analysis + post == wall_time_seconds (within 0.05 s)
  e1.routing               Research Manager and Portfolio Manager on the deep model, others on quick
  e1.fallback              (--force-fallback) backup switches logged, E3 errors > 0, run still completes
  e2.cold                  e2_cache present with misses > 0 (fresh cache; not in --phase2-baseline, where E2 is off)
  e2.warm                  (--warm-run) second run: misses == 0, hit rate 100%
  langfuse.session         observations found in Langfuse under session_id == run_id (polls up to 120 s)
  run.budget               the call/cost budget was not hit (a hit cancels the run: FAIL)
  time.wall                WARN above 300 s
  phase2.*                 (--phase2-baseline) calls 10-14 checked; wall, LLM time, tokens and cost
                           reported against consistency_evaluation/nvda_live (WARN outside +/-25%)
"""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def preflight(config: dict, force_fallback: bool) -> list[str]:
    """What is missing for a live run, checked offline. Never prints secret values."""
    problems = []
    if config.get("llm_provider") != "google":
        problems.append(f"TRADINGAGENTS_LLM_PROVIDER should be google (is {config.get('llm_provider')!r})")
    if not os.environ.get("GOOGLE_API_KEY"):
        problems.append("GOOGLE_API_KEY is not set")
    if not _truthy(os.environ.get("E3_TRACE_ENABLED")):
        problems.append("E3_TRACE_ENABLED=1 is not set")
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        if not os.environ.get(key):
            problems.append(f"{key} is not set")
    pricing_path = os.environ.get("E3_PRICING_JSON")
    if not pricing_path or not Path(pricing_path).is_file():
        problems.append("E3_PRICING_JSON must name a pricing file (e.g. e3_pricing.json)")
    else:
        prices = json.loads(Path(pricing_path).read_text(encoding="utf-8"))
        # With --force-fallback the quick tier is a missing model on purpose; its backup answers.
        needed = {config.get("deep_think_llm"),
                  config.get("quick_fallback_llm") if force_fallback else config.get("quick_think_llm")}
        missing = sorted(str(m) for m in needed if m not in prices)
        if missing:
            problems.append(f"no price in {pricing_path} for: {', '.join(missing)}")
    return problems


# Phase 2 baseline (read-only references, tracked in git): the live-data runs this mode is compared with.
PHASE2_REFERENCES = sorted((ROOT / "consistency_evaluation" / "nvda_live" / "run_1" / "e3").glob("*_e3.json"))
PHASE2_MODELS = {"quick_think_llm": "gemini-3.5-flash-lite", "deep_think_llm": "gemini-3.5-flash-lite",
                 "quick_fallback_llm": "gemini-3.1-flash-lite", "deep_fallback_llm": "gemini-3.1-flash-lite"}


def phase2_baseline_problems(config: dict) -> list[str]:
    """The configured models must be Phase 2's for the latency comparison to mean anything."""
    return [f"{key} is {config.get(key)!r}; the Phase 2 baseline used {value!r}"
            for key, value in PHASE2_MODELS.items() if config.get(key) != value]


def compare_with_phase2(report, summary: dict, references: list[dict]) -> dict:
    """Phase 3 vs the Phase 2 live-data references. Calls and completeness are checks; latency,
    tokens and cost are reported against the references' range (WARN outside +/-25%): LLM
    output and provider latency vary from run to run, so they cannot be pass/fail."""
    def stats(s):
        calls = s.get("per_call", [])
        return {"wall_s": s.get("wall_time_seconds"), "llm_s": round(sum(c["latency_ms"] for c in calls) / 1000, 1),
                "calls": s.get("observed_llm_calls"), "tokens_in": sum(c["input_tokens"] or 0 for c in calls),
                "tokens_out": sum(c["output_tokens"] or 0 for c in calls),
                "cost": s.get("estimated_cost_usd_successful_calls", s.get("estimated_cost_usd"))}
    mine, refs = stats(summary), [stats(r) for r in references]
    out = {"phase3": mine, "phase2_references": refs}
    report.add("phase2.calls", 10 <= (mine["calls"] or 0) <= 14,
               f"{mine['calls']} (Phase 2 market-only runs made 10-12: the model batches its tool calls 1-3 per round)")
    for key in ("wall_s", "llm_s", "tokens_in", "tokens_out", "cost"):
        values = [r[key] for r in refs if isinstance(r[key], int | float)]
        if not values or not isinstance(mine[key], int | float):
            report.add(f"phase2.{key}", None, f"{mine[key]} (no reference)", warn=True)
            continue
        low, high = min(values) * 0.75, max(values) * 1.25
        report.add(f"phase2.{key}", low <= mine[key] <= high,
                   f"{mine[key]} vs Phase 2 {min(values)}-{max(values)} (band {low:.4g}-{high:.4g})", warn=True)
    return out


class BudgetGuard:
    """Cancels the run when the call count or the priced cost reaches its ceiling.

    Works through the shared service's cancel event, so the CancellationGuard then
    refuses every further model and tool call. Cost is priced from each call's
    reported usage with the same pricing file E3 uses.
    """

    def __new__(cls, cancel, max_calls: int, max_cost: float, pricing: dict):
        from langchain_core.callbacks import BaseCallbackHandler

        class Guard(BaseCallbackHandler):
            raise_error = True      # a refusal must stop the call
            run_inline = True

            def __init__(self):
                super().__init__()
                self.calls, self.cost, self.tripped, self.models = 0, 0.0, "", {}
                self.lock = threading.Lock()

            def _check(self):
                if self.calls >= max_calls and not self.tripped:
                    self.tripped = f"{self.calls} model calls reached the --max-calls budget ({max_calls})"
                elif self.cost >= max_cost and not self.tripped:
                    self.tripped = f"${self.cost:.4f} reached the --max-cost budget (${max_cost})"
                if self.tripped:
                    cancel.set()

            def _started(self, kwargs):
                # Atomic: with parallel analysts, a call is refused here, at its own start, once the
                # budget is spent; waiting for the cancel flag would let concurrent starts slip by.
                from tradingagents.service import _CancelSignal

                with self.lock:
                    if self.calls >= max_calls or self.cost >= max_cost:
                        self._check()
                        raise _CancelSignal("before a model call: the smoke-test budget is spent")
                    self.calls += 1
                    self.models[str(kwargs.get("run_id"))] = (kwargs.get("metadata") or {}).get("ls_model_name")
                    self._check()

            def on_chat_model_start(self, serialized, messages, **kwargs):
                self._started(kwargs)

            def on_llm_start(self, serialized, prompts, **kwargs):
                self._started(kwargs)

            def on_llm_end(self, response, **kwargs):
                from tradingagents.e3_tracing import _tokens

                tokens_in, tokens_out = _tokens(response)
                model = self.models.pop(str(kwargs.get("run_id")), None)
                # An unpriced model is charged at the most expensive known price: never under-count.
                price = pricing.get(model) or max(pricing.values(), key=lambda p: p["output_per_million_usd"],
                                                  default=None)
                with self.lock:
                    if price and tokens_in is not None and tokens_out is not None:
                        self.cost += (tokens_in * price["input_per_million_usd"]
                                      + tokens_out * price["output_per_million_usd"]) / 1_000_000
                    self._check()

        return Guard()


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, ok: bool | None, detail: str = "", warn: bool = False) -> None:
        status = "PASS" if ok else ("WARN" if warn or ok is None else "FAIL")
        self.rows.append((name, status, redact(detail)))

    @property
    def failed(self) -> bool:
        return any(status == "FAIL" for _, status, _ in self.rows)

    def show(self) -> None:
        width = max(len(n) for n, _, _ in self.rows)
        for name, status, detail in self.rows:
            print(f"  {status:4}  {name:{width}}  {detail}")


def _langfuse_observations(run_id: str, timeout: float = 120.0) -> int:
    from langfuse import get_client

    client = get_client()
    client.flush()
    deadline = time.monotonic() + timeout
    while True:
        response = client.api.observations.get_many(session_id=run_id, limit=100)
        found = len(getattr(response, "data", None) or [])
        if found or time.monotonic() > deadline:
            return found
        time.sleep(5)


def check_run(report: Report, result, label: str, *, max_cost: float, deep_model: str,
              force_fallback: bool, quick_model: str | None = None,
              allowed_models: tuple = ()) -> dict:
    from tradingagents.e1_routing import ROUTING_TABLE, summarize_events as e1_summarize

    usage = json.loads(result.usage_path.read_text(encoding="utf-8"))
    report.add(f"{label}.files.token_usage", usage.get("run_id") == result.run_id and usage.get("source") == "api",
               str(result.usage_path))
    summary = json.loads(result.e3_summary_path.read_text(encoding="utf-8")) if result.e3_summary_path else {}
    report.add(f"{label}.e3.summary", summary.get("status") == "success" and summary.get("run_id") == result.run_id
               and summary.get("source") == "api", str(result.e3_summary_path))
    per_agent = summary.get("per_agent", {})
    report.add(f"{label}.e3.attribution", bool(per_agent) and "unattributed" not in per_agent,
               f"{len(per_agent)} agents")
    report.add(f"{label}.e3.calls_match", summary.get("observed_llm_calls") == usage.get("llm_calls"),
               f"E3 {summary.get('observed_llm_calls')} vs token_usage {usage.get('llm_calls')}")
    e3_in = sum(r["input_tokens_reported"] for r in per_agent.values())
    e3_out = sum(r["output_tokens_reported"] for r in per_agent.values())
    report.add(f"{label}.e3.tokens_match", (e3_in, e3_out) == (usage["tokens_in"], usage["tokens_out"]) and e3_in > 0,
               f"E3 {e3_in}/{e3_out} vs {usage['tokens_in']}/{usage['tokens_out']}")
    cost = summary.get("estimated_cost_usd_successful_calls")
    report.add(f"{label}.e3.cost", cost is not None and cost <= max_cost,
               f"${cost} (strict estimate ${summary.get('estimated_cost_usd')}, ceiling ${max_cost})")
    phases = [summary.get(k) for k in ("pre_analysis_seconds", "analysis_seconds", "post_analysis_seconds")]
    report.add(f"{label}.e3.timing", None not in phases
               and abs(sum(phases) - summary.get("wall_time_seconds", 0)) < 0.05,
               f"{phases} vs wall {summary.get('wall_time_seconds')}")
    deep_nodes = {"Research Manager", "Portfolio Manager"}
    if quick_model is not None and quick_model == deep_model:
        # One model on both tiers (the Phase 2 baseline): routing cannot be seen in model names. The
        # per-agent tier assignment is verified offline (tests/test_e1_e2_e3_audit.py); here, check
        # that every call went to a configured model (main or backup) and the table is unchanged.
        allowed = set(allowed_models or (quick_model, deep_model))
        stray = sorted({m for r in per_agent.values() for m in r["models"]} - allowed)
        report.add(f"{label}.e1.routing", None if not stray else False,
                   f"same model on both tiers ({deep_model}): tier routing is not visible in model names "
                   f"(verified offline); models outside the configuration: {stray or 'none'}")
    else:
        wrong = [a for a, r in per_agent.items()
                 if (deep_model in r["models"]) != (a in deep_nodes) and not force_fallback]
        report.add(f"{label}.e1.routing", not wrong and sum(v == "deep" for v in ROUTING_TABLE.values()) == 2,
                   "deep: " + ", ".join(sorted(a for a, r in per_agent.items() if deep_model in r["models"])))
    if force_fallback:
        report.add(f"{label}.e1.fallback", bool(result.fallback_events) and summary.get("observed_llm_errors", 0) > 0,
                   f"{e1_summarize(result.fallback_events)['backup_calls']} switches, "
                   f"{summary.get('observed_llm_errors')} failed calls")
    report.add(f"{label}.time.wall", summary.get("wall_time_seconds", 0) <= 300,
               f"{summary.get('wall_time_seconds')} s", warn=True)
    return usage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--approve-live-llm", action="store_true", help="make the live Gemini calls")
    parser.add_argument("--warm-run", action="store_true", help="run a second analysis to check E2 warm")
    parser.add_argument("--force-fallback", action="store_true", help="main quick model set to a missing name")
    parser.add_argument("--phase2-baseline", action="store_true",
                        help="run exactly as the Phase 2 live NVDA runs (E2 off, default tool rounds and retries) "
                             "and compare with them; the call/cost budget still bounds the run")
    parser.add_argument("--ticker", default="NVDA")
    parser.add_argument("--date", default="2026-10-23", help="past date, as in the Phase 2 runs")
    parser.add_argument("--max-cost", type=float, default=0.05, help="hard cost ceiling per analysis (USD)")
    parser.add_argument("--max-calls", type=int, default=30, help="hard model-call ceiling per analysis")
    parser.add_argument("--out", default=str(ROOT / "results" / "phase3_smoke" / datetime.now().strftime("%Y%m%d_%H%M%S")))
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)

    from tradingagents import service

    config = service.config_for_source("api")
    if args.force_fallback:
        config["quick_fallback_llm"] = config.get("quick_fallback_llm") or config["quick_think_llm"]
        config["quick_think_llm"] = "gemini-does-not-exist"
    problems = preflight(config, args.force_fallback)
    if args.phase2_baseline:
        if args.warm_run or args.force_fallback:
            problems.append("--phase2-baseline runs one plain analysis: drop --warm-run and --force-fallback")
        problems += phase2_baseline_problems(config)
        if not PHASE2_REFERENCES:
            problems.append("the Phase 2 reference summaries (consistency_evaluation/nvda_live) are missing")

    if not args.approve_live_llm:
        print(PLAN)
        print("Pre-flight (offline):", "ready" if not problems else "NOT READY")
        for problem in problems:
            print(f"  - {redact(problem)}")
        return 2
    if problems:
        print("Pre-flight failed; nothing was run:")
        for problem in problems:
            print(f"  - {redact(problem)}")
        return 1

    out = fresh_dir(args.out)
    run_info = provenance('live_gemini_langfuse.py', raw_argv)
    print("Run:", json.dumps(run_info))
    os.environ["E3_RESULTS_DIR"] = str(out / "e3")
    config.update(results_dir=str(out / "results"), data_cache_dir=str(out / "cache"),
                  memory_log_path=str(out / "memory" / "trading_memory.md"),
                  max_debate_rounds=1, max_risk_discuss_rounds=1)
    if args.phase2_baseline:
        # As the Phase 2 runs: E2 off, a fresh data folder (live fetch), default tool rounds and retries.
        config.update(e2_cache_enabled=False)
    else:
        config.update(e2_cache_enabled=True, max_tool_rounds=3, llm_max_retries=2)
    pricing = json.loads(Path(os.environ["E3_PRICING_JSON"]).read_text(encoding="utf-8"))
    report = Report()
    runs = 2 if args.warm_run else 1
    usages = []
    for n in range(1, runs + 1):
        label = f"run{n}"
        request = service.AnalysisRequest(ticker=args.ticker, trade_date=args.date, source="api",
                                          analysts=("market",), config=config)
        cancel = threading.Event()
        budget = BudgetGuard(cancel, args.max_calls, args.max_cost, pricing)
        try:
            result = service.run_analysis(request, cancel=cancel, extra_callbacks=[budget])
        except Exception as exc:  # noqa: BLE001 - reported as the failure it is
            report.add(f"{label}.run.budget", not budget.tripped, budget.tripped or "within budget")
            report.add(f"{label}.run.completed", False, f"{type(exc).__name__}: {str(exc)[:200]}")
            break
        report.add(f"{label}.run.budget", not budget.tripped,
                   budget.tripped or f"{budget.calls} calls, ${budget.cost:.4f}")
        report.add(f"{label}.run.completed", True, result.run_id)
        report.add(f"{label}.run.rating", not result.is_review, result.rating, warn=True)
        usages.append(check_run(report, result, label, max_cost=args.max_cost,
                                deep_model=config["deep_think_llm"], force_fallback=args.force_fallback,
                                quick_model=config["quick_think_llm"],
                                allowed_models=tuple(m for m in (config["quick_think_llm"], config["deep_think_llm"],
                                                                 config.get("quick_fallback_llm"),
                                                                 config.get("deep_fallback_llm")) if m)))
        if args.phase2_baseline:
            summary = json.loads(result.e3_summary_path.read_text(encoding="utf-8"))
            references = [json.loads(p.read_text(encoding="utf-8")) for p in PHASE2_REFERENCES]
            comparison = compare_with_phase2(report, summary, references)
            out.mkdir(parents=True, exist_ok=True)
            (out / "phase2_comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
        try:
            found = _langfuse_observations(result.run_id)
            report.add(f"{label}.langfuse.session", found > 0, f"{found} observations in session {result.run_id}")
        except Exception as exc:  # noqa: BLE001
            report.add(f"{label}.langfuse.session", False, f"{type(exc).__name__}: {str(exc)[:200]}")
    if usages and not args.phase2_baseline:
        cold = usages[0].get("e2_cache") or {}
        report.add("e2.cold", cold.get("misses", 0) > 0, json.dumps(cold))
    if len(usages) == 2:
        warm = usages[1].get("e2_cache") or {}
        report.add("e2.warm", warm.get("misses") == 0 and warm.get("hit_rate_pct") == 100.0, json.dumps(warm))

    print(f"Live smoke test 1 results ({out}):")
    report.show()
    out.mkdir(parents=True, exist_ok=True)
    (out / "smoke_report.json").write_text(json.dumps(
        {"run": {**run_info, "finished_at_local": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
                 "output_dir": str(out)},
         "checks": [{"check": n, "status": s, "detail": d} for n, s, d in report.rows]}, indent=2), encoding="utf-8")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
