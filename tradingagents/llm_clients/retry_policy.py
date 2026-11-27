"""Provider-aware retry settings: what ``max_retries`` means for each SDK, and how its retries are seen.

The same number means different things per SDK (checked against the installed versions):

- Google Gemini (langchain-google-genai 4.5 -> google-genai 3.3): ``max_retries`` becomes
  ``HttpRetryOptions(attempts=...)``, the number of ATTEMPTS including the first request; 0 is treated
  as 1. Retries 408/429/500/502/503/504 and transient network errors, waiting 1 s doubling to 60 s plus
  up to 1 s of jitter, and logs each retry at INFO on ``google_genai._api_client`` ("Retrying ...").
- OpenAI and OpenAI-compatible endpoints, and Azure (openai 3.51): ADDITIONAL retries after the first
  request (default 2), 0.5 s up to 8 s backoff; each retry logged at INFO on ``openai._base_client``
  ("Retrying request in ...").
- Anthropic (anthropic 2.8): ADDITIONAL retries (default 2), 0.5 s up to 8 s; logged at INFO on
  ``anthropic._base_client`` ("Retrying request to ...").
- Bedrock and anything else: the integration is not installed here, so its semantics are not assumed and
  its retries are reported as unavailable.

``max_retries_value(provider, retries)`` turns "this many retries after the first request" into the value
that provider's client expects, so one provider's convention is never applied to another.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    family: str                  # google | openai | anthropic | unknown
    counts: str                  # "attempts" (includes the first request) | "retries" | "unknown"
    logger: str | None           # where the SDK logs each retry, if it does
    message_prefix: str | None   # how that log message starts


GOOGLE = RetryPolicy("google", "attempts", "google_genai._api_client", "Retrying")
OPENAI = RetryPolicy("openai", "retries", "openai._base_client", "Retrying request")
ANTHROPIC = RetryPolicy("anthropic", "retries", "anthropic._base_client", "Retrying request")
UNKNOWN = RetryPolicy("unknown", "unknown", None, None)
KNOWN = (GOOGLE, OPENAI, ANTHROPIC)


def policy_for(provider: str | None) -> RetryPolicy:
    name = (provider or "").lower()
    if name == "google":
        return GOOGLE
    if name == "anthropic":
        return ANTHROPIC
    if name == "bedrock":
        return UNKNOWN
    if name == "azure":
        return OPENAI
    from .openai_client import is_openai_compatible

    return OPENAI if is_openai_compatible(name) else UNKNOWN


def max_retries_value(provider: str | None, retries: int) -> int | None:
    """The client setting for ``retries`` retries after the first request, or None if unknown."""
    if retries < 0:
        raise ValueError("retries must be >= 0")
    policy = policy_for(provider)
    if policy.counts == "attempts":
        return retries + 1
    if policy.counts == "retries":
        return retries
    return None


def tier_provider(config: dict, tier: str) -> str:
    """The provider a tier's models use: its own override, else the run's provider."""
    return (config.get(f"{tier}_think_provider") or config.get("llm_provider") or "").lower()
