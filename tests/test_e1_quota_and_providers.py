"""Phase 2 limitations 1 and 2, offline: quota exhaustion is told apart from rate limiting and handled
without retrying a model that cannot recover; and a tier's backup may run on a second provider."""

from __future__ import annotations

import math

import pytest
from langchain_core.messages import HumanMessage

from tests.test_e1_breaker import Overloaded, _build
from tests.test_e1_e2_e3_audit import Named
from tests.test_failed_run_reporting import _setup
from tests.test_graph_end_to_end import _Client, offline  # noqa: F401
from tradingagents import e1_routing, mcp_server, service
from tradingagents.llm_clients.provider_errors import classify, hint_for
from tradingagents.run_budget import RunBudget

# The shape of google-genai's ClientError text for each quota (QuotaFailure + RetryInfo details).
GEMINI_DAILY = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota, please check "
    "your plan and billing details.\\n* Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests, limit: 20, model: gemini-3.5-flash-lite\\nPlease retry in 41.63s.', "
    "'status': 'RESOURCE_EXHAUSTED', 'details': [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', "
    "'violations': [{'quotaMetric': 'generativelanguage.googleapis.com/generate_content_free_tier_requests', "
    "'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier', 'quotaValue': '20'}]}, "
    "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '41s'}]}}")
GEMINI_MINUTE = GEMINI_DAILY.replace("PerDay", "PerMinute").replace("41.63s", "7.5s").replace("'41s'", "'7s'")
OPENAI_BILLING = ("Error code: 429 - {'error': {'message': 'You exceeded your current quota, please check your plan "
                  "and billing details.', 'type': 'insufficient_quota', 'code': 'insufficient_quota'}}")
OPENAI_RATE = ("Error code: 429 - {'error': {'message': 'Rate limit reached for gpt-4.1-mini on requests per min "
               "(RPM): Limit 3.', 'type': 'requests', 'code': 'rate_limit_exceeded'}}")
ANTHROPIC_CREDIT = ("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': "
                    "'Your credit balance is too low to access the Anthropic API.'}}")
GEMINI_404 = ("404 NOT_FOUND. {'error': {'code': 404, 'message': 'models/gemini-nope is not found for API version "
              "v1beta, or is not supported for generateContent.', 'status': 'NOT_FOUND'}}")
GEMINI_503 = ("503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high "
              "demand.', 'status': 'UNAVAILABLE'}}")


# ------------------------------------------------------------------ 1. classification
@pytest.mark.unit
@pytest.mark.parametrize("text, kind, retry, quota", [
    (GEMINI_DAILY, "quota_exhausted", 41.0, "GenerateRequestsPerDayPerProjectPerModel-FreeTier"),
    (GEMINI_MINUTE, "rate_limited", 7.0, "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"),
    (OPENAI_BILLING, "quota_exhausted", None, None),
    (OPENAI_RATE, "rate_limited", None, None),
    (ANTHROPIC_CREDIT, "quota_exhausted", None, None),
    ("429 RESOURCE_EXHAUSTED", "rate_limited", None, None),       # no detail: assume it clears
    (GEMINI_503, "unavailable", None, None),
])
def test_provider_errors_are_classified_from_what_the_provider_says(text, kind, retry, quota):
    found = classify(RuntimeError(text))
    assert (found.kind, found.retry_after_seconds, found.quota_id) == (kind, retry, quota)


@pytest.mark.unit
def test_non_overloads_are_not_classified_and_get_their_own_category():
    assert classify(RuntimeError(GEMINI_404)) is None
    assert classify(RuntimeError("400 INVALID_ARGUMENT: bad schema")) is None
    assert service.error_category(RuntimeError(GEMINI_404)) == "model_not_found"
    assert service.error_category(RuntimeError("401 UNAUTHENTICATED: API key not valid")) == "auth"
    assert service.error_category(RuntimeError(GEMINI_DAILY)) == "quota_exhausted"
    assert service.error_category(RuntimeError(GEMINI_MINUTE)) == "rate_limited"
    assert service.error_category(RuntimeError(GEMINI_503)) == "provider_unavailable"


@pytest.mark.unit
def test_a_retry_after_header_is_used_when_the_sdk_exposes_the_response():
    error = RuntimeError("Error code: 429 - rate limited")
    error.response = type("R", (), {"headers": {"retry-after": "12"}})()
    assert classify(error).retry_after_seconds == 12.0


@pytest.mark.unit
def test_failures_come_with_actionable_advice():
    assert "quota is used up" in service.failure_hint(RuntimeError(GEMINI_DAILY))
    assert "wait 7 s" in service.failure_hint(RuntimeError(GEMINI_MINUTE))
    assert "model list" in service.failure_hint(RuntimeError(GEMINI_404))
    assert service.failure_hint(RuntimeError("something odd")) is None
    assert hint_for("provider_unavailable") and hint_for("auth")
    details = mcp_server.failure_details(RuntimeError(GEMINI_DAILY), "r1")
    assert details["category"] == "quota_exhausted" and "quota is used up" in details["hint"]
    assert "what to do: The model's quota" in mcp_server.failure_message(RuntimeError(GEMINI_DAILY), "r1")


# ------------------------------------------------------------------ 1. reaction
@pytest.mark.unit
def test_a_used_up_quota_opens_the_cli_breaker_for_the_rest_of_the_run(tmp_path, monkeypatch):
    main = Overloaded(model_name="main-m", calls_made=[], message=GEMINI_DAILY)
    config = _build(tmp_path, monkeypatch, main, Named(model_name="backup-m"), seconds=None, scope="q-1")
    model = e1_routing.build_model(config, "quick")                    # the CLI: no e1_breaker_seconds
    for _ in range(4):
        model.invoke([HumanMessage("hi")])
    assert len(main.calls_made) == 1                                   # not retried on every step
    assert model.breaker.open_until == math.inf
    log = (tmp_path / "e1_fallback_log.txt").read_text()
    assert "quota exhausted, quota GenerateRequestsPerDay" in log and "for the rest of this run" in log
    assert e1_routing.release_breakers("q-1") == {"trips": 1, "skipped_calls": 3}


@pytest.mark.unit
def test_a_short_overload_keeps_phase_2_behaviour_in_the_cli(tmp_path, monkeypatch):
    for scope, message in (("q-2", GEMINI_MINUTE), ("q-3", GEMINI_503)):
        main = Overloaded(model_name="main-m", calls_made=[], message=message)
        config = _build(tmp_path / scope, monkeypatch, main, Named(model_name="backup-m"), seconds=None, scope=scope)
        (tmp_path / scope).mkdir()
        model = e1_routing.build_model(config, "quick")
        model.invoke([HumanMessage("hi")])
        model.invoke([HumanMessage("hi")])
        assert len(main.calls_made) == 2
        e1_routing.release_breakers(scope)


@pytest.mark.unit
def test_an_interactive_breaker_honours_a_longer_provider_retry_delay(tmp_path, monkeypatch):
    slow = GEMINI_MINUTE.replace("'7s'", "'90s'").replace("7.5s", "90s")
    main = Overloaded(model_name="main-m", calls_made=[], message=slow)
    config = _build(tmp_path, monkeypatch, main, Named(model_name="backup-m"), seconds=60.0, scope="q-4")
    model = e1_routing.build_model(config, "quick")
    model.invoke([HumanMessage("hi")])
    assert "for 90 s" in (tmp_path / "e1_fallback_log.txt").read_text()        # 90 > the 60 s cooldown
    e1_routing.release_breakers("q-4")


@pytest.mark.unit
def test_a_cli_run_with_a_used_up_quota_completes_on_the_backup_and_says_so(tmp_path, monkeypatch, offline):  # noqa: F811
    request = _setup(tmp_path, monkeypatch, main=Overloaded(model_name="main-m", calls_made=[], message=GEMINI_DAILY),
                     backup=Named(model_name="backup-m"), budget=RunBudget(15, 0.04))
    config = dict(request.config, e1_breaker_seconds=None)             # as the CLI: quota-only breaker
    result = service.run_analysis(request.__class__(**{**request.__dict__, "config": config}))
    assert result.usage["llm_calls"] == 11 and result.usage["e1_breaker"] == {"trips": 1, "skipped_calls": 9}
    counts = e1_routing.summarize_events(result.fallback_events)
    assert counts["switches"] == 1 and counts["breaker_skips"] == 9 and counts["backup_calls"] == 10


# ------------------------------------------------------------------ 2. a backup on a second provider
def _capture(monkeypatch, models):
    built = []

    def create(cfg, tier, **k):
        built.append(cfg)
        return _Client(models[cfg[f"{tier}_think_llm"]].model_copy(update={"callbacks": k.get("callbacks")}))

    monkeypatch.setattr(e1_routing, "create_tier_client", create)
    return built


def _cross_config(tmp_path, **extra):
    return {"llm_provider": "google", "quick_think_llm": "main-m", "deep_think_llm": "main-m",
            "quick_fallback_llm": "claude-backup", "quick_fallback_provider": "anthropic",
            "quick_fallback_backend_url": "https://example.invalid/anthropic", "llm_backup_retries": 1,
            "results_dir": str(tmp_path), **extra}


@pytest.mark.unit
def test_a_backup_on_another_provider_is_built_with_that_providers_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-key-not-real")
    main = Overloaded(model_name="main-m", calls_made=[], message=GEMINI_503)
    built = _capture(monkeypatch, {"main-m": main, "claude-backup": Named(model_name="claude-backup")})
    model = e1_routing.build_model(_cross_config(tmp_path), "quick")
    main_cfg, backup_cfg = built
    assert main_cfg.get("quick_think_provider") is None and main_cfg["llm_max_retries"] == 1     # Gemini attempts
    assert backup_cfg["quick_think_provider"] == "anthropic"
    assert backup_cfg["quick_think_backend_url"] == "https://example.invalid/anthropic"
    assert backup_cfg["llm_max_retries"] == 1                     # Anthropic counts retries (Gemini would be 2)
    answer = model.invoke([HumanMessage("hi")])                   # Gemini outage: the other provider answers
    assert answer.content and len(main.calls_made) == 1
    assert "-> switching to claude-backup (anthropic)" in (tmp_path / "e1_fallback_log.txt").read_text()


@pytest.mark.unit
def test_an_unusable_second_provider_is_refused_before_any_call(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _capture(monkeypatch, {"main-m": Named(model_name="main-m"), "claude-backup": Named(model_name="claude-backup")})
    with pytest.raises(e1_routing.FallbackConfigError, match="ANTHROPIC_API_KEY is not set"):
        e1_routing.build_model(_cross_config(tmp_path), "quick")
    with pytest.raises(e1_routing.FallbackConfigError, match="not a supported provider"):
        e1_routing.build_model(_cross_config(tmp_path, quick_fallback_provider="acme-ai"), "quick")
    assert service.error_category(e1_routing.FallbackConfigError("x")) == "configuration"


@pytest.mark.unit
def test_backup_warnings_for_an_unlisted_or_unpriced_model(tmp_path, monkeypatch):
    pricing = tmp_path / "pricing.json"
    pricing.write_text('{"main-m": {"input_per_million_usd": 1, "output_per_million_usd": 1}}')
    monkeypatch.setenv("E3_PRICING_JSON", str(pricing))
    checked = e1_routing.check_backup(_cross_config(tmp_path), "quick", environ={"ANTHROPIC_API_KEY": "k"})
    assert checked["errors"] == []
    assert any("not in the anthropic model catalogue" in w for w in checked["warnings"])
    assert any("has no E3 price" in w for w in checked["warnings"])
    # Nothing configured: nothing to check, and the same provider needs no second key.
    assert e1_routing.check_backup({"quick_think_llm": "m"}, "quick") == {"errors": [], "warnings": []}
    same = {"llm_provider": "google", "quick_fallback_llm": "gemini-3.1-flash-lite"}
    assert e1_routing.check_backup(same, "quick", environ={"GOOGLE_API_KEY": "k"})["errors"] == []


@pytest.mark.unit
def test_a_second_provider_is_never_chosen_automatically():
    from tradingagents.default_config import DEFAULT_CONFIG

    for tier in ("quick", "deep"):
        assert DEFAULT_CONFIG[f"{tier}_fallback_provider"] is None
        assert e1_routing.backup_provider({"llm_provider": "google", f"{tier}_fallback_llm": "x"}, tier) == "google"
