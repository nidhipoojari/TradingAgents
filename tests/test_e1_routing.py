"""E1 tests: routing table + backup model. Offline, no API calls."""
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
