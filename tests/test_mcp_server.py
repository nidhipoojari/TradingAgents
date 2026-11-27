"""F2 MCP server: one read-only analyze_stock tool over the shared analysis service.

The tool's core is tested without FastMCP; the protocol tests run once the ``mcp``
extra is installed (they are skipped otherwise). The service is stubbed: no LLM.
"""

import ast
import asyncio
import datetime
import threading
from pathlib import Path

import pytest

from tradingagents import mcp_server, service

ROOT = Path(__file__).resolve().parents[1]
PAST = "2026-01-09"


def _stub(rating="Overweight", text=None, record=None):
    def run(request, on_event=None, cancel=None):
        if record is not None:
            record.append(request)
        for done in (1, 1, 2, 3):   # a repeated count must be reported once
            on_event(service.AnalysisEvent("r", "step", progress=(done, 3, "next")))
        return service.AnalysisResult(
            run_id="20261121-101500-ab12cd34", source=request.source, ticker=request.ticker,
            trade_date=request.trade_date, asset_type=request.asset_type, analysts=request.analysts,
            rating=rating,
            final_state={"final_trade_decision": text or f"**Rating**: {rating}\n\nReasons.",
                         "investment_plan": "Plan.", "trader_investment_plan": "Trade.",
                         "market_report": "Market.", "news_report": "News."},
            usage={"llm_calls": 9, "tokens_in": 100, "tokens_out": 50, "tokens_total": 150,
                   "elapsed_seconds": 80.0, "run_id": "x", "source": "mcp"},
            results_dir=Path("."), usage_path=Path("token_usage.json"))
    return run


# ------------------------------------------------------------------ request validation
@pytest.mark.unit
def test_a_request_is_validated_like_the_cli_flags():
    request = mcp_server.build_request(" nvda ", PAST, ["news", "Market"])
    assert (request.ticker, request.trade_date, request.source) == ("NVDA", PAST, "mcp")
    assert request.analysts == ("market", "news")
    assert mcp_server.build_request("NVDA", PAST, ["sentiment"]).analysts == ("social",)


@pytest.mark.unit
def test_defaults_are_today_and_every_analyst_the_asset_allows():
    stock = mcp_server.build_request("AAPL")
    assert stock.trade_date == datetime.date.today().isoformat()
    assert stock.analysts == service.ANALYST_ORDER
    crypto = mcp_server.build_request("BTC-USD", PAST)
    assert crypto.asset_type == "crypto" and "fundamentals" not in crypto.analysts


@pytest.mark.unit
@pytest.mark.parametrize("ticker, date, analysts, message", [
    ("", None, None, "ticker"),
    ("NV DA", None, None, "ticker"),
    ("../../etc", None, None, "ticker"),
    ("NVDA", "2999-01-01", None, "future"),
    ("NVDA", "01/09/2026", None, "YYYY-MM-DD"),
    ("NVDA", PAST, ["macro"], "unknown analyst"),
])
def test_a_bad_request_is_refused(ticker, date, analysts, message):
    with pytest.raises(ValueError, match=message):
        mcp_server.build_request(ticker, date, analysts)


# ------------------------------------------------------------------ the tool's answer
@pytest.mark.unit
def test_the_answer_leads_with_the_decision():
    seen, progress = [], []
    answer = mcp_server.analyze_stock_sync("NVDA", PAST, on_progress=lambda *p: progress.append(p),
                                           run=_stub(record=seen))
    assert seen[0].source == "mcp"
    assert answer["rating"] == "Overweight" and answer["needs_review"] is False
    assert answer["decision"].startswith("**Rating**: Overweight")
    assert answer["investment_plan"] == "Plan." and answer["trader_plan"] == "Trade."
    assert answer["usage"]["llm_calls"] == 9 and "reports" not in answer
    assert "never places orders" in answer["note"]
    assert [p[0] for p in progress] == [1, 2, 3]


@pytest.mark.unit
def test_reports_are_included_on_request_and_bounded(monkeypatch):
    monkeypatch.setenv(mcp_server.REPORT_CHARS_ENV, "50")
    answer = mcp_server.analyze_stock_sync("NVDA", PAST, include_reports=True,
                                           run=_stub(text="A" * 500))
    assert set(answer["reports"]) == {"market", "news"}
    assert len(answer["decision"]) < 150 and "truncated" in answer["decision"]


@pytest.mark.unit
def test_a_review_rating_is_flagged():
    answer = mcp_server.analyze_stock_sync("NVDA", PAST, run=_stub(rating="REVIEW"))
    assert answer["needs_review"] is True


# ------------------------------------------------------------------ read-only by construction
def _imported_modules(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):   # includes function-level imports
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
    return names


@pytest.mark.unit
@pytest.mark.parametrize("module", ["tradingagents/mcp_server.py", "tradingagents/service.py",
                                    "tradingagents/mcp_read_tools.py", "tradingagents/run_store.py",
                                    "tradingagents/trade_rules.py", "tradingagents/mcp_audit.py",
                                    "tradingagents/audit_log.py", "tradingagents/proposal_store.py"])
def test_the_mcp_path_cannot_reach_the_broker(module):
    imported = _imported_modules(ROOT / module)
    assert not {m for m in imported if m.startswith(("tradingagents.execution", "tradingagents.dashboard",
                                                      "alpaca"))}


@pytest.mark.unit
def test_only_the_paper_adapter_imports_alpaca():
    offenders = sorted(
        str(path.relative_to(ROOT))
        for package in ("tradingagents", "cli")
        for path in (ROOT / package).rglob("*.py")
        if any(m == "alpaca" or m.startswith("alpaca.") for m in _imported_modules(path))
    )
    assert offenders == ["tradingagents/execution/alpaca_paper.py"]


@pytest.mark.unit
def test_streamlit_stays_in_the_page():
    offenders = sorted(
        str(path.relative_to(ROOT))
        for package in ("tradingagents", "cli")
        for path in (ROOT / package).rglob("*.py")
        if any(m.split(".")[0] == "streamlit" for m in _imported_modules(path))
    )
    assert offenders == ["tradingagents/dashboard/app.py", "tradingagents/dashboard/launch.py"]


@pytest.mark.unit
def test_http_needs_a_token(monkeypatch, capsys):
    monkeypatch.delenv(mcp_server.TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit):
        mcp_server.main(["--transport", "http"])
    assert mcp_server.TOKEN_ENV in capsys.readouterr().err


# ------------------------------------------------------------------ over the protocol (needs fastmcp)
def _client(server):
    fastmcp = pytest.importorskip("fastmcp")
    return fastmcp.Client(server)


@pytest.mark.unit
def test_the_server_lists_five_read_only_tools():
    pytest.importorskip("fastmcp")

    async def go():
        async with _client(mcp_server.create_server(run=_stub())) as client:
            return await client.list_tools()

    tools = {t.name: t for t in asyncio.run(go())}
    assert sorted(tools) == ["analyze_stock", "get_report", "get_run_metrics", "list_recent_runs", "propose_trade"]
    for tool in tools.values():
        hints = tool.annotations
        assert hints.read_only_hint is True and hints.destructive_hint is False, tool.name
        assert "ctx" not in tool.input_schema["properties"]
    assert tools["analyze_stock"].annotations.open_world_hint is True       # models and market data
    assert all(tools[n].annotations.open_world_hint is False for n in tools if n != "analyze_stock")


@pytest.mark.unit
def test_the_tool_answers_over_the_protocol():
    pytest.importorskip("fastmcp")

    async def go():
        async with _client(mcp_server.create_server(run=_stub())) as client:
            return await client.call_tool("analyze_stock", {"ticker": "NVDA", "trade_date": PAST})

    result = asyncio.run(go())
    assert result.data["rating"] == "Overweight"


@pytest.mark.unit
@pytest.mark.parametrize("arguments, message", [
    ({"ticker": "NVDA", "trade_date": "2999-01-01"}, "future"),
    ({"ticker": "NV DA"}, "ticker"),
])
def test_bad_arguments_come_back_as_tool_errors(arguments, message):
    pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    async def go():
        async with _client(mcp_server.create_server(run=_stub())) as client:
            await client.call_tool("analyze_stock", arguments)

    with pytest.raises(ToolError, match=message):
        asyncio.run(go())


@pytest.mark.unit
@pytest.mark.parametrize("value", ["yes", "true", "1", "on", 1, 0, None, [True]])
def test_a_non_boolean_include_reports_is_refused_before_any_run(value):
    """FastMCP's lax mode turned "yes" into True and started a full analysis (found 2026-11-27 over stdio)."""
    pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    started = []

    async def go():
        async with _client(mcp_server.create_server(run=_stub(record=started))) as client:
            await client.call_tool("analyze_stock", {"ticker": "NVDA", "trade_date": PAST, "include_reports": value})

    with pytest.raises(ToolError, match="include_reports"):
        asyncio.run(go())
    assert started == []


@pytest.mark.unit
@pytest.mark.parametrize("value", [True, False])
def test_a_json_boolean_include_reports_still_works(value):
    pytest.importorskip("fastmcp")

    async def go():
        async with _client(mcp_server.create_server(run=_stub())) as client:
            return await client.call_tool("analyze_stock", {"ticker": "NVDA", "trade_date": PAST,
                                                            "include_reports": value})

    answer = asyncio.run(go()).data
    assert ("reports" in answer) is value


@pytest.mark.unit
def test_a_run_past_the_timeout_is_stopped():
    pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    cancelled = threading.Event()

    def slow(request, on_event=None, cancel=None):
        while not cancel.wait(0.01):
            pass
        cancelled.set()
        raise service.AnalysisCancelled("r", "on request")

    async def go():
        async with _client(mcp_server.create_server(run=slow, timeout=0.2)) as client:
            await client.call_tool("analyze_stock", {"ticker": "NVDA", "trade_date": PAST})

    with pytest.raises(ToolError, match="timed out"):
        asyncio.run(go())
    assert cancelled.wait(2)


@pytest.mark.unit
def test_a_busy_server_refuses_at_once():
    pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    def busy(request, on_event=None, cancel=None):
        raise service.AnalysisBusyError("another analysis is running")

    async def go():
        async with _client(mcp_server.create_server(run=busy)) as client:
            await client.call_tool("analyze_stock", {"ticker": "NVDA", "trade_date": PAST})

    with pytest.raises(ToolError, match="another analysis"):
        asyncio.run(go())


@pytest.mark.unit
def test_the_api_key_is_checked():
    pytest.importorskip("fastmcp")
    verifier = mcp_server.api_key_verifier("s3cret-token-0123456789")
    assert asyncio.run(verifier.verify_token("s3cret-token-0123456789")) is not None
    assert asyncio.run(verifier.verify_token("wrong")) is None
    assert asyncio.run(verifier.verify_token("")) is None


# ------------------------------------------------------------------ HTTP exposure
TOKEN = "t" * 40


@pytest.mark.unit
@pytest.mark.parametrize("host, loopback", [
    ("127.0.0.1", True), ("127.8.9.10", True), ("::1", True), ("[::1]", True), ("localhost", True),
    ("0.0.0.0", False), ("::", False), ("192.168.1.5", False), ("10.0.0.1", False),
    ("example.com", False), ("", False),
])
def test_loopback_addresses_are_recognized(host, loopback):
    assert mcp_server.is_loopback(host) is loopback


@pytest.mark.unit
def test_loopback_needs_only_a_token():
    assert mcp_server.http_exposure_problems("127.0.0.1", "s" * 16, allow_remote=False,
                                             tls_cert=None, tls_key=None) == []
    assert mcp_server.http_exposure_problems("127.0.0.1", "", allow_remote=False, tls_cert=None, tls_key=None)


@pytest.mark.unit
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "mcp.example.com"])
def test_a_reachable_address_is_refused_without_every_safeguard(tmp_path, host):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("cert")
    key.write_text("key")
    check = mcp_server.http_exposure_problems
    assert any("--allow-remote" in p for p in check(host, TOKEN, allow_remote=False,
                                                    tls_cert=str(cert), tls_key=str(key)))
    assert any("needs TLS" in p for p in check(host, TOKEN, allow_remote=True, tls_cert=None, tls_key=None))
    assert any("32" in p for p in check(host, "s" * 20, allow_remote=True,
                                        tls_cert=str(cert), tls_key=str(key)))
    assert check(host, TOKEN, allow_remote=True, tls_cert=str(cert), tls_key=str(key)) == []


@pytest.mark.unit
def test_tls_needs_both_files_and_they_must_exist(tmp_path):
    check = mcp_server.http_exposure_problems
    assert any("both" in p for p in check("127.0.0.1", TOKEN, allow_remote=False,
                                          tls_cert=str(tmp_path / "c.pem"), tls_key=None))
    assert any("not a readable file" in p for p in check("127.0.0.1", TOKEN, allow_remote=False,
                                                         tls_cert=str(tmp_path / "missing.pem"),
                                                         tls_key=str(tmp_path / "missing.key")))


@pytest.mark.unit
@pytest.mark.parametrize("argv, message", [
    (["--transport", "http", "--host", "0.0.0.0"], "binds to 127.0.0.1"),
    (["--transport", "http", "--host", "0.0.0.0", "--allow-remote"], "needs TLS"),
    (["--host", "0.0.0.0"], "http transport only"),
    (["--allow-remote"], "http transport only"),
])
def test_the_command_refuses_public_exposure(monkeypatch, capsys, argv, message):
    monkeypatch.setenv(mcp_server.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(mcp_server, "create_server", lambda **k: pytest.fail("the server must not start"))
    with pytest.raises(SystemExit):
        mcp_server.main(argv)
    assert message in capsys.readouterr().err


@pytest.mark.unit
def test_tls_settings_reach_the_http_server(monkeypatch, tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("cert")
    key.write_text("key")
    seen = {}

    class Server:
        def run(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setenv(mcp_server.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(mcp_server, "create_server", lambda **k: Server())
    monkeypatch.setattr(mcp_server, "api_key_verifier", lambda token: object())
    mcp_server.main(["--transport", "http", "--host", "0.0.0.0", "--allow-remote",
                     "--tls-cert", str(cert), "--tls-key", str(key)])
    assert seen["host"] == "0.0.0.0"
    assert seen["uvicorn_config"] == {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
