"""E1 tests: routing table + backup model. Offline, no API calls."""
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from tradingagents import e1_routing
from tradingagents.e1_routing import ROUTING_TABLE, build_model, pick_model


class BrokenModel(FakeListChatModel):
    """A model that always fails, like Gemini returning 429."""

    def _call(self, *args, **kwargs):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")


class _Client:
    def __init__(self, llm):
        self.llm = llm

    def get_llm(self):
        return self.llm


def _fake_clients(monkeypatch, main, backup, seen):
    """Hand out `main` for the main model name and `backup` for the backup name."""
    def fake_create(config, tier, **kwargs):
        seen.append(config[f"{tier}_think_llm"])
        llm = main if config[f"{tier}_think_llm"] == "main-model" else backup
        llm.callbacks = kwargs.get("callbacks")  # the real clients attach callbacks too
        return _Client(llm)
    monkeypatch.setattr(e1_routing, "create_tier_client", fake_create)


def test_every_agent_has_a_tier():
    assert len(ROUTING_TABLE) == 12
    assert set(ROUTING_TABLE.values()) == {"quick", "deep"}
    assert pick_model("portfolio_manager", "Q", "D") == "D"
    assert pick_model("market_analyst", "Q", "D") == "Q"


def test_backup_answers_when_main_model_fails(monkeypatch, tmp_path):
    seen = []
    _fake_clients(monkeypatch, BrokenModel(responses=["x"]),
                  FakeListChatModel(responses=["backup answer"]), seen)
    config = {"quick_think_llm": "main-model", "quick_fallback_llm": "backup-model",
              "results_dir": str(tmp_path), "llm_max_retries": 6}

    llm = build_model(config, "quick")

    assert llm.invoke("hi").content == "backup answer"
    assert seen == ["main-model", "backup-model"]
    assert "switching to backup-model" in (tmp_path / "e1_fallback_log.txt").read_text()


def test_no_backup_means_original_behavior(monkeypatch, tmp_path):
    seen = []
    main = FakeListChatModel(responses=["main answer"])
    _fake_clients(monkeypatch, main, None, seen)
    config = {"quick_think_llm": "main-model", "results_dir": str(tmp_path)}

    assert build_model(config, "quick") is main
    assert seen == ["main-model"]


def test_backup_failure_is_logged(monkeypatch, tmp_path):
    seen = []
    _fake_clients(monkeypatch, BrokenModel(responses=["x"]), BrokenModel(responses=["y"]), seen)
    config = {"quick_think_llm": "main-model", "quick_fallback_llm": "backup-model",
              "results_dir": str(tmp_path)}

    with pytest.raises(RuntimeError):
        build_model(config, "quick").invoke("hi")
    assert "BACKUP backup-model also failed" in (tmp_path / "e1_fallback_log.txt").read_text()


def test_timeout_triggers_backup(monkeypatch, tmp_path):
    """A primary-model timeout must allow E1 to use the backup."""

    class TimedOutModel(FakeListChatModel):
        def _call(self, *args, **kwargs):
            raise TimeoutError("Simulated Gemini request timeout")

    seen = []

    _fake_clients(
        monkeypatch,
        TimedOutModel(responses=["unused"]),
        FakeListChatModel(responses=["backup succeeded"]),
        seen,
    )

    config = {
        "quick_think_llm": "main-model",
        "quick_fallback_llm": "backup-model",
        "results_dir": str(tmp_path),
        "llm_timeout": 15.0,
    }

    llm = build_model(config, "quick")
    result = llm.invoke("Analyze MSFT")

    assert result.content == "backup succeeded"
    assert seen == ["main-model", "backup-model"]

    log = (tmp_path / "e1_fallback_log.txt").read_text()
    assert "switching to backup-model" in log


def test_backup_receives_configured_retry_limit(monkeypatch, tmp_path):
    """E1 passes independent retry settings to primary and backup."""
    captured = []

    def fake_create(config, tier, **kwargs):
        captured.append({
            "model": config[f"{tier}_think_llm"],
            "retries": config.get("llm_max_retries"),
        })
        return _Client(FakeListChatModel(responses=["ok"]))

    monkeypatch.setattr(e1_routing, "create_tier_client", fake_create)

    config = {
        "quick_think_llm": "main-model",
        "quick_fallback_llm": "backup-model",
        "results_dir": str(tmp_path),
        "llm_max_retries": 6,
        "llm_backup_max_retries": 0,
    }

    build_model(config, "quick")

    assert captured == [
        {"model": "main-model", "retries": 0},
        {"model": "backup-model", "retries": 0},
    ]


def test_primary_429_then_backup_503_then_recovery(monkeypatch, tmp_path):
    """Simulate SDK retry behavior: primary 429, backup 503, then success."""
    attempts = {"primary": 0, "backup": 0}
    configurations = []

    def fake_create(config, tier, **kwargs):
        model_name = config[f"{tier}_think_llm"]
        retry_limit = config["llm_max_retries"]
        configurations.append((model_name, retry_limit))

        class SimulatedModel(FakeListChatModel):
            def _call(self, messages, stop=None, run_manager=None, **kw):
                if model_name == "main-model":
                    attempts["primary"] += 1
                    raise RuntimeError("429 RESOURCE_EXHAUSTED")

                # Simulate the provider SDK applying its retry budget.
                for attempt in range(retry_limit + 1):
                    attempts["backup"] += 1
                    if attempt == 0:
                        if retry_limit == 0:
                            raise RuntimeError("503 UNAVAILABLE")
                        continue
                    return "Backup successfully recovered"

                raise RuntimeError("503 UNAVAILABLE")

        return _Client(SimulatedModel(responses=["unused"]))

    monkeypatch.setattr(e1_routing, "create_tier_client", fake_create)

    config = {
        "quick_think_llm": "main-model",
        "quick_fallback_llm": "backup-model",
        "results_dir": str(tmp_path),
        "llm_max_retries": 6,
        "llm_backup_max_retries": 1,
    }

    result = build_model(config, "quick").invoke("Analyze NVDA")

    assert result.content == "Backup successfully recovered"
    assert attempts == {"primary": 1, "backup": 2}
    assert configurations == [
        ("main-model", 0),
        ("backup-model", 1),
    ]
