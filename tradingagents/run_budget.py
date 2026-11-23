"""Per-run LLM limits: a maximum number of model calls and an estimated-cost ceiling.

Opt-in: a run has limits only when its ``AnalysisRequest.budget`` is set. The dashboard sets one
by default (``dashboard_budget_from_env``); the CLI and the MCP server do not, so their behavior
is unchanged.

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


def load_pricing() -> dict[str, dict[str, float]]:
    """The pricing E3 uses (E3_PRICING_JSON, USD per million tokens), or {} when none is configured."""
    path = os.environ.get("E3_PRICING_JSON")
    if not path or not Path(path).is_file():
        return {}
    prices = json.loads(Path(path).read_text(encoding="utf-8"))
    return {model: p for model, p in prices.items()
            if isinstance(p, dict) and "input_per_million_usd" in p and "output_per_million_usd" in p}


def check_pricing(budget: RunBudget | None, pricing: dict | None = None) -> None:
    """Refuse a cost ceiling that cannot be enforced (no prices at all)."""
    if budget is not None and budget.max_cost_usd is not None and not (load_pricing() if pricing is None else pricing):
        raise ValueError("a per-run cost limit needs model prices: set E3_PRICING_JSON to a pricing file "
                         f"(or switch the limit off with {COST_ENV}=off)")


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
        self.charged_usd = 0.0
        self._in_flight: dict[str, tuple[float, dict | None, int]] = {}
        self.unpriced_calls = 0
        self.calls_without_usage = 0
        self.failed_calls = 0
        self.exhausted: str | None = None

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
            committed = self.charged_usd + sum(r for r, _, _ in self._in_flight.values())
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
                self._in_flight[str(run_id)] = (reservation, price, tokens_in)
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
    def on_llm_end(self, response, *, run_id, **kwargs):
        from tradingagents.e3_tracing import _tokens

        tokens_in, tokens_out = _tokens(response)
        with self._lock:
            reservation, price, _ = self._in_flight.pop(str(run_id), (None, None, 0))
            if reservation is None:
                return
            if tokens_in is not None and tokens_out is not None:
                self.charged_usd += self._cost(price, tokens_in, tokens_out)
            else:                                   # no usage reported: keep the whole reservation
                self.calls_without_usage += 1
                self.charged_usd += reservation

    def on_llm_error(self, error, *, run_id, **kwargs):
        with self._lock:
            reservation, price, tokens_in = self._in_flight.pop(str(run_id), (None, None, 0))
            if reservation is None:
                return
            self.failed_calls += 1
            # A failed request reports no usage; it may still have been billed for its input.
            self.charged_usd += self._cost(price, tokens_in, 0)

    # ------------------------------------------------------------------ reporting
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            reserved = sum(r for r, _, _ in self._in_flight.values())
            return {
                "max_llm_calls": self.budget.max_llm_calls,
                "max_cost_usd": self.budget.max_cost_usd,
                "llm_calls_started": self.calls_started,
                "llm_calls_refused": self.refused,
                "llm_calls_in_flight": len(self._in_flight),
                "budget_cost_usd": round(self.charged_usd, 6),
                "reserved_in_flight_usd": round(reserved, 6),
                "failed_calls": self.failed_calls,
                "calls_without_usage": self.calls_without_usage,
                "unpriced_calls": self.unpriced_calls,
                "exhausted": self.exhausted is not None,
                "reason": self.exhausted,
                "basis": ("conservative budget estimate: reported usage where available; a reservation of "
                          f"prompt/{CHARS_PER_TOKEN} input + {OUTPUT_RESERVE_TOKENS} output tokens otherwise; "
                          "unpriced models at the highest known price. Not the provider's bill."),
            }


def describe_snapshot(snapshot: dict | None) -> str:
    """One line for people: what the run used against its limits."""
    if not snapshot:
        return "Per-run limits: off"
    calls = f"{snapshot['llm_calls_started']}"
    if snapshot.get("max_llm_calls") is not None:
        calls += f" of {snapshot['max_llm_calls']}"
    cost = f"${snapshot['budget_cost_usd']:.6f}"
    if snapshot.get("max_cost_usd") is not None:
        cost += f" of ${snapshot['max_cost_usd']:.2f}"
    line = f"Per-run limits: {calls} LLM calls; {cost} budget estimate"
    extras = [f"{snapshot[k]} {label}" for k, label in (("llm_calls_refused", "refused"),
                                                        ("unpriced_calls", "unpriced"),
                                                        ("calls_without_usage", "without usage"))
              if snapshot.get(k)]
    if extras:
        line += " (" + ", ".join(extras) + ")"
    if snapshot.get("exhausted"):
        line += f". Stopped: {snapshot['reason']}"
    return line
