"""F2: an MCP server with one read-only tool, ``analyze_stock``.

Other MCP-compatible AI assistants can ask TradingAgents for an analysis of a
stock and get the decision back. The tool runs the same shared analysis service
as the CLI and the dashboard (``tradingagents.service``), so E1 routing and
backup models, the E2 market-data cache and E3 tracing all apply unchanged.

Read-only by construction: this module never imports the execution layer
(``tradingagents.execution``) and places no orders; a test pins that. A run
does write its own analysis records (token_usage.json, the state log, the E3
summary) and records its decision in a separate MCP memory log, so requests from
other assistants do not feed lessons into the CLI's and dashboard's decisions.

Run it:
    tradingagents-mcp                      # stdio, for a local assistant that launches it
    TRADINGAGENTS_MCP_TOKEN=... tradingagents-mcp --transport http --port 8765

Security: stdio is the recommended transport for a local assistant and needs no
key (the assistant starts the process with your environment). HTTP refuses to
start without ``TRADINGAGENTS_MCP_TOKEN`` (clients send it as ``Authorization:
Bearer <token>``) and binds to 127.0.0.1. Any other address, including 0.0.0.0,
is refused unless ``--allow-remote`` is given together with ``--tls-cert`` and
``--tls-key`` (HTTPS) and a token of at least 32 characters, so the server cannot
be exposed unauthenticated or unencrypted by accident.

Long runs: an analysis takes one to several minutes. The tool reports progress
as each report is filed, and stops the run at the next step after
``TRADINGAGENTS_MCP_TIMEOUT_SECONDS`` (default 900). One analysis runs at a time;
a second request while one runs is refused at once rather than queued.

Note: no ``from __future__ import annotations`` here; FastMCP reads the tool's
type hints, including the Context class imported inside ``create_server``.
"""

import argparse
import hmac
import logging
import os
import sys
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from tradingagents import service

logger = logging.getLogger(__name__)

TOKEN_ENV = "TRADINGAGENTS_MCP_TOKEN"
TIMEOUT_ENV = "TRADINGAGENTS_MCP_TIMEOUT_SECONDS"
REPORT_CHARS_ENV = "TRADINGAGENTS_MCP_MAX_REPORT_CHARS"
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_REPORT_CHARS = 4000

TOOL_DESCRIPTION = (
    "Run the TradingAgents multi-agent analysis (market, sentiment, news and fundamentals "
    "analysts, a bull/bear research debate, a trader and a risk debate) for one stock ticker "
    "and return the Portfolio Manager's rating (Buy, Overweight, Hold, Underweight, Sell, or "
    "REVIEW when no rating could be read) with the decision text. Read-only: it never places "
    "orders. Takes one to several minutes. Research output, not financial advice."
)

SERVER_INSTRUCTIONS = (
    "TradingAgents exposes one tool, analyze_stock. Each call runs a full multi-agent LLM "
    "analysis that takes minutes and costs model tokens; call it once per ticker and date "
    "and reuse the result. Dates are YYYY-MM-DD and cannot be in the future; omit the date "
    "for today. A REVIEW rating means the decision had no readable rating."
)

READ_ONLY_ANNOTATIONS = {
    "title": "Analyze stock",
    "readOnlyHint": True,       # no orders, no changes outside its own analysis records
    "destructiveHint": False,
    "idempotentHint": False,    # a re-run is a new LLM analysis and may differ
    "openWorldHint": True,      # reads market data and calls LLM providers
}

SUMMARY_SECTIONS = {
    "investment_plan": "investment_plan",
    "trader_plan": "trader_investment_plan",
}
REPORT_SECTIONS = {
    "market": "market_report",
    "sentiment": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _truncate(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n[... truncated, {len(text) - limit:,} more characters in the saved run]"


def build_request(ticker: str, trade_date: str | None = None,
                  analysts: list[str] | None = None) -> service.AnalysisRequest:
    """Validate the tool's arguments the way the CLI validates its flags."""
    from cli.models import AnalystType
    from cli.prompts import (
        detect_asset_type,
        filter_analysts_for_asset_type,
        parse_analysis_date,
        parse_analysts,
        parse_ticker,
    )
    from tradingagents.dataflows.date_window import get_current_date

    symbol = parse_ticker(ticker or "")
    asset_type = detect_asset_type(symbol)
    day = parse_analysis_date(trade_date) if trade_date else get_current_date()
    chosen = (parse_analysts(",".join(analysts), asset_type) if analysts
              else filter_analysts_for_asset_type(list(AnalystType), asset_type))
    return service.AnalysisRequest(
        ticker=symbol, trade_date=day, source="mcp", asset_type=asset_type.value,
        analysts=tuple(a.value for a in chosen),
    )


def summarize(result: service.AnalysisResult, include_reports: bool = False,
              max_chars: int | None = None) -> dict[str, Any]:
    """The tool's answer: the decision first, sizes bounded for the caller's context window."""
    limit = int(max_chars or _env_float(REPORT_CHARS_ENV, DEFAULT_REPORT_CHARS))
    state = result.final_state
    usage = result.usage
    answer: dict[str, Any] = {
        "run_id": result.run_id,
        "source": result.source,
        "ticker": result.ticker,
        "trade_date": result.trade_date,
        "asset_type": result.asset_type,
        "analysts": ["sentiment" if a == "social" else a for a in result.analysts],
        "rating": result.rating,
        "needs_review": result.is_review,
        "decision": _truncate(result.decision_text, limit),
        **{name: _truncate(state.get(key), limit) for name, key in SUMMARY_SECTIONS.items()},
        "usage": {key: usage.get(key) for key in
                  ("llm_calls", "tool_calls", "tokens_in", "tokens_out", "tokens_total",
                   "elapsed_seconds", "e2_cache") if key in usage},
        "model_fallbacks": len(result.fallback_events),
        "estimated_cost_usd": _e3_cost(result),
        "note": "Research output, not financial advice. This tool never places orders.",
    }
    if include_reports:
        answer["reports"] = {name: _truncate(state.get(key), limit)
                             for name, key in REPORT_SECTIONS.items() if state.get(key)}
    return answer


def _e3_cost(result: service.AnalysisResult) -> float | None:
    """E3's cost estimate for the run, when tracing and pricing were on."""
    if result.e3_summary_path is None:
        return None
    import json
    try:
        summary = json.loads(result.e3_summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return summary.get("estimated_cost_usd") or summary.get("known_usage_cost_usd")


def analyze_stock_sync(
    ticker: str,
    trade_date: str | None = None,
    analysts: list[str] | None = None,
    include_reports: bool = False,
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
    cancel: threading.Event | None = None,
    run=None,
) -> dict[str, Any]:
    """The tool without the protocol: validate, run through the shared service, summarize.

    ``run`` defaults to the analysis worker process, so a timed-out or cancelled
    request releases the server within the grace period even if a model call hangs.
    """
    if run is None:
        from tradingagents.worker import analysis_runner

        run = analysis_runner()
    request = build_request(ticker, trade_date, analysts)
    last = [-1]

    def on_event(event: service.AnalysisEvent) -> None:
        done, total, label = event.progress
        if on_progress is not None and event.kind == "step" and done != last[0]:
            last[0] = done
            on_progress(done, total, label)

    result = run(request, on_event=on_event, cancel=cancel)
    return summarize(result, include_reports)


def api_key_verifier(token: str):
    """Bearer-token check for the HTTP transport, compared in constant time."""
    from fastmcp.server.auth import AccessToken, TokenVerifier

    class ApiKeyVerifier(TokenVerifier):
        async def verify_token(self, presented: str):
            if presented and hmac.compare_digest(presented.encode(), token.encode()):
                return AccessToken(token=presented, client_id="tradingagents-mcp", scopes=[])
            return None

    return ApiKeyVerifier()


def create_server(*, auth=None, timeout: float | None = None, run=None):
    """Build the FastMCP server with its one tool. ``run`` is the service call (tests stub it)."""
    import anyio
    from fastmcp import Context, FastMCP
    from fastmcp.exceptions import ToolError

    limit = timeout or _env_float(TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS)
    mcp = FastMCP("TradingAgents", instructions=SERVER_INSTRUCTIONS, auth=auth)

    @mcp.tool(name="analyze_stock", description=TOOL_DESCRIPTION, annotations=READ_ONLY_ANNOTATIONS)
    async def analyze_stock(
        ticker: str,
        ctx: Context,
        trade_date: str | None = None,
        analysts: list[str] | None = None,
        include_reports: bool = False,
    ) -> dict:
        """ticker: e.g. NVDA, 0700.HK, BTC-USD. trade_date: YYYY-MM-DD, today or earlier; omit
        for today. analysts: any of market, sentiment, news, fundamentals; omit for all.
        include_reports: also return each analyst's report (truncated)."""
        cancel = threading.Event()

        def progress(done: int, total: int, label: str) -> None:
            # Called from the worker thread; a lost notification must not stop the run.
            try:
                anyio.from_thread.run(ctx.report_progress, done, total, f"{done}/{total} done; next: {label}")
            except Exception:  # noqa: BLE001
                logger.debug("progress notification failed", exc_info=True)

        call = partial(analyze_stock_sync, ticker, trade_date, analysts, include_reports,
                       on_progress=progress, cancel=cancel, run=run)
        try:
            with anyio.fail_after(limit):
                return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
        except TimeoutError:
            cancel.set()   # the run stops at its next step and frees the server
            raise ToolError(f"analysis timed out after {limit:.0f}s and was stopped; "
                            f"raise {TIMEOUT_ENV} or analyze fewer analysts") from None
        except anyio.get_cancelled_exc_class():
            cancel.set()   # the client went away or cancelled the request
            raise
        except ValueError as exc:
            raise ToolError(f"invalid request: {exc}") from None
        except service.AnalysisBusyError as exc:
            raise ToolError(str(exc)) from None
        except service.AnalysisCancelled as exc:
            raise ToolError(str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - a provider or data failure, reported briefly
            logger.exception("analyze_stock failed")
            raise ToolError(f"analysis failed: {type(exc).__name__}: {str(exc)[:300]}") from None

    return mcp


MIN_TOKEN_CHARS = 16
MIN_REMOTE_TOKEN_CHARS = 32


def is_loopback(host: str) -> bool:
    """127.0.0.0/8, ::1 or the name localhost. Anything else (0.0.0.0, a LAN or public
    address, any other host name) can be reached from other machines."""
    import ipaddress

    if (host or "").strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def http_exposure_problems(host: str, token: str, *, allow_remote: bool,
                           tls_cert: str | None, tls_key: str | None) -> list[str]:
    """Why the HTTP transport must not start with these settings; empty when it may.

    Loopback needs a bearer token. Any other address needs, all together,
    ``--allow-remote``, a TLS certificate and key (so the token never crosses the
    network in clear text) and a token of at least MIN_REMOTE_TOKEN_CHARS.
    """
    problems = []
    if len(token) < MIN_TOKEN_CHARS:
        problems.append(f"the http transport needs {TOKEN_ENV} set to a secret of at least "
                        f"{MIN_TOKEN_CHARS} characters")
    if bool(tls_cert) != bool(tls_key):
        problems.append("TLS needs both --tls-cert and --tls-key")
    for label, path in (("--tls-cert", tls_cert), ("--tls-key", tls_key)):
        if path and not Path(path).is_file():
            problems.append(f"{label} {path} is not a readable file")
    if is_loopback(host):
        return problems
    if not allow_remote:
        problems.append(f"{host} can be reached from other machines; the server binds to 127.0.0.1 unless "
                        "--allow-remote is given (with --tls-cert and --tls-key). For a local assistant, "
                        "use the stdio transport instead")
    if not (tls_cert and tls_key):
        problems.append(f"serving on {host} needs TLS (--tls-cert and --tls-key); without it the bearer "
                        "token and the analyses cross the network unencrypted")
    if len(token) < MIN_REMOTE_TOKEN_CHARS:
        problems.append(f"serving beyond this machine needs {TOKEN_ENV} of at least "
                        f"{MIN_REMOTE_TOKEN_CHARS} characters")
    return problems


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tradingagents-mcp",
        description="TradingAgents MCP server. stdio (the default) is the recommended local transport.")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="http only; loopback unless --allow-remote")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-remote", action="store_true",
                        help="http only: allow a non-loopback address; needs TLS and a 32+ character token")
    parser.add_argument("--tls-cert", help="http only: PEM certificate file for HTTPS")
    parser.add_argument("--tls-key", help="http only: PEM private key file for HTTPS")
    args = parser.parse_args(argv)

    # stdout is the protocol channel under stdio; every log line goes to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.transport == "stdio":
        if args.allow_remote or args.tls_cert or args.tls_key or args.host != "127.0.0.1":
            parser.error("--host, --allow-remote and --tls-* apply to the http transport only")
        create_server().run(transport="stdio", show_banner=False)
        return
    token = os.environ.get(TOKEN_ENV, "").strip()
    problems = http_exposure_problems(args.host, token, allow_remote=args.allow_remote,
                                      tls_cert=args.tls_cert, tls_key=args.tls_key)
    if problems:
        parser.error("; ".join(problems))
    uvicorn_config = ({"ssl_certfile": args.tls_cert, "ssl_keyfile": args.tls_key}
                      if args.tls_cert else None)
    create_server(auth=api_key_verifier(token)).run(
        transport="http", host=args.host, port=args.port, show_banner=False,
        uvicorn_config=uvicorn_config)


if __name__ == "__main__":
    main()
