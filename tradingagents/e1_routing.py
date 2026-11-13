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
from datetime import datetime
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

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
RETRIES_BEFORE_BACKUP = 1


def pick_model(agent: str, quick_llm, deep_llm):
    """Return the model this agent should use, according to ROUTING_TABLE."""
    tier = ROUTING_TABLE[agent]
    return deep_llm if tier == "deep" else quick_llm


class FallbackLogger(BaseCallbackHandler):
    """Writes one line to the log file each time the main model fails."""

    def __init__(self, tier: str, main_model: str, backup_model: str, log_file: Path):
        self.tier = tier
        self.main_model = main_model
        self.backup_model = backup_model
        self.log_file = log_file

    def on_llm_error(self, error: BaseException, **kwargs) -> None:
        line = (f"{datetime.now():%Y-%m-%d %H:%M:%S} | {self.tier} tier | "
                f"{self.main_model} failed ({type(error).__name__}: {str(error)[:120]}) "
                f"-> switching to {self.backup_model}")
        logger.warning("E1 fallback: %s", line)
        try:
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # logging must never break the run


# 2. Build a tier's model, with a backup if one is configured
def build_model(config: dict, tier: str, callbacks: list | None = None):
    """Create the "quick" or "deep" model; wrap it with a backup if set in .env."""
    callbacks = list(callbacks or [])
    main_name = config[f"{tier}_think_llm"]
    backup_name = config.get(f"{tier}_fallback_llm")

    # No backup configured: build the model exactly like the original code.
    if not backup_name:
        extra = {"callbacks": callbacks} if callbacks else {}
        return create_tier_client(config, tier, **extra).get_llm()

    # Main model: fewer retries, plus the logger that records each failure.
    log_file = Path(config["results_dir"]) / "e1_fallback_log.txt"
    switch_logger = FallbackLogger(tier, main_name, backup_name, log_file)
    main_config = {**config, "llm_max_retries": RETRIES_BEFORE_BACKUP}
    main_llm = create_tier_client(main_config, tier, callbacks=callbacks + [switch_logger]).get_llm()

    # Backup model: same provider and settings, different model name.
    backup_config = {**config, f"{tier}_think_llm": backup_name}
    extra = {"callbacks": callbacks} if callbacks else {}
    backup_llm = create_tier_client(backup_config, tier, **extra).get_llm()

    # LangChain's built-in fallback: if main_llm raises an error, call backup_llm.
    return main_llm.with_fallbacks([backup_llm])
