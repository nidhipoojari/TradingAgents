"""One CLI analysis on the replay, in whichever tree this file is copied to; writes its numbers as JSON."""
import copy, json, os, sys, time, types
from pathlib import Path

sys.path.insert(0, os.environ["BENCH_DIR"])


def test_bench(tmp_path, monkeypatch):
    import replay_fakes
    import cli.display as display, cli.main as cli_main
    from cli import run as cli_run
    from cli.models import AnalystType
    from tradingagents import e3_cli_hook
    from tradingagents.default_config import DEFAULT_CONFIG

    replay_fakes.install()
    for k, v in {"E3_TRACE_ENABLED": "1", "LANGFUSE_PUBLIC_KEY": "x", "LANGFUSE_SECRET_KEY": "x",
                 "LANGFUSE_BASE_URL": "http://localhost.invalid", "E3_RESULTS_DIR": str(tmp_path / "e3"),
                 "E3_PRICING_JSON": os.environ["PRICING"], "DATA_LOG": str(tmp_path / "data.log")}.items():
        monkeypatch.setenv(k, v)
    saves = []
    monkeypatch.setattr(e3_cli_hook, "atexit", types.SimpleNamespace(register=saves.append, unregister=lambda f: None))
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cache = Path(os.environ["CACHE_DIR"]) if os.environ.get("CACHE_DIR") else tmp_path / "c"
    cfg.update(results_dir=str(tmp_path / "r"), data_cache_dir=str(cache), memory_log_path=str(tmp_path / "m.md"),
               quick_think_llm="gemini-3.5-flash-lite", deep_think_llm="gemini-3.5-flash-lite", llm_provider="google",
               max_debate_rounds=1, max_risk_discuss_rounds=1, e2_cache_enabled=os.environ.get("E2") == "1")
    buffer = display.MessageBuffer()
    monkeypatch.setattr(display, "message_buffer", buffer); monkeypatch.setattr(cli_run, "message_buffer", buffer)
    monkeypatch.setattr(cli_run, "Live", type("L", (), {"__init__": lambda s, *a, **k: None, "__enter__": lambda s: s, "__exit__": lambda s, *a: False}))
    monkeypatch.setattr(cli_run, "create_layout", lambda: None)
    monkeypatch.setattr(cli_run, "update_display", lambda *a, **k: None)
    monkeypatch.setattr(cli_run, "_build_run_config", lambda s, c: cfg)
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *a, **k: "N")
    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags=None: {
        "ticker": "NVDA", "analysis_date": "2026-10-23", "asset_type": "stock", "analysts": [AnalystType.MARKET]})
    from tradingagents import e2_cache
    e2_cache.reset_stats()
    began = time.perf_counter()
    cli_run.run_analysis()
    for save in saves:
        save()
    total = time.perf_counter() - began
    [e3file] = (tmp_path / "e3").glob("*.json")
    e3 = json.loads(e3file.read_text())
    usage = json.loads((tmp_path / "r" / "NVDA" / "2026-10-23" / "token_usage.json").read_text())
    Path(os.environ["BENCH_OUT"]).write_text(json.dumps({"total_seconds": total, "usage": usage, "e3": e3,
        "data_requests": (tmp_path / "data.log").read_text().count("request") if (tmp_path / "data.log").exists() else 0}))
