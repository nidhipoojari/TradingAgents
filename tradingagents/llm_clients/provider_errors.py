"""What kind of overload a provider error is, from what the provider actually says (Phase 3).

Three kinds, because each calls for a different reaction:

- ``quota_exhausted``: a quota that will not come back within this run, i.e. a per-day or per-month quota
  (Gemini's ``QuotaFailure`` with a ``...PerDay...`` quota ID), OpenAI's ``insufficient_quota`` (billing), or
  Anthropic's "credit balance is too low". Retrying the same model is pointless: E1 sends the rest of the
  run's calls for that model to the backup, and the error says what to change.
- ``rate_limited``: any other 429 / ``RESOURCE_EXHAUSTED`` (a per-minute limit, or no detail given). It
  clears by itself; the provider's own retry delay is used when it gives one.
- ``unavailable``: 500/502/503/504, ``UNAVAILABLE``, "overloaded".

No quota size is assumed (free-tier limits differ by model and change): only the provider's own words are
used. ``retry_after_seconds`` comes from Gemini's ``RetryInfo.retryDelay`` / "Please retry in N s", or a
``Retry-After`` header when the SDK exposes the response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_RETRY_PATTERNS = (
    re.compile(r"""retryDelay['"]?\s*:\s*['"]?(\d+(?:\.\d+)?)s"""),
    re.compile(r"[Pp]lease retry in (\d+(?:\.\d+)?)\s*s"),
    re.compile(r"(?i)retry[- ]after['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)"),
)
_QUOTA_ID = re.compile(r"""quotaId['"]?\s*:\s*['"]([\w.\-]+)""")
_LONG_QUOTA_ID = re.compile(r"Per(Day|Month)", re.IGNORECASE)
_LONG_QUOTA_TEXT = re.compile(r"\b(per[ -]?day|daily|per[ -]?month|monthly)\b.{0,40}\b(quota|limit)\b|"
                              r"\b(quota|limit)\b.{0,40}\b(per[ -]?day|daily|per[ -]?month|monthly)\b",
                              re.IGNORECASE)
_BILLING = re.compile(r"insufficient_quota|credit balance is too low|exceeded your current quota, please check "
                      r"your plan and billing", re.IGNORECASE)
_RATE = re.compile(r"\b429\b|RESOURCE_EXHAUSTED|rate[_ ]limit", re.IGNORECASE)
_UNAVAILABLE = re.compile(r"\b50[0234]\b|\bUNAVAILABLE\b|overloaded", re.IGNORECASE)


@dataclass(frozen=True)
class Overload:
    kind: str                           # quota_exhausted | rate_limited | unavailable
    retry_after_seconds: float | None = None
    quota_id: str | None = None

    def describe(self) -> str:
        parts = [self.kind.replace("_", " ")]
        if self.quota_id:
            parts.append(f"quota {self.quota_id}")
        if self.retry_after_seconds is not None:
            parts.append(f"provider asks to retry in {self.retry_after_seconds:g} s")
        return ", ".join(parts)


def _retry_after(error: BaseException, text: str) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get("retry-after")
            if value is not None:
                return float(value)
        except (TypeError, ValueError, AttributeError):
            pass
    for pattern in _RETRY_PATTERNS:
        found = pattern.search(text)
        if found:
            return float(found.group(1))
    return None


def classify(error: BaseException) -> Overload | None:
    """The overload kind of ``error``, or None when it is not an overload (e.g. 400, 401, 404)."""
    from tradingagents.e3_tracing import error_code

    code = error_code(error)
    text = str(error)[:6000]
    quota_id = (_QUOTA_ID.search(text) or [None, None])[1]
    retry_after = _retry_after(error, text)
    if _BILLING.search(text) and "credit balance" in text.lower():
        return Overload("quota_exhausted", retry_after, quota_id)       # Anthropic: a 400, not a 429
    if code == 429 or _RATE.search(text) or "RateLimit" in type(error).__name__:
        if (quota_id and _LONG_QUOTA_ID.search(quota_id)) or "insufficient_quota" in text \
                or (_LONG_QUOTA_TEXT.search(text) and not (quota_id and "PerMinute" in quota_id)):
            return Overload("quota_exhausted", retry_after, quota_id)
        return Overload("rate_limited", retry_after, quota_id)
    if code in (500, 502, 503, 504) or _UNAVAILABLE.search(text):
        return Overload("unavailable", retry_after, quota_id)
    return None


HINTS = {
    "quota_exhausted": ("The model's quota is used up (a daily, monthly or billing limit), so retrying will not "
                        "help. Wait for the quota to reset, choose another model, or configure an E1 backup on "
                        "another provider (TRADINGAGENTS_QUICK_FALLBACK_PROVIDER / _DEEP_)."),
    "rate_limited": "Too many requests in a short time; the limit clears by itself. Try again shortly.",
    "provider_unavailable": ("The provider is overloaded or down (5xx). It is usually temporary; try again "
                             "later, or configure an E1 backup on another provider."),
    "model_not_found": ("The provider does not know this model name (404). Check TRADINGAGENTS_QUICK_THINK_LLM / "
                        "_DEEP_THINK_LLM and the backups against the provider's current model list."),
    "auth": "The provider refused the credentials (401/403). Check the API key for this provider in .env.",
    "configuration": "The run's configuration is invalid; fix the setting named above and start again.",
}


def hint_for(category: str, error: BaseException | None = None) -> str | None:
    """A sentence on what to do about a failed run of this category, or None."""
    hint = HINTS.get(category)
    if hint and category == "rate_limited" and error is not None:
        overload = classify(error)
        if overload is not None and overload.retry_after_seconds is not None:
            hint += f" The provider asked to wait {overload.retry_after_seconds:g} s."
    return hint
