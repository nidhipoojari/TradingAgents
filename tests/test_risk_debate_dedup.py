"""Risk debate prompts no longer send the latest opponent arguments twice (2026-11-27).

Each risk debator appends its argument to ``history`` and also passes it on as ``current_<speaker>_response``,
so the next speaker received every recent argument twice: in the history and again as "the last response". The
three risk debators now use the same rule as the Bull/Bear researchers (Phase 2), through one shared helper: an
opponent's latest argument is replaced by a short reference only when the history already holds it verbatim.
With an empty or incomplete history, or when the opponent has not spoken yet, it is kept in full.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tradingagents.agents.context import ALREADY_IN_HISTORY, latest_argument_unless_in_history
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from tradingagents.agents.risk_mgmt.conservative_debator import create_conservative_debator
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator

REPORTS = {"company_of_interest": "NVDA", "asset_type": "stock", "market_report": "m", "sentiment_report": "s",
           "news_report": "n", "fundamentals_report": "f", "trader_investment_plan": "plan"}
ARGS = {"Aggressive": "Aggressive Analyst: buy the dip, the upside is large",
        "Conservative": "Conservative Analyst: protect capital, size it small",
        "Neutral": "Neutral Analyst: a measured position balances both"}
FACTORIES = {"Aggressive": create_aggressive_debator, "Conservative": create_conservative_debator,
             "Neutral": create_neutral_debator}


def _llm(prompts: list, speaker: str):
    llm = MagicMock()
    text = ARGS[speaker].split(": ", 1)[1]
    llm.invoke.side_effect = lambda prompt: prompts.append(prompt) or MagicMock(content=text)
    return llm


def _empty_risk_state() -> dict:
    return {"history": "", "aggressive_history": "", "conservative_history": "", "neutral_history": "",
            "latest_speaker": "", "current_aggressive_response": "", "current_conservative_response": "",
            "current_neutral_response": "", "count": 0}


def _round(rounds: int = 1):
    """Run the risk debate in the graph's order (Aggressive, Conservative, Neutral), capturing each prompt."""
    state = {**REPORTS, "risk_debate_state": _empty_risk_state()}
    prompts: dict[str, list] = {s: [] for s in FACTORIES}
    for _ in range(rounds):
        for speaker, factory in FACTORIES.items():
            state["risk_debate_state"] = factory(_llm(prompts[speaker], speaker))(state)["risk_debate_state"]
    return state["risk_debate_state"], prompts


# ------------------------------------------------------------------ the helper
@pytest.mark.unit
def test_an_argument_already_in_the_history_is_referenced_not_repeated():
    history = "\nAggressive Analyst: buy\nConservative Analyst: wait"
    assert latest_argument_unless_in_history("Aggressive Analyst: buy", history) == ALREADY_IN_HISTORY
    assert latest_argument_unless_in_history("  Conservative Analyst: wait \n", history) == ALREADY_IN_HISTORY


@pytest.mark.unit
@pytest.mark.parametrize("history", ["", None, "\nAggressive Analyst: buy the d", "\nNeutral Analyst: other"])
def test_an_argument_missing_from_an_empty_or_incomplete_history_is_kept(history):
    argument = "Aggressive Analyst: buy the dip"
    assert latest_argument_unless_in_history(argument, history) == argument


@pytest.mark.unit
def test_an_opening_marker_or_empty_argument_is_never_replaced():
    marker = "(The neutral analyst has not spoken yet — open the debate with your own case.)"
    assert latest_argument_unless_in_history(marker, "\nAggressive Analyst: buy") == marker
    assert latest_argument_unless_in_history("", "") == ""          # "" is in every string; still not a duplicate


# ------------------------------------------------------------------ the three risk debators
@pytest.mark.unit
def test_one_round_sends_each_argument_once_and_keeps_opening_markers():
    _, prompts = _round()
    aggressive, conservative, neutral = (prompts[s][0] for s in FACTORIES)
    # The opening speaker has nobody to answer: both markers, nothing replaced.
    assert aggressive.count("has not spoken yet") == 2 and ALREADY_IN_HISTORY not in aggressive
    # Conservative: Aggressive's argument once (in the history), the neutral marker kept.
    assert conservative.count(ARGS["Aggressive"]) == 1 and conservative.count(ALREADY_IN_HISTORY) == 1
    assert "neutral analyst has not spoken yet" in conservative
    # Neutral: both arguments once each.
    assert neutral.count(ARGS["Aggressive"]) == 1 and neutral.count(ARGS["Conservative"]) == 1
    assert neutral.count(ALREADY_IN_HISTORY) == 2


@pytest.mark.unit
def test_a_second_round_still_sends_every_argument_once():
    _, prompts = _round(rounds=2)
    second_aggressive = prompts["Aggressive"][1]
    assert second_aggressive.count(ARGS["Conservative"]) == 1 and second_aggressive.count(ARGS["Neutral"]) == 1
    assert second_aggressive.count(ARGS["Aggressive"]) == 1        # its own earlier argument, in the history
    assert second_aggressive.count(ALREADY_IN_HISTORY) == 2


@pytest.mark.unit
@pytest.mark.parametrize("speaker", list(FACTORIES))
def test_a_response_missing_from_the_history_is_still_sent_in_full(speaker):
    """State whose history is empty or lost (e.g. resumed or edited): the opponents' arguments must reach the
    model in full, not as a reference to a history that does not hold them."""
    risk = {**_empty_risk_state(), "current_aggressive_response": ARGS["Aggressive"],
            "current_conservative_response": ARGS["Conservative"], "current_neutral_response": ARGS["Neutral"],
            "history": "\nUnrelated earlier text"}
    prompts: list = []
    FACTORIES[speaker](_llm(prompts, speaker))({**REPORTS, "risk_debate_state": risk})
    for other in FACTORIES:
        if other != speaker:
            assert prompts[0].count(ARGS[other]) == 1
    assert ALREADY_IN_HISTORY not in prompts[0]


@pytest.mark.unit
def test_state_updates_rounds_and_all_three_perspectives_are_unchanged():
    risk, _ = _round(rounds=2)
    assert risk["count"] == 6 and risk["latest_speaker"] == "Neutral"
    assert risk["history"] == "".join("\n" + ARGS[s] for s in FACTORIES) * 2
    for speaker in FACTORIES:
        assert risk[f"{speaker.lower()}_history"] == ("\n" + ARGS[speaker]) * 2
        assert risk[f"current_{speaker.lower()}_response"] == ARGS[speaker]   # raw text, never the reference


# ------------------------------------------------------------------ Bull/Bear keep their Phase 2 behaviour
@pytest.mark.unit
@pytest.mark.parametrize("factory,opponent", [(create_bull_researcher, "Bear"), (create_bear_researcher, "Bull")])
def test_researchers_keep_their_dedup_through_the_shared_helper(factory, opponent):
    argument = f"{opponent} Analyst: a point"
    for history, expect_full in ((f"\n{argument}", False), ("", True)):
        prompts: list = []
        llm = MagicMock()
        llm.invoke.side_effect = lambda p, _ps=prompts: _ps.append(p) or MagicMock(content="reply")
        state = {**REPORTS, "investment_debate_state": {"history": history, "bull_history": "", "bear_history": "",
                                                        "current_response": argument, "count": 0}}
        factory(llm)(state)
        assert prompts[0].count(argument) == 1
        assert (ALREADY_IN_HISTORY in prompts[0]) is (not expect_full)
