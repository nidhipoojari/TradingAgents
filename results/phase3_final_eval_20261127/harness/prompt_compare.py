"""Does Phase 3 change any prompt or add any LLM call? Run one tree's analysis sequence (graph mode, as
phase_ab.py) with identical scripted models and offline data, recording every model call's agent and prompt
size (characters of all messages sent). Offline.

usage: prompt_compare.py TREE LABEL ANALYSTS(comma) OUT_JSONL
"""
import copy
import json
import os
import sys
import tempfile
from pathlib import Path


def main():
    tree, label, analysts, out = sys.argv[1:5]
    analysts = analysts.split(",")
    sys.path.insert(0, tree)
    os.chdir(tree)
    work = Path(tempfile.mkdtemp())
    profile = work / "zero.json"
    profile.write_text(json.dumps({"latencies": [0.0], "market_tool_rounds": 1, "data_seconds_per_tool": 0}))
    pricing = work / "pricing.json"
    pricing.write_text(json.dumps({m: {"input_per_million_usd": 0.3, "output_per_million_usd": 2.5}
                                   for m in ("replay-main", "replay-backup")}))
    os.environ.update(LANGFUSE_PUBLIC_KEY="offline", LANGFUSE_SECRET_KEY="offline",
                      LANGFUSE_BASE_URL="http://localhost.invalid", WORKER_FAKE_MARKER=os.devnull,
                      E3_PRICING_JSON=str(pricing), REPLAY_PROFILE=str(profile), E3_TRACE_ENABLED="1",
                      E3_RESULTS_DIR=str(work / "e3"), HTTPS_PROXY="http://127.0.0.1:9")
    from langchain_core.callbacks import BaseCallbackHandler

    from tests import worker_fakes
    from tradingagents.default_config import DEFAULT_CONFIG

    calls = []

    class Prompts(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, *, metadata=None, **kwargs):
            text = "".join(str(m.content) for batch in messages for m in batch)
            calls.append({"agent": (metadata or {}).get("langgraph_node"), "prompt_chars": len(text),
                          "messages": sum(len(b) for b in messages)})

    worker_fakes.replay()
    from cli.stats_handler import StatsCallbackHandler
    from tradingagents.e3_cli_hook import E3Session
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(results_dir=str(work / "results"), memory_log_path=str(work / "m.md"),
                  data_cache_dir=str(work / "cache"), e2_cache_enabled=True, llm_provider="google",
                  quick_think_llm="replay-main", deep_think_llm="replay-main", quick_fallback_llm="replay-backup",
                  deep_fallback_llm="replay-backup", max_debate_rounds=1, max_risk_discuss_rounds=1)
    stats = StatsCallbackHandler()
    session = E3Session.from_env(ticker="NVDA", analysis_date="2026-01-09")
    callbacks = [stats, Prompts()] + session.callbacks
    graph = TradingAgentsGraph(analysts, config=config, debug=False, callbacks=callbacks)
    state = graph.create_run_state("NVDA", "2026-01-09", "stock", None)
    args = graph.propagator.get_graph_args(callbacks=callbacks)
    graph.begin_checkpoint("NVDA", "2026-01-09", "stock", None)
    final = {}
    try:
        for _messages, chunk in graph.stream_run(graph.checkpoint_input(state), **args):
            if chunk is not None:
                final.update(chunk)
        graph.record_decision("NVDA", "2026-01-09", final)
    finally:
        graph.end_checkpoint()
    row = {"label": label, "analysts": analysts, "llm_calls": stats.get_stats()["llm_calls"],
           "tool_calls": stats.get_stats()["tool_calls"], "calls": calls,
           "total_prompt_chars": sum(c["prompt_chars"] for c in calls)}
    with open(out, "a") as handle:
        handle.write(json.dumps(row) + "\n")
    print("ROW", label, ",".join(analysts), row["llm_calls"], row["tool_calls"], row["total_prompt_chars"], flush=True)


if __name__ == "__main__":
    main()
