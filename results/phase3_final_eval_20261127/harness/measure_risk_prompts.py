"""Measure the debate prompts on the REAL text of the live F1 run 20261127-111852-5a8696f8, before vs after the
risk-debate dedup. Offline: no model is called. Each debater is given a fake model that answers with that
debater's recorded live argument, so the debate state evolves exactly as it did live, and every prompt is captured.

usage: measure_risk_prompts.py TREE LABEL OUT_JSONL
TREE is a source tree (the HEAD export = before; the working tree = after).
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

tree, label, out = sys.argv[1:4]
sys.path.insert(0, tree)
RECORD = Path(__file__).resolve().parents[1] / "08_live_f1_analysis" / "run_record.json"
rec = json.loads(RECORD.read_text())
rep = rec["reports"]

from tradingagents.agents.researchers.bear_researcher import create_bear_researcher  # noqa: E402
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher  # noqa: E402
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator  # noqa: E402
from tradingagents.agents.risk_mgmt.conservative_debator import create_conservative_debator  # noqa: E402
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator  # noqa: E402

import tradingagents  # noqa: E402

assert str(Path(tradingagents.__file__).resolve()).startswith(str(Path(tree).resolve())), tradingagents.__file__


def recorded(history: str, prefix: str) -> str:
    """The live argument text (one round at shallow depth), without its speaker prefix."""
    text = history.strip()
    assert text.startswith(prefix), (prefix, text[:40])
    return text[len(prefix):].strip()


state = {"company_of_interest": rec["ticker"], "asset_type": rec["asset_type"], "trade_date": rec["trade_date"],
         "market_report": rep["analyst_reports"]["market"], "sentiment_report": rep["analyst_reports"]["sentiment"],
         "news_report": rep["analyst_reports"]["news"], "fundamentals_report": rep["analyst_reports"]["fundamentals"],
         "trader_investment_plan": rep["trader_plan"], "investment_plan": rep["investment_plan"]}
prompts = []


def llm(agent, answer):
    m = MagicMock()
    m.invoke.side_effect = lambda p: prompts.append({"agent": agent, "chars": len(p), "words": len(p.split()),
                                                     "prompt": p}) or MagicMock(content=answer)
    return m


inv = {"history": "", "bull_history": "", "bear_history": "", "current_response": "", "count": 0}
for name, factory, key in (("Bull Researcher", create_bull_researcher, "bull"),
                           ("Bear Researcher", create_bear_researcher, "bear")):
    answer = recorded(rep["investment_debate"][key], f"{key.title()} Analyst:")
    inv = factory(llm(name, answer))({**state, "investment_debate_state": inv})["investment_debate_state"]

risk = {"history": "", "aggressive_history": "", "conservative_history": "", "neutral_history": "",
        "latest_speaker": "", "current_aggressive_response": "", "current_conservative_response": "",
        "current_neutral_response": "", "count": 0}
for name, factory, key in (("Aggressive Analyst", create_aggressive_debator, "aggressive"),
                           ("Conservative Analyst", create_conservative_debator, "conservative"),
                           ("Neutral Analyst", create_neutral_debator, "neutral")):
    answer = recorded(rep["risk_debate"][key], f"{key.title()} Analyst:")
    risk = factory(llm(name, answer))({**state, "risk_debate_state": risk})["risk_debate_state"]

with open(out, "a") as handle:
    handle.write(json.dumps({"label": label, "calls": prompts, "final_risk_state": risk,
                             "final_investment_state": inv}) + "\n")
print("ROW", label, [(p["agent"], p["chars"]) for p in prompts])
