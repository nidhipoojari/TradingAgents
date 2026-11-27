"""Per-run LLM limits: a maximum number of model calls and an estimated-cost ceiling.

Opt-in: a run has limits only when its ``AnalysisRequest.budget`` is set. The dashboard (F1,
``dashboard_budget``) and the MCP server (F2, ``mcp_budget``) set one by default; the CLI does not, so its
behavior is unchanged. The MCP limits are read from the server's environment only: an MCP client
cannot raise or remove them.

How the guard works (``BudgetGuard``, a LangChain callback run in the process that makes the calls):

- **Admission at call start, atomically.** Every model call is admitted or refused in
  ``on_chat_model_start`` / ``on_llm_start`` under one lock, so parallel agents cannot slip past a
  limit together. A refusal raises the service's cancel signal (a BaseException), so E1's
  ``with_fallbacks`` cannot send the refused call to the backup model, and sets the run's cancel
  event, so no further agent step, model call or tool call starts.
- **Calls counted.** Every call LangChain starts counts: E1 primary calls, E1 backup calls after a
  primary failure, and failed calls. Retries inside a provider SDK happen within one LangChain
  call and are invisible here (see limitations).
- **Cost reserved before a call, reconciled after.** At admission a call reserves an estimate:
  its prompt at ``CHARS_PER_TOKEN`` characters per token plus ``OUTPUT_RESERVE_TOKENS`` output
  tokens, at the model's price. A call is refused if the cost already charged, plus the
  reservations of calls still running, plus its own reservation would pass the ceiling. When the
  call ends its reservation is replaced by its reported usage; a successful call that reports no
  usage keeps its full reservation, and a failed call without usage is charged its input estimate.
- **Unpriced is never free.** A model missing from the pricing file is charged at the highest
  price in it. A cost ceiling without any pricing refuses to start the run.

Limitations: the ceiling bounds this *estimate*, not the provider's bill. A call that writes more
than ``OUTPUT_RESERVE_TOKENS`` can take the estimate past the ceiling by the difference; calls
already running when a limit is reached are not interrupted (no provider SDK here can abort a
request) and are charged when they return; SDK-internal retries are not seen; the character-based
input estimate is approximate.
"""

from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

CALLS_ENV = "TRADINGAGENTS_DASHBOARD_MAX_LLM_CALLS"
COST_ENV = "TRADINGAGENTS_DASHBOARD_MAX_COST_USD"
DASHBOARD_DEFAULT_CALLS = 15
DASHBOARD_DEFAULT_COST_USD = 0.04
MCP_CALLS_ENV = "TRADINGAGENTS_MCP_MAX_LLM_CALLS"
MCP_COST_ENV = "TRADINGAGENTS_MCP_MAX_COST_USD"
MCP_DEFAULT_COST_USD = 0.04
OUTPUT_RESERVE_TOKENS = 2000
CHARS_PER_TOKEN = 3          # conservative: English text averages about 4 characters per token
_OFF = {"0", "off", "none", "false", "no"}


@dataclass(frozen=True)
class RunBudget:
    """Limits for one run; None means no limit of that kind."""

    max_llm_calls: int | None = None
    max_cost_usd: float | None = None

    def __post_init__(self):
        if self.max_llm_calls is not None and (not isinstance(self.max_llm_calls, int) or self.max_llm_calls < 1):
            raise ValueError(f"max_llm_calls must be a positive whole number, got {self.max_llm_calls!r}")
        if self.max_cost_usd is not None and not (self.max_cost_usd > 0 and math.isfinite(self.max_cost_usd)):
            raise ValueError(f"max_cost_usd must be a positive amount, got {self.max_cost_usd!r}")

    @property
    def active(self) -> bool:
        return self.max_llm_calls is not None or self.max_cost_usd is not None

    def describe(self) -> str:
        parts = []
        if self.max_llm_calls is not None:
            parts.append(f"{self.max_llm_calls} LLM calls")
        if self.max_cost_usd is not None:
            parts.append(f"${self.max_cost_usd:.2f} estimated cost")
        return ", ".join(parts) if parts else "no limits"


def dashboard_budget_from_env() -> RunBudget | None:
    """The dashboard's per-run limits: 15 calls and $0.04 unless the two variables say otherwise.

    Each variable takes a number, or 0/off/none to switch that limit off. Raises ValueError on
    anything else, so a typo never silently removes a limit.
    """
    def read(name, default, kind):
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return default
        if raw.lower() in _OFF:
            return None
        try:
            return kind(raw)
        except ValueError:
            raise ValueError(f"{name}={raw!r} is not a number; use a number, or 0/off to switch it off") from None

    budget = RunBudget(read(CALLS_ENV, DASHBOARD_DEFAULT_CALLS, int), read(COST_ENV, DASHBOARD_DEFAULT_COST_USD, float))
    return budget if budget.active else None


CALL_HEADROOM = 1.5           # room for E1 fallbacks and extra tool rounds above the expected count


def expected_llm_calls(analysts: int, debate_rounds: int = 1, risk_rounds: int = 1) -> int:
    """Model calls a run makes when every model answers first time and each analyst asks for its tools
    once: 2 per analyst (tool request, report), 2 per debate round (bull, bear), the research manager,
    the trader, 3 per risk round (aggressive, neutral, conservative) and the portfolio manager."""
    return 2 * analysts + 2 * debate_rounds + 1 + 1 + 3 * risk_rounds + 1


def call_limit_for(analysts: int, debate_rounds: int = 1, risk_rounds: int = 1) -> int:
    """The default call limit for that workflow: the expected count x CALL_HEADROOM, rounded up.
    Market only at depth 1: 10 expected -> 15. All four analysts at depth 1: 16 -> 24."""
    return math.ceil(expected_llm_calls(analysts, debate_rounds, risk_rounds) * CALL_HEADROOM)


def default_cost_ceiling() -> float | None:
    """The configured cost ceiling (TRADINGAGENTS_DASHBOARD_MAX_COST_USD, default $0.04); None if 'off'."""
    raw = (os.environ.get(COST_ENV) or "").strip()
    if not raw:
        return DASHBOARD_DEFAULT_COST_USD
    if raw.lower() in _OFF:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{COST_ENV}={raw!r} is not a number; use a number, or 0/off to switch it off") from None


def dashboard_budget(analysts: int, debate_rounds: int = 1, risk_rounds: int = 1, *,
                     cost_ceiling: float | None = None, approved: bool = False) -> RunBudget | None:
    """The dashboard's limits for one run.

    Calls: TRADINGAGENTS_DASHBOARD_MAX_LLM_CALLS when set (a number, or off), else ``call_limit_for`` the
    chosen analysts and depth. Cost: the configured ceiling; a lower ``cost_ceiling`` is accepted, a
    higher one only with ``approved=True`` (never raised automatically). Raises ValueError otherwise.
    """
    raw = (os.environ.get(CALLS_ENV) or "").strip()
    if not raw:
        calls = call_limit_for(analysts, debate_rounds, risk_rounds)
    elif raw.lower() in _OFF:
        calls = None
    else:
        try:
            calls = int(raw)
        except ValueError:
            raise ValueError(f"{CALLS_ENV}={raw!r} is not a number; use a number, or 0/off to switch it off") from None
    default = default_cost_ceiling()
    cost = default
    if cost_ceiling is not None:
        if default is not None and cost_ceiling > default and not approved:
            raise ValueError(f"raising the cost ceiling from ${default:.2f} to ${cost_ceiling:.2f} needs your "
                             "explicit approval for this run")
        cost = cost_ceiling
    budget = RunBudget(calls, cost)
    return budget if budget.active else None


def _read_limit(name: str, default, kind):
    """A limit from the environment: unset -> default, 0/off/none -> None (switched off), else a number.
    Anything else raises ValueError, so a typo never silently removes a limit."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    if raw.lower() in _OFF:
        return None
    try:
        return kind(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number; use a number, or 0/off to switch it off") from None


def mcp_budget(analysts: int, debate_rounds: int = 1, risk_rounds: int = 1) -> RunBudget | None:
    """F2's limits for one MCP run, set by the server operator only (never by the MCP client).

    Calls: TRADINGAGENTS_MCP_MAX_LLM_CALLS when set (a number, or off), else ``call_limit_for`` the requested
    analysts and depth, as in the dashboard (market only: 15; all four analysts: 24). Cost:
    TRADINGAGENTS_MCP_MAX_COST_USD, default $0.04 estimated. Raises ValueError on a malformed value.
    """
    calls = _read_limit(MCP_CALLS_ENV, call_limit_for(analysts, debate_rounds, risk_rounds), int)
    cost = _read_limit(MCP_COST_ENV, MCP_DEFAULT_COST_USD, float)
    budget = RunBudget(calls, cost)
    return budget if budget.active else None


def load_pricing() -> dict[str, dict[str, float]]:
    """The pricing E3 uses (E3_PRICING_JSON, USD per million tokens), or {} when none is configured."""
    path = os.environ.get("E3_PRICING_JSON")
    if not path or not Path(path).is_file():
        return {}
    prices = json.loads(Path(path).read_text(encoding="utf-8"))
    return {model: p for model, p in prices.items()
            if isinstance(p, dict) and "input_per_million_usd" in p and "output_per_million_usd" in p}


def check_pricing(budget: RunBudget | None, pricing: dict | None = None, *, cost_env: str = COST_ENV) -> None:
    """Refuse a cost ceiling that cannot be enforced (no prices at all)."""
    if budget is not None and budget.max_cost_usd is not None and not (load_pricing() if pricing is None else pricing):
        raise ValueError("a per-run cost limit needs model prices: set E3_PRICING_JSON to a pricing file "
                         f"(or switch the limit off with {cost_env}=off)")


class BudgetGuard(BaseCallbackHandler):
    """Enforces a RunBudget on the calls of one run (see the module docstring)."""

    raise_error = True      # a refusal must stop the call, not be logged and ignored
    run_inline = True

    def __init__(self, budget: RunBudget, cancel: threading.Event, pricing: dict | None = None,
                 on_exhausted=None):
        super().__init__()
        self.budget, self.cancel, self.on_exhausted = budget, cancel, on_exhausted
        self.pricing = load_pricing() if pricing is None else pricing
        check_pricing(budget, self.pricing)
        self._lock = threading.Lock()
        self.calls_started = 0
        self.refused = 0
        # The charged amount, by source (see snapshot()); their sum is what admission counts.
        self.reported_usd = 0.0            # reported tokens x the model's own price
        self.unpriced_usd = 0.0            # reported tokens of an unpriced model x the highest known price
        self.unreported_usd = 0.0          # calls that reported no usage: reservation or input estimate
        self._in_flight: dict[str, tuple[float, dict | None, bool, int]] = {}
        self.unpriced_calls = 0
        self.calls_without_usage = 0       # successful calls that reported no usage
        self.failed_calls = 0
        self.failed_calls_with_usage = 0
        self.exhausted: str | None = None
        self.retry_counter = None          # the service's SDK-retry observer, when one is installed

    @property
    def charged_usd(self) -> float:
        return self.reported_usd + self.unpriced_usd + self.unreported_usd

    # ------------------------------------------------------------------ pricing
    def _price(self, model: str) -> tuple[dict | None, bool]:
        """(price, priced): an unknown model gets the highest known price, never zero."""
        if model in self.pricing:
            return self.pricing[model], True
        highest = max(self.pricing.values(), default=None,
                      key=lambda p: p["input_per_million_usd"] + p["output_per_million_usd"])
        return highest, False

    @staticmethod
    def _cost(price: dict | None, tokens_in: int, tokens_out: int) -> float:
        if price is None:
            return 0.0
        return (tokens_in * price["input_per_million_usd"] + tokens_out * price["output_per_million_usd"]) / 1e6

    # ------------------------------------------------------------------ admission
    def _admit(self, run_id: Any, serialized, metadata, text_chars: int) -> None:
        from tradingagents.e3_tracing import _model_name
        from tradingagents.service import _CancelSignal

        if self.cancel.is_set() and not self.exhausted:
            return          # cancelled by the person: the CancellationGuard refuses it, uncounted here
        price, priced = self._price(_model_name(serialized, metadata))
        tokens_in = math.ceil(text_chars / CHARS_PER_TOKEN)
        reservation = self._cost(price, tokens_in, OUTPUT_RESERVE_TOKENS)
        tripped = None
        with self._lock:
            if self.exhausted:
                self.refused += 1
                raise _CancelSignal("before a model call")      # the reason is in the stop's message
            limit_calls, limit_cost = self.budget.max_llm_calls, self.budget.max_cost_usd
            committed = self.charged_usd + sum(entry[0] for entry in self._in_flight.values())
            if limit_calls is not None and self.calls_started >= limit_calls:
                tripped = f"call limit reached: {self.calls_started} of {limit_calls} LLM calls used"
            elif limit_cost is not None and committed + reservation > limit_cost:
                tripped = (f"cost limit reached: ${committed:.6f} charged or reserved, and the next call's "
                           f"reservation (${reservation:.6f}) would pass ${limit_cost:.2f}")
            if tripped:
                self.exhausted = tripped
                self.refused += 1
            else:
                self.calls_started += 1
                if not priced:
                    self.unpriced_calls += 1
                self._in_flight[str(run_id)] = (reservation, price, priced, tokens_in)
        if tripped:
            self.cancel.set()
            if self.on_exhausted is not None:
                self.on_exhausted(self.snapshot())
            raise _CancelSignal("before a model call")

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        chars = sum(len(str(getattr(m, "content", "") or "")) for batch in messages for m in batch)
        self._admit(run_id, serialized, metadata, chars)

    def on_llm_start(self, serialized, prompts, *, run_id, metadata=None, **kwargs):
        self._admit(run_id, serialized, metadata, sum(len(p or "") for p in prompts))

    # ------------------------------------------------------------------ reconciliation
    def _charge_reported(self, price, priced, tokens_in, tokens_out) -> None:
        cost = self._cost(price, tokens_in, tokens_out)
        if priced:
            self.reported_usd += cost
        else:
            self.unpriced_usd += cost

    def on_llm_end(self, response, *, run_id, **kwargs):
        from tradingagents.e3_tracing import _tokens

        tokens_in, tokens_out = _tokens(response)
        with self._lock:
            reservation, price, priced, _ = self._in_flight.pop(str(run_id), (None, None, True, 0))
            if reservation is None:
                return
            if tokens_in is not None and tokens_out is not None:
                self._charge_reported(price, priced, tokens_in, tokens_out)
            else:                                   # no usage reported: keep the whole reservation
                self.calls_without_usage += 1
                self.unreported_usd += reservation

    def on_llm_error(self, error, *, run_id, **kwargs):
        from tradingagents.e3_tracing import _tokens

        response = kwargs.get("response")
        tokens_in, tokens_out = _tokens(response) if response is not None else (None, None)
        with self._lock:
            reservation, price, priced, input_estimate = self._in_flight.pop(str(run_id), (None, None, True, 0))
            if reservation is None:
                return
            self.failed_calls += 1
            if tokens_in is not None and tokens_out is not None:
                # A failed (e.g. streamed) call that still reported usage is charged that usage.
                self.failed_calls_with_usage += 1
                self._charge_reported(price, priced, tokens_in, tokens_out)
            else:
                # No usage reported: it may still have been billed for its input, so charge that estimate.
                self.unreported_usd += self._cost(price, input_estimate, 0)

    # ------------------------------------------------------------------ reporting
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            reserved = sum(entry[0] for entry in self._in_flight.values())
            charged = self.charged_usd
            return {
                "max_llm_calls": self.budget.max_llm_calls,
                "max_cost_usd": self.budget.max_cost_usd,
                "llm_calls_started": self.calls_started,
                "llm_calls_refused": self.refused,
                "llm_calls_in_flight": len(self._in_flight),
                # What the limit counts, split by where each part comes from. Only the first is computed
                # from the provider's reported tokens; none of these is the provider's bill.
                "reported_usage_cost_usd": round(self.reported_usd, 6),
                "unpriced_usage_estimate_usd": round(self.unpriced_usd, 6),
                "unreported_usage_estimate_usd": round(self.unreported_usd, 6),
                "budget_cost_usd": round(charged, 6),
                "reserved_in_flight_usd": round(reserved, 6),
                "conservative_total_usd": round(charged + reserved, 6),
                "failed_calls": self.failed_calls,
                "failed_calls_with_usage": self.failed_calls_with_usage,
                "sdk_retries_observed": self.retry_counter.report() if self.retry_counter is not None else None,
                "sdk_retries_observability": (self.retry_counter.observability if self.retry_counter is not None
                                              else "not measured"),
                "calls_without_usage": self.calls_without_usage,
                "unpriced_calls": self.unpriced_calls,
                "exhausted": self.exhausted is not None,
                "reason": self.exhausted,
                "basis": ("conservative budget accounting, not the provider's bill: reported tokens at the "
                          "model's price; unpriced models at the highest known price; calls that reported no "
                          f"usage at a reservation (prompt/{CHARS_PER_TOKEN} input + {OUTPUT_RESERVE_TOKENS} "
                          "output tokens) or, if they failed, their input estimate. The provider may bill "
                          "differently (failed or retried requests, its own token counts and prices)."),
            }


def describe_snapshot(snapshot: dict | None) -> str:
    """One line for people: the run's use of its limits, with every estimated part labelled."""
    if not snapshot:
        return "Per-run limits: off"
    calls = f"{snapshot['llm_calls_started']}"
    if snapshot.get("max_llm_calls") is not None:
        calls += f" of {snapshot['max_llm_calls']}"
    total = f"${snapshot['budget_cost_usd']:.6f}"
    if snapshot.get("max_cost_usd") is not None:
        total += f" of ${snapshot['max_cost_usd']:.2f}"
    parts = [f"${snapshot.get('reported_usage_cost_usd', 0):.6f} from reported tokens"]
    if snapshot.get("unpriced_usage_estimate_usd"):
        parts.append(f"${snapshot['unpriced_usage_estimate_usd']:.6f} estimated for unpriced models")
    if snapshot.get("unreported_usage_estimate_usd"):
        parts.append(f"${snapshot['unreported_usage_estimate_usd']:.6f} estimated for calls that reported no usage")
    if snapshot.get("reserved_in_flight_usd"):
        parts.append(f"${snapshot['reserved_in_flight_usd']:.6f} reserved for calls still running")
    line = (f"Per-run limits: {calls} LLM calls; budget accounting {total} "
            f"(conservative, not a bill: {' + '.join(parts)})")
    extras = [f"{snapshot[k]} {label}" for k, label in (("llm_calls_refused", "refused"),
                                                        ("sdk_retries_observed", "hidden SDK retries"))
              if snapshot.get(k)]
    if extras:
        line += "; " + ", ".join(extras)
    if snapshot.get("exhausted"):
        line += f". Stopped: {snapshot['reason']}"
    return line
