"""F1: the TradingAgents dashboard page.

    tradingagents-dashboard          # serves on http://127.0.0.1:8501

Shows a run's analyst reports, the bull/bear research debate, the trader's plan,
the risk debate and the Portfolio Manager's decision, with E1/E2/E3 run metrics.
A person can then place one Alpaca **paper** order per analysis run: they type
the quantity, the order is checked (no shorting, notional cap, buying power,
shares held), and nothing is sent until they tick the approval for that exact
order and press submit.

The page only renders state. Analyses run in a background thread through the
shared analysis service (``jobs``); orders go through ``trade`` and
``tradingagents.execution``, where every safeguard lives.

Trust boundary: this is one local process serving 127.0.0.1. The browser
session is not trusted (it must sign in, and every order is re-checked by the
broker); code running inside this process is trusted. See PHASE3_CHANGES.md,
"the dashboard's trust boundary".
"""

import json
import os
from datetime import date
from pathlib import Path

import streamlit as st

from cli.prompts import detect_asset_type, parse_ticker
from tradingagents import run_budget, service
from tradingagents.dashboard import jobs, trade
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.execution import identity as auth, order_policy as op
from tradingagents.execution.alpaca_paper import (
    AlpacaPaperBroker,
    BrokerError,
    BrokerNotConfigured,
    OrderLedger,
)
from tradingagents.portfolio import PortfolioContext, Position

ANALYST_LABELS = {"market": "Market", "social": "Sentiment", "news": "News", "fundamentals": "Fundamentals"}
DEPTHS = {"Shallow: 1 debate round": 1, "Medium: 3 rounds": 3, "Deep: 5 rounds": 5}
ROUND_ENVS = (("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "max_debate_rounds"),
              ("TRADINGAGENTS_MAX_RISK_ROUNDS", "max_risk_discuss_rounds"))


@st.cache_resource
def _job_registry() -> dict:
    """Jobs by run ID, shared by every browser session of this server process."""
    return {}


@st.cache_resource
@st.cache_resource
def _sessions() -> auth.SessionStore:
    """Server-side sessions and persistent sign-in limits (SQLite, mode 0600)."""
    return auth.SessionStore.default()


def _broker():
    """The paper account, or why there is none. Cached; the sidebar can reconnect."""
    try:
        # Beside the dashboard runs' own results, under the same run config.
        ledger = OrderLedger.for_results_dir(service.config_for_source("dashboard")["results_dir"])
        broker = AlpacaPaperBroker.from_env(ledger=ledger)
        broker.sessions = _sessions()     # orders go out only while the approver's session is active
        return broker, None
    except BrokerNotConfigured as exc:
        return None, str(exc)


def _identity():
    """The browser session's verified sign-in, or None. Never the OS user or a checkbox."""
    identity = st.session_state.get("identity")
    if identity is not None and auth.verify_identity(identity, _sessions()):
        st.session_state.pop("identity", None)   # expired, idle, signed out or pre-restart: sign in again
        return None
    return identity


def sign_in_panel():
    st.sidebar.header("Sign-in")
    identity = _identity()
    if identity is not None:
        st.sidebar.success(f"Signed in as **{identity.subject}**.")
        if st.sidebar.button("Sign out"):
            _sessions().revoke(identity.session)
            st.session_state.pop("identity", None)
            st.rerun()
        return
    if not auth.auth_required():
        st.sidebar.warning("Sign-in is switched off (TRADINGAGENTS_REQUIRE_ORDER_AUTH=false); "
                           "paper orders are recorded as unauthenticated.")
    else:
        st.sidebar.caption("Sign in to approve paper orders. Viewing analyses needs no sign-in.")
    with st.sidebar.form("sign-in"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            try:
                st.session_state["identity"] = auth.sign_in_with_password(username, password, store=_sessions())
            except auth.AuthError as exc:
                st.error(str(exc))
            else:
                st.rerun()


def _current_job():
    job_id = st.session_state.get("job_id")
    return _job_registry().get(job_id) if job_id else None


# ------------------------------------------------------------------ sidebar
def sidebar(broker, broker_error):
    cfg = DEFAULT_CONFIG
    st.sidebar.header("Run settings")
    st.sidebar.caption("From .env / TRADINGAGENTS_* variables; the same ones the CLI uses.")
    st.sidebar.markdown(
        f"**Provider:** {cfg.get('llm_provider')}  \n"
        f"**Quick model:** {cfg.get('quick_think_llm')}  \n"
        f"**Deep model:** {cfg.get('deep_think_llm')}")
    fallbacks = [f"{t}: {cfg.get(f'{t}_fallback_llm')}" for t in ("quick", "deep") if cfg.get(f"{t}_fallback_llm")]
    st.sidebar.markdown(f"**E1 backup models:** {', '.join(fallbacks) if fallbacks else 'off'}")
    st.sidebar.markdown(f"**E2 data cache:** {'on' if cfg.get('e2_cache_enabled') else 'off'}")
    e3_on = os.getenv("E3_TRACE_ENABLED", "").lower() in {"1", "true", "yes"}
    st.sidebar.markdown(f"**E3 tracing:** {'on (Langfuse)' if e3_on else 'off'}")
    try:
        limits = run_budget.dashboard_budget_from_env()
        st.sidebar.markdown(f"**Per-run limits:** {limits.describe() if limits else 'off'}")
        run_budget.check_pricing(limits)
    except ValueError as exc:
        st.sidebar.error(str(exc))

    st.sidebar.header("Paper trading")
    if broker is None:
        st.sidebar.warning(broker_error)
    else:
        st.sidebar.success("Connected to the Alpaca **paper** account.")
    try:
        st.sidebar.markdown(f"**Max order value:** ${op.OrderLimits.from_env().max_notional:,.2f}")
    except ValueError as exc:
        st.sidebar.error(str(exc))
    if st.sidebar.button("Reconnect to Alpaca"):
        _broker.clear()
        st.rerun()


# ------------------------------------------------------------------ analyze form
def analyze_form(broker, busy: bool):
    with st.form("analyze"):
        cols = st.columns([2, 2, 3])
        ticker = cols[0].text_input("Ticker", value=st.session_state.get("last_ticker", "NVDA"))
        day = cols[1].date_input("Analysis date", value=date.today(), max_value=date.today())
        depth = cols[2].selectbox("Research depth", list(DEPTHS))
        analysts = st.multiselect("Analysts", options=list(ANALYST_LABELS), default=list(ANALYST_LABELS),
                                  format_func=ANALYST_LABELS.get)
        use_book = st.checkbox("Tell the agents what the paper account holds (read-only)",
                               disabled=broker is None)
        if day.isoformat() == date.today().isoformat():
            st.caption("Today's data is always fetched fresh; the E2 cache only reuses past-dated requests.")
        go = st.form_submit_button("Run analysis", type="primary", disabled=busy)
    if go:
        _start(ticker, day.isoformat(), analysts, DEPTHS[depth], broker if use_book else None)


def _start(ticker_text, trade_date, analysts, rounds, broker):
    try:
        ticker = parse_ticker(ticker_text)
        asset_type = detect_asset_type(ticker).value
        chosen = [a for a in analysts if not (asset_type == "crypto" and a == "fundamentals")]
        if not chosen:
            raise ValueError("choose at least one analyst")
        limits = run_budget.dashboard_budget_from_env()
        run_budget.check_pricing(limits)     # a cost limit needs prices: refuse rather than run unlimited
        config = service.config_for_source("dashboard")
        # As in the CLI: an explicit TRADINGAGENTS_MAX_*_ROUNDS wins over the depth picked here.
        for env, key in ROUND_ENVS:
            if not os.environ.get(env):
                config[key] = rounds
        portfolio = _portfolio(broker) if broker is not None else None
        request = service.AnalysisRequest(ticker=ticker, trade_date=trade_date, source="dashboard",
                                          asset_type=asset_type, analysts=tuple(chosen),
                                          config=config, portfolio=portfolio, budget=limits)
        job = jobs.start_job(request)
    except (ValueError, BrokerError, service.AnalysisBusyError) as exc:
        st.error(str(exc))
        return
    _job_registry()[job.run_id] = job
    st.session_state.update(job_id=job.run_id, last_ticker=ticker)
    st.rerun()


def _portfolio(broker) -> PortfolioContext:
    account = broker.account()
    return PortfolioContext(cash=account.cash, currency=account.currency,
                            positions=[Position(**p) for p in broker.positions()])


# ------------------------------------------------------------------ progress
@st.fragment(run_every=2.0)
def progress(job):
    snap = job.snapshot()
    if snap["status"] != "running":
        st.rerun()
    done, total, label = snap["progress"]
    st.progress(done / total if total else 0.0,
                text=f"Analyzing {job.request.ticker} for {job.request.trade_date}: "
                     f"{done}/{total} reports, next {label} ({snap['elapsed']:.0f}s)")
    limits = job.request.budget
    st.caption(f"Per-run limits: {limits.describe() if limits else 'off'}. The run stops before a model call "
               "that would pass them.")
    with st.expander("Agent activity"):
        st.code("\n".join(job.log()[-40:]) or "Waiting for the first agent...", language=None)
    if st.button("Cancel analysis"):
        job.cancel.set()
        st.info("Stopping after the current step...")


# ------------------------------------------------------------------ results
def results(job):
    if job.status == "budget_exceeded":
        st.error(f"Stopped by the per-run limit, not by you and not by a model error. {job.error or ''}")
        st.markdown(run_budget.describe_snapshot(job.budget))
        summary = _e3_summary_for(job.run_id)
        if summary is not None:
            st.markdown(cost_text(service.e3_cost(summary)))
        return
    if job.status != "done":
        (st.warning if job.status == "cancelled" else st.error)(job.error or job.status)
        return
    result = job.result
    state = result.final_state
    head = st.columns(4)
    head[0].metric("Rating", result.rating)
    head[1].metric("Ticker", result.ticker, help=f"Analysis date {result.trade_date}")
    head[2].metric("LLM calls", result.usage.get("llm_calls", 0))
    head[3].metric("Run time", f"{result.usage.get('elapsed_seconds', 0):.0f}s")
    if result.is_review:
        st.warning("No rating could be read from the final decision. Read it below before acting; "
                   "no order can be placed from this run.")
    st.caption(f"Run ID {result.run_id}. Research output, not financial advice.")

    tabs = st.tabs(["Decision", "Analysts", "Research debate", "Trader", "Risk debate",
                    "Run metrics", "Paper trade"])
    with tabs[0]:
        st.markdown(state.get("final_trade_decision") or "_No decision text._")
    with tabs[1]:
        _analysts(result)
    with tabs[2]:
        _research(state)
    with tabs[3]:
        st.markdown(state.get("trader_investment_plan") or "_No trader plan._")
    with tabs[4]:
        _risk(state)
    with tabs[5]:
        _metrics(result)
    with tabs[6]:
        paper_trade(result)


def _analysts(result):
    reports = [(ANALYST_LABELS[a], result.final_state.get(service.ANALYST_REPORTS[a]))
               for a in result.analysts]
    for tab, (_, text) in zip(st.tabs([name for name, _ in reports]), reports, strict=True):
        with tab:
            st.markdown(text or "_No report._")


def _research(state):
    debate = state.get("investment_debate_state") or {}
    bull, bear = st.columns(2)
    with bull:
        st.subheader("Bull case")
        st.markdown(debate.get("bull_history") or "_None._")
    with bear:
        st.subheader("Bear case")
        st.markdown(debate.get("bear_history") or "_None._")
    st.subheader("Research Manager")
    st.markdown(state.get("investment_plan") or "_No plan._")


def _risk(state):
    debate = state.get("risk_debate_state") or {}
    cols = st.columns(3)
    for col, (title, key) in zip(cols, (("Aggressive", "aggressive_history"),
                                        ("Neutral", "neutral_history"),
                                        ("Conservative", "conservative_history")), strict=True):
        with col:
            st.subheader(title)
            st.markdown(debate.get(key) or "_None._")


def _e3_summary_for(run_id: str) -> dict | None:
    """The E3 summary a stopped run saved (its file name carries the run ID), if any."""
    folder = Path(os.getenv("E3_RESULTS_DIR", "e3_results"))
    for path in folder.glob(f"*_{run_id}_e3.json"):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return None


def cost_text(cost: dict) -> str:
    """The E3 cost line: a figure is labelled an estimate only when E3 could compute one."""
    if cost["estimated_cost_usd"] is not None:
        return f"**E3 estimated cost:** ${cost['estimated_cost_usd']:.6f} ({cost['note']})"
    return f"**E3 estimated cost:** not available ({cost['note']})"


def _metrics(result):
    usage = result.usage
    cols = st.columns(4)
    cols[0].metric("Tokens in", f"{usage.get('tokens_in', 0):,}")
    cols[1].metric("Tokens out", f"{usage.get('tokens_out', 0):,}")
    cols[2].metric("Tool calls", usage.get("tool_calls", 0))
    e2 = usage.get("e2_cache")
    cols[3].metric("E2 cache hit rate", f"{e2['hit_rate_pct']}%" if e2 and e2.get("hit_rate_pct") is not None
                   else "n/a")
    st.markdown(run_budget.describe_snapshot(usage.get("run_budget")))
    st.markdown("**E1 backup-model switches this run:** "
                + (str(len(result.fallback_events)) if result.fallback_events else "none"))
    for line in result.fallback_events:
        st.caption(line)
    if result.e3_summary_path:
        try:
            summary = json.loads(result.e3_summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            summary = None
        if summary:
            st.markdown(cost_text(service.e3_cost(summary)))
            per_agent = summary.get("per_agent") or {}
            if per_agent:
                st.dataframe([{"agent": name, **{k: v for k, v in row.items() if k != "models"},
                               "models": ", ".join(row.get("models", []))}
                              for name, row in per_agent.items()], hide_index=True)
    else:
        st.caption("E3 tracing is off; turn it on with E3_TRACE_ENABLED=1 for latency and cost per agent.")
    st.caption(f"Saved: {result.usage_path}" + (f" and {result.e3_summary_path}" if result.e3_summary_path else ""))


# ------------------------------------------------------------------ paper trade
def paper_trade(result):
    broker, error = _broker()
    if broker is None:
        st.info(f"Paper trading is off: {error}")
        return
    try:
        limits = op.OrderLimits.from_env()
        panel = trade.load_panel(result, broker, limits)
    except (BrokerError, ValueError) as exc:
        st.error(str(exc))
        return

    cols = st.columns(5)
    cols[0].metric("Buying power", f"${panel.account.buying_power:,.2f}")
    cols[1].metric(f"{result.ticker} shares held", f"{panel.held_long_qty:g}")
    cols[2].metric("Last price", f"${panel.price:,.2f}" if panel.price else "n/a")
    cols[3].metric("Max order value", f"${limits.max_notional:,.2f}")
    if panel.clock is not None:
        cols[4].metric("Market", "Open" if panel.clock.is_open else "Closed",
                       help=None if panel.clock.is_open else f"Next open {panel.clock.next_open}")

    receipts = st.session_state.setdefault("receipts", {})
    sent = receipts.get(panel.client_order_id) or panel.already_submitted
    if sent:
        if isinstance(sent, dict):   # from the ledger: placed in an earlier session
            sent = (f"{sent.get('side')} {sent.get('qty')} {sent.get('symbol')}, "
                    f"order {sent.get('order_id')}, status {sent.get('status')}")
        st.success(f"This run's paper order was placed: {sent}. One order per analysis run.")
        return
    if not panel.proposal.allowed:
        st.warning(panel.proposal.reason)
        return
    st.markdown(f"**{panel.proposal.reason}**")
    identity = _identity()
    if identity is None and auth.auth_required():
        st.info("Sign in (sidebar) to approve a paper order.")

    side = panel.proposal.side.value
    kind = "limit"   # limit orders only: the fill can never pass the checked value
    if panel.clock is not None and not panel.clock.is_open:
        st.caption(f"The market is closed; a limit order placed now waits for the open ({panel.clock.next_open}).")
    qty = st.number_input("Quantity (whole shares, entered by you)", min_value=0, step=1, value=0,
                          key=f"qty-{result.run_id}")
    limit_price = st.number_input(
        f"Limit price ({'most you will pay' if side == 'buy' else 'least you will accept'})",
        min_value=0.0, step=0.01, format="%.2f",
        value=round(panel.price, 2) if panel.price else 0.0, key=f"limit-{result.run_id}",
        help="Limit orders only: a buy never fills above this price, so the order value is at most "
             "quantity x limit. Set it at or above the last price for an immediate fill.")
    check = trade.evaluate(panel, qty, False, order_type=kind, limit_price=limit_price, identity=identity)
    value = f"${check.notional:,.2f}" if check.notional else "n/a"
    stale_ack = False
    if panel.stale:
        stale_ack = st.checkbox(f"The analysis is for {panel.trade_date}, not today. Act on it anyway.",
                                key=f"stale-{result.run_id}")
    # The approval is keyed to the exact order: changing any part of it clears the tick.
    confirmed = st.checkbox(
        f"I approve a paper {kind} order: {side.upper()} {qty} {result.ticker}"
        + (f" at ${limit_price:,.2f} limit" if limit_price else "") + f" (up to {value}).",
        key=f"approve-{result.run_id}-{side}-{kind}-{qty}-{limit_price}")
    check = trade.evaluate(panel, qty, confirmed, stale_ack, order_type=kind, limit_price=limit_price,
                           identity=identity)
    for problem in check.problems:
        st.caption(f"- {problem}")
    # Step-up: the password again for this order; a signed-in but unattended browser cannot approve.
    password = st.text_input("Your password, to approve this order", type="password",
                             key=f"stepup-{result.run_id}", disabled=identity is None)
    if st.button("Submit paper order", type="primary", disabled=not check.can_submit or not password,
                 key=f"submit-{panel.client_order_id}"):
        try:
            identity = auth.confirm_password(identity, password, store=_sessions())
            st.session_state["identity"] = identity
            receipt = trade.submit(panel, result, broker, qty, confirmed=confirmed, identity=identity,
                                   order_type=kind, limit_price=limit_price, stale_acknowledged=stale_ack)
        except auth.AuthError as exc:
            st.error(f"Not sent: {exc}")
        except op.OrderRejected as exc:
            st.error("Not sent: " + "; ".join(exc.problems))
        except BrokerError as exc:
            st.error(f"Not sent: {exc}")
        else:
            receipts[panel.client_order_id] = (f"{receipt.side} {receipt.qty:g} {receipt.symbol}, "
                                               f"order {receipt.order_id}, status {receipt.status}")
            st.rerun()


# ------------------------------------------------------------------ page
def main():
    broker, broker_error = _broker()
    sidebar(broker, broker_error)
    sign_in_panel()
    st.title("TradingAgents")
    st.caption("Multi-agent stock research with human-approved Alpaca paper trading. No real money is used.")

    job = _current_job()
    running = job is not None and job.running
    if service.is_busy() and not running:
        st.info("An analysis started in another browser session is running; this one can start when it ends.")
    analyze_form(broker, busy=running or service.is_busy())
    if job is None:
        return
    if running:
        progress(job)
    else:
        results(job)


# Streamlit runs this page as ``__main__``. Each analysis worker is a spawned process, and
# spawn re-imports the parent's ``__main__`` file in the child as ``__mp_main__``; without
# this guard every worker would run the whole page (broker, sessions, widgets) in bare mode.
if __name__ == "__main__":
    st.set_page_config(page_title="TradingAgents", page_icon=":material/monitoring:", layout="wide")
    main()
