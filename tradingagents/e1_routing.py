"""E1: Task-based model routing with a backup model.

Two simple ideas:

1. ROUTING TABLE: every agent is given a model tier.
     "quick" = cheaper, faster model (gathering data, arguing one side)
     "deep"  = stronger model (the two final judgment calls)
   The split was checked against E3's per-agent measurements
   (results/phase2/benchmarks.csv): the analysts and debaters use the most
   tokens, so they stay on the cheap tier.

2. BACKUP MODEL: if the main model fails (rate limit 429, server error,
   model not found), the same request is sent to a backup model, so the
   run finishes instead of crashing. Every switch is written to
   e1_fallback_log.txt, and E3 records the failed call as an error.

Turn the backup on in .env (leave empty = no backup, same as before):
    TRADINGAGENTS_QUICK_FALLBACK_LLM=<backup model for the quick tier>
    TRADINGAGENTS_DEEP_FALLBACK_LLM=<backup model for the deep tier>
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables.fallbacks import RunnableWithFallbacks

from tradingagents.llm_clients import create_tier_client

logger = logging.getLogger(__name__)


# 1. Routing table: agent -> model tier ("quick" or "deep")
ROUTING_TABLE = {
    "market_analyst": "quick",
    "social_analyst": "quick",
    "news_analyst": "quick",
    "fundamentals_analyst": "quick",
    "bull_researcher": "quick",
    "bear_researcher": "quick",
    "research_manager": "deep",
    "trader": "quick",
    "aggressive_analyst": "quick",
    "neutral_analyst": "quick",
    "conservative_analyst": "quick",
    "portfolio_manager": "deep",
}

# When a backup exists, try the main model only this many extra times
# before switching (the default is 6 retries, which wastes minutes on a 429).
RETRIES_BEFORE_BACKUP = 0


def pick_model(agent: str, quick_llm, deep_llm):
    """Return the model this agent should use, according to ROUTING_TABLE."""
    tier = ROUTING_TABLE[agent]
    return deep_llm if tier == "deep" else quick_llm


def _log_line(log_file: Path, tier: str, what: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | {tier} tier | {what}"
    logger.warning("E1 fallback: %s", line)
    try:
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # logging must never break the run


# Phase 3: a circuit breaker for interactive runs (dashboard, MCP). After the main model fails with an
# overload error, its next calls go straight to the backup for a cooldown, instead of paying for a
# request that is likely to fail again (2.8 s per call in the live 503 incident). Off unless the run
# config sets e1_breaker_seconds and e1_breaker_scope (the service does, for interactive sources).
_BREAKERS: dict[tuple, CircuitBreaker] = {}
_BREAKERS_LOCK = threading.Lock()


class CircuitBreaker:
    """One per run and main model, shared by the tiers that use it; so it never keeps a tier of its own:
    each log line names the tier of the call that failed or was rerouted."""

    def __init__(self, main_model: str, backup_model: str, cooldown: float | None, log_file: Path):
        self.main_model, self.backup_model, self.cooldown = main_model, backup_model, cooldown
        self.log_file = log_file
        self._lock = threading.Lock()
        self.open_until = 0.0
        self.trips = 0
        self.skipped = 0

    def record_failure(self, error: BaseException, tier: str) -> None:
        """Open the circuit for an overload. A used-up quota (per day/month, billing) opens it for the rest of
        the run, in every source; a rate limit or 5xx opens it for the cooldown (interactive runs only), or
        for the provider's own retry delay when that is longer."""
        from tradingagents.llm_clients.provider_errors import classify

        overload = classify(error)
        if overload is None:
            return                                # e.g. 400/401/404: not a reason to avoid the model
        if overload.kind == "quota_exhausted":
            seconds = math.inf
        elif self.cooldown is None:
            return                                # quota-only breaker (the CLI): short overloads keep Phase 2's per-call fallback
        else:
            seconds = max(self.cooldown, overload.retry_after_seconds or 0.0)
        with self._lock:
            already_open = time.monotonic() < self.open_until
            self.open_until = max(self.open_until, time.monotonic() + seconds)
            self.trips += 0 if already_open else 1
        if not already_open:
            span = "for the rest of this run" if seconds == math.inf else f"for {seconds:g} s"
            _log_line(self.log_file, tier, f"circuit open: {self.main_model} overloaded "
                      f"({type(error).__name__}: {overload.describe()}); its calls go to {self.backup_model} {span}")

    def force_open(self, tier: str, reason: str) -> None:
        """Adaptive routing's demotion: start the run on the backup (logged with the decision's reason)."""
        with self._lock:
            already_open = time.monotonic() < self.open_until
            self.open_until = math.inf
            self.trips += 0 if already_open else 1
        if not already_open:
            _log_line(self.log_file, tier, f"circuit open: adaptive routing ({reason}); {self.main_model}'s calls "
                      f"go to {self.backup_model} for the rest of this run")

    def is_open(self) -> bool:
        return time.monotonic() < self.open_until

    def record_skip(self, tier: str) -> None:
        with self._lock:
            self.skipped += 1
        _log_line(self.log_file, tier, f"{self.main_model} skipped (circuit open) -> {self.backup_model}")


def breaker_for(config: dict, tier: str, main_model: str, backup_model: str, log_file: Path) -> CircuitBreaker | None:
    """The run's breaker for this main model. With ``e1_breaker_seconds`` (dashboard, MCP) it opens on any
    overload; without it (the CLI) it is quota-only: it opens only when a daily/monthly/billing quota is used
    up, which cannot recover within the run. None when the run has no scope (no run ID) or turns it off."""
    seconds, scope = config.get("e1_breaker_seconds"), config.get("e1_breaker_scope")
    if not scope or config.get("e1_quota_breaker") is False and not seconds:
        return None
    from tradingagents.llm_clients.retry_policy import tier_provider

    key = (scope, tier_provider(config, tier), main_model, backup_model)
    with _BREAKERS_LOCK:
        if key not in _BREAKERS:
            _BREAKERS[key] = CircuitBreaker(main_model, backup_model, float(seconds) if seconds else None, log_file)
        return _BREAKERS[key]


def release_breakers(scope: str) -> dict:
    """Forget a finished run's breakers; returns {"trips": n, "skipped_calls": n} for its record."""
    with _BREAKERS_LOCK:
        mine = [key for key in _BREAKERS if key[0] == scope]
        breakers = [_BREAKERS.pop(key) for key in mine]
    return {"trips": sum(b.trips for b in breakers), "skipped_calls": sum(b.skipped for b in breakers)}


class BreakerFallbacks(RunnableWithFallbacks):
    """with_fallbacks, plus: while the breaker is open, call the backup directly. The breaker survives
    bind_tools / with_structured_output (RunnableWithFallbacks rebuilds itself from model_dump())."""

    breaker: Any = None
    tier: str = ""

    def invoke(self, input, config=None, **kwargs):
        if self.breaker is not None and self.breaker.is_open():
            self.breaker.record_skip(self.tier)
            return self.fallbacks[0].invoke(input, config, **kwargs)
        return super().invoke(input, config, **kwargs)

    async def ainvoke(self, input, config=None, **kwargs):
        if self.breaker is not None and self.breaker.is_open():
            self.breaker.record_skip(self.tier)
            return await self.fallbacks[0].ainvoke(input, config, **kwargs)
        return await super().ainvoke(input, config, **kwargs)


def summarize_events(lines: list[str] | None) -> dict[str, int]:
    """Counts by kind for E1 log lines (``e1_fallback_log.txt``, or a run's ``fallback_events``).

    The log holds four kinds of line, and only two of them send a call to the backup model:
      switches        "<main> failed (...) -> switching to <backup>"      a failed call, retried on the backup
      breaker_skips   "<main> skipped (circuit open) -> <backup>"         sent straight to the backup
      circuit_opens   "circuit open: <main> overloaded ..."               a notice, not a call
      backup_failures "BACKUP <backup> also failed (...) -> run stops"    the backup's own failure
    ``backup_calls`` = switches + breaker_skips: the calls that were moved to the backup model.
    """
    counts = {"backup_calls": 0, "switches": 0, "breaker_skips": 0, "circuit_opens": 0, "backup_failures": 0,
              "other": 0}
    for line in lines or []:
        what = line.split(" | ", 2)[-1]           # "<time> | <tier> tier | <what>"; the error text may hold " | "
        if what.startswith("BACKUP ") and " also failed" in what:
            counts["backup_failures"] += 1
        elif what.startswith("circuit open: "):
            counts["circuit_opens"] += 1
        elif " skipped (circuit open) -> " in what:
            counts["breaker_skips"] += 1
        elif " -> switching to " in what:
            counts["switches"] += 1
        else:
            counts["other"] += 1
    counts["backup_calls"] = counts["switches"] + counts["breaker_skips"]
    return counts


def describe_events(lines: list[str] | None) -> str:
    """One line for the dashboard: calls moved to the backup, then what else the log recorded."""
    c = summarize_events(lines)
    if not lines:
        return "none"
    text = str(c["backup_calls"])
    details = []
    if c["switches"] and c["breaker_skips"]:
        details.append(f"{c['switches']} after a failed call, {c['breaker_skips']} skipped by the circuit breaker")
    elif c["breaker_skips"]:
        details.append("skipped by the circuit breaker")
    if c["circuit_opens"]:
        details.append(f"circuit opened {c['circuit_opens']}x")
    if c["backup_failures"]:
        details.append(f"backup failed {c['backup_failures']}x")
    return text + (f" ({'; '.join(details)})" if details else "")


class FallbackLogger(BaseCallbackHandler):
    """Writes one line to the log file each time the main (or backup) model fails."""

    def __init__(self, tier: str, main_model: str, backup_model: str, log_file: Path,
                 is_backup: bool = False, breaker: CircuitBreaker | None = None):
        self.tier = tier
        self.main_model = main_model
        self.backup_model = backup_model
        self.log_file = log_file
        self.is_backup = is_backup
        self.breaker = breaker

    def on_llm_error(self, error: BaseException, **kwargs) -> None:
        reason = f"{type(error).__name__}: {str(error)[:120]}"
        if self.is_backup:
            what = f"BACKUP {self.backup_model} also failed ({reason}) -> run stops"
        else:
            what = f"{self.main_model} failed ({reason}) -> switching to {self.backup_model}"
        _log_line(self.log_file, self.tier, what)
        if self.breaker is not None and not self.is_backup:
            self.breaker.record_failure(error, self.tier)


# 2. Build a tier's model, with a backup if one is configured
class FallbackConfigError(ValueError):
    """The configured backup cannot work (unknown provider, missing credentials); raised before any call."""


def backup_provider(config: dict, tier: str) -> str:
    """The provider the tier's backup model runs on: its own setting, else the tier's provider."""
    from tradingagents.llm_clients.retry_policy import tier_provider

    return (config.get(f"{tier}_fallback_provider") or "").strip().lower() or tier_provider(config, tier)


def check_backup(config: dict, tier: str, environ: dict | None = None) -> dict:
    """Validate a tier's backup before the run: {"errors": [...], "warnings": [...]}.

    Errors (the run is refused): an unknown provider, or a provider whose API-key variable is unset.
    Warnings: a model name the provider's catalogue does not list (it may still exist), a model with no
    E3 price (cost is then estimated conservatively by the run budget and missing from E3's estimate).
    """
    import os

    from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
    from tradingagents.llm_clients.validators import validate_model

    environ = os.environ if environ is None else environ
    model = config.get(f"{tier}_fallback_llm")
    out = {"errors": [], "warnings": []}
    if not model:
        return out
    provider = backup_provider(config, tier)
    if provider not in PROVIDER_API_KEY_ENV:
        out["errors"].append(f"{tier} backup provider {provider!r} is not a supported provider "
                             f"(one of: {', '.join(sorted(PROVIDER_API_KEY_ENV))})")
        return out
    key_env = PROVIDER_API_KEY_ENV[provider]
    if key_env and not (environ.get(key_env) or "").strip():
        out["errors"].append(f"{tier} backup {model!r} runs on {provider}, but {key_env} is not set")
    if not validate_model(provider, model):
        out["warnings"].append(f"{tier} backup model {model!r} is not in the {provider} model catalogue")
    from tradingagents.run_budget import load_pricing

    try:
        prices = load_pricing()          # {} when no pricing file is configured: nothing to check against
    except (OSError, ValueError):
        prices = {}
    if prices and model not in prices:
        out["warnings"].append(f"{tier} backup model {model!r} has no E3 price: its cost is estimated "
                               "conservatively by the run budget and left out of E3's estimate")
    return out


def build_model(config: dict, tier: str, callbacks: list | None = None):
    """Create the "quick" or "deep" model; wrap it with a backup if set in .env."""
    callbacks = list(callbacks or [])
    main_name = config[f"{tier}_think_llm"]
    backup_name = config.get(f"{tier}_fallback_llm")

    # No backup configured: build the model exactly like the original code.
    if not backup_name:
        extra = {"callbacks": callbacks} if callbacks else {}
        return create_tier_client(config, tier, **extra).get_llm()

    from tradingagents.llm_clients.retry_policy import tier_provider

    if backup_provider(config, tier) != tier_provider(config, tier):
        backup_name_in_log = f"{backup_name} ({backup_provider(config, tier)})"
    else:
        backup_name_in_log = backup_name
    # Main model: fewer retries, plus the logger that records each failure.
    log_file = Path(config["results_dir"]) / "e1_fallback_log.txt"
    breaker = breaker_for(config, tier, main_name, backup_name_in_log, log_file)
    prefer_backup = (config.get("e1_prefer_backup") or {}).get(tier)
    if breaker is not None and prefer_backup:
        breaker.force_open(tier, prefer_backup)
    switch_logger = FallbackLogger(tier, main_name, backup_name_in_log, log_file, breaker=breaker)
    # Provider-aware (llm_clients/retry_policy.py): "no retries" is attempts=1 for Gemini, 0 for
    # OpenAI/Anthropic-style clients; unknown providers keep RETRIES_BEFORE_BACKUP as before.
    from tradingagents.llm_clients.retry_policy import max_retries_value

    provider = tier_provider(config, tier)
    no_retries = max_retries_value(provider, RETRIES_BEFORE_BACKUP)
    main_config = {**config, "llm_max_retries": RETRIES_BEFORE_BACKUP if no_retries is None else no_retries}
    main_llm = create_tier_client(main_config, tier, callbacks=callbacks + [switch_logger]).get_llm()

    # Backup model: same settings, different model name; on the tier's provider unless
    # {tier}_fallback_provider names another (Phase 3: a second provider, never chosen automatically).
    # It gets a logger too, so a failing backup shows up in the log file
    # (LangChain re-raises the MAIN model's error when both fail).
    backup_config = {**config, f"{tier}_think_llm": backup_name}
    b_provider = backup_provider(config, tier)
    if b_provider != provider:
        problems = check_backup(config, tier)["errors"]
        if problems:
            raise FallbackConfigError("; ".join(problems))
        backup_config[f"{tier}_think_provider"] = b_provider
        backup_config[f"{tier}_think_backend_url"] = config.get(f"{tier}_fallback_backend_url")
    if config.get("llm_backup_max_retries") is not None:      # the raw client setting (CLI, Phase 2)
        backup_config["llm_max_retries"] = config["llm_backup_max_retries"]
    elif config.get("llm_backup_retries") is not None:          # retries after the first request, any provider
        translated = max_retries_value(b_provider, int(config["llm_backup_retries"]))
        if translated is not None:
            backup_config["llm_max_retries"] = translated
    backup_logger = FallbackLogger(tier, main_name, backup_name_in_log, log_file, is_backup=True)
    backup_llm = create_tier_client(backup_config, tier, callbacks=callbacks + [backup_logger]).get_llm()

    # LangChain's built-in fallback: if main_llm raises an error, call backup_llm.
    if breaker is not None:                   # interactive runs: skip an overloaded primary for a while
        return BreakerFallbacks(runnable=main_llm, fallbacks=[backup_llm], breaker=breaker, tier=tier)
    return main_llm.with_fallbacks([backup_llm])
