# Phase 3 changes (TradeDesk AI)

Every Phase 3 change, grouped by feature. In the code, look for `F1:` and `F2:` and the
`Phase 3` notes. E1 (model routing + backup), E2 (market-data cache) and E3 (tracing + cost)
are unchanged and apply to every interface; see `PHASE2_CHANGES.md`.

## Shared analysis service (used by the CLI, F1 and F2)

**What it does.** One function, `tradingagents.service.run_analysis(AnalysisRequest)`, runs an
analysis for any interface. It builds the graph (E1 routes models and adds backups there), attaches
the run counters and E3 tracing, streams the run with checkpoint resume, records the decision in the
memory log and writes `token_usage.json`. Interfaces only turn its events into a view.

- **Run ID and source label.** Every run gets a unique `run_id` (e.g. `20261121-153012-ab12cd34`) and a
  `source` (`cli`, `dashboard`, `mcp`, `api`). Both are written to `token_usage.json`, the E3 summary
  (and its file name) and the Langfuse trace metadata (`langfuse_session_id` = run ID, tag `source:<name>`).
  The paper-order audit line carries the run ID too.
- **Memory logs.** MCP runs record decisions in their own log (`mcp_trading_memory.md` beside the main
  one, or `TRADINGAGENTS_MCP_MEMORY_LOG_PATH`), so analyses other assistants request do not feed
  lessons into CLI/dashboard decisions. The CLI and the dashboard use the configured main log.
- **Shared E2/E3.** The E2 SQLite cache and E3 tracing are the same pipelines for all sources.
  `token_usage.json` now reports E2 counts *for that run only* (the counters are process-wide; the
  service subtracts the count at the start), which matters in a long-lived dashboard or MCP process.
- **No duplicate files.** A run writes the same files as before (state log, memory-log entry,
  `token_usage.json`, E3 summary when on). A repeat run on the same ticker and date replaces its
  `token_usage.json`. The E3 summary is saved once by the service and its exit hook is removed.
- **One run per process.** A second request while one runs gets `AnalysisBusyError` straight away
  (the dataflows config, E2 counters and E3 hook are process-wide).
- **Cancellation.** `cancel=threading.Event()` stops a run at its next step (the dashboard's Cancel
  button, the MCP timeout).

| File | Change |
| --- | --- |
| `tradingagents/service.py` | **New.** `AnalysisRequest`, `AnalysisEvent`, `AnalysisResult`, `run_analysis()`, `config_for_source()`, `progress_of()` |
| `cli/run.py` | The streaming loop moved to the service; the live view is now an `on_event` handler. Prompts, the live view, `message_tool.log` and the per-section report files are unchanged |
| `tradingagents/run_stats.py` | Moved from `cli/stats_handler.py` (which re-exports it) so the core package does not import the CLI |
| `tradingagents/e3_cli_hook.py` | Additive: optional `run_id`/`source`, `mark_failed()`, `saved_path`; an explicit `save()` unregisters the exit hook; status lines go to stderr (stdout is the MCP stdio channel) |
| `tests/test_analysis_service.py` | **New.** 20 offline tests: decision and files, run IDs, MCP memory log, E1 backup through the service, E2 per-run counts, E3 saved once / failed status, trace metadata, busy, cancel, input checks, CLI parity on the real graph |

**Behavior notes.** A crashed CLI run's E3 summary now says `status: failed` with the error, instead of
`process-exit (completion not established)`; a cancelled run says `status: cancelled`. Timing boundaries
are listed exactly in "E3 timing boundaries" below; the CLI's `wall_time_seconds` keeps its Phase 2 span.

## F1: Streamlit dashboard with human-approved Alpaca paper trading

**What it does.** A web page (`tradingagents-dashboard`, on `http://127.0.0.1:8501`) runs an analysis in
the background and shows: the analyst reports, the bull/bear research debate and Research Manager plan,
the trader's plan, the aggressive/neutral/conservative risk debate, the Portfolio Manager's decision, and
the run's metrics (tokens, E2 hit rate, E1 backup switches, E3 cost and per-agent latency). A person can
then place **one Alpaca paper order per analysis run**.

**Safeguards (all in `tradingagents/execution/`, all unit-tested):**

- Buy / Overweight may propose a buy. Underweight / Sell may only reduce or close an existing long
  position; with none, the sell is disabled and the page says why. No short selling, no margin, never a
  negative position. Hold and REVIEW create no order.
- The person types the quantity (whole shares). Nothing reads a quantity from the decision text.
- Every order stays under `TRADINGAGENTS_MAX_ORDER_NOTIONAL` (USD, default 1000); a buy stays within
  buying power, a sell within the shares held.
- Each order needs its own approval tick, keyed to the exact order (changing the quantity clears it).
  A decision for an earlier date needs a second tick.
- Duplicates are prevented three ways: the client order ID is derived from the run ID
  (`ta-<run_id>-buy`); the local ledger `<results_dir>/paper_orders.jsonl` is checked under a file lock;
  Alpaca is asked for an existing order with that ID before sending.
- **The backend is the gate, not the page.** `AlpacaPaperBroker.submit()` re-runs every check itself,
  immediately before sending, so a script that skips the dashboard gets the same answer:
  - the approval must come from `order_policy.approve()`: it is HMAC-signed over the exact order (a
    hand-built or edited `ApprovedOrder` is refused), names who approved it, and lapses after 5 minutes;
  - the cap is read from `TRADINGAGENTS_MAX_ORDER_NOTIONAL` at submission; a caller's `limits` can only
    lower it, and an invalid setting sends nothing;
  - quantity, buying power and (for sells) the long position are checked against fresh account data,
    and the notional uses a fresh latest price; with no fresh price nothing is sent;
  - an audit ledger is required; the `approved` line (with the numbers checked) is written before the
    order goes out, followed by `submitted` or `failed`.
- Paper only: the trading client is built with `paper=True` and no URL override, and its base URL is checked
  again before each submit. `ALPACA_PAPER=false` or a live base URL is refused.
- Only US-listed stocks are paper-traded (crypto pairs, foreign listings and indices are analysed only).
- The page binds to 127.0.0.1 by default, since it has no login of its own.

| File | Change |
| --- | --- |
| `tradingagents/execution/order_policy.py` | **New.** Pure rules: `propose_order`, `check_order`, `approve` → `ApprovedOrder`, `OrderLimits`, `client_order_id` |
| `tradingagents/execution/alpaca_paper.py` | **New.** The only module importing `alpaca`: `AlpacaPaperBroker` (account, positions, latest price, market clock, `submit`), `OrderLedger` |
| `tradingagents/execution/identity.py` | **New.** Signed sign-in identities: password (scrypt users file) |
| `tradingagents/dashboard/jobs.py` | **New.** Runs the analysis in a background thread (a Streamlit rerun cannot interrupt it); progress and a bounded activity log |
| `tradingagents/dashboard/trade.py` | **New.** The paper-trade panel's logic: load account and proposal, evaluate the person's entries, submit |
| `tradingagents/dashboard/app.py`, `launch.py` | **New.** The Streamlit page and the `tradingagents-dashboard` launcher |
| `tests/test_order_policy.py`, `tests/test_alpaca_paper.py`, `tests/test_dashboard_logic.py` | **New.** Safeguard tests with fake Alpaca clients (no network), including direct calls to the backend that bypass the page; a Streamlit `AppTest` smoke test |
| `tests/test_phase3_integration.py` | **New.** The MCP tool (over the protocol) and the dashboard page (via `AppTest`) on the real graph with E1 backups, E2 and E3 on; the dashboard sign-in and limit-order flow |
| `tests/test_limit_orders_and_identity.py` | **New.** Limit orders, market hours, the final cap check, real alpaca-py requests, password identities, limit-as-default and the exact cap invariant |
| `tests/test_e2_vendor_key.py` | **New.** E2 vendor-aware keys: switching vendors misses, Phase 2 keys and rows still hit |
| `tests/test_e1_e2_e3_audit.py`, `tests/test_phase3_validation.py` | **New.** E1 routing per agent, fallback accounting, E3 tokens and cost; restart voids sign-ins and approvals, localhost-only, E2 schema |
| `tests/test_worker.py`, `tests/worker_fakes.py` | **New.** The worker process with offline fakes inside the child: hung call cut off, slot released, overlapping runs refused, cooperative path, MCP timeout, dashboard job, warm-worker environment |
| `tests/test_sessions.py` | **New.** Server-side sessions (idle, absolute, revoked, restart), persistent limits, step-up, session-bound approvals |
| `tests/test_smoke_scripts.py` | **New.** The live smoke scripts, offline: nothing live without approval; their checks pass on a correct run |
| `tests/test_phase3_timing_and_cancel.py` | **New.** Cancellation (no new calls after cancel, E1 backup not used, E3 `cancelled`) and E3 timing boundaries |

**How to turn it on.** `pip install ".[dashboard]"`, then add paper-account keys to `.env`:
```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
TRADINGAGENTS_MAX_ORDER_NOTIONAL=1000
```

## F2: MCP server with a read-only `analyze_stock` tool

*Update 2026-11-25: four more read-only tools (`get_report`, `get_run_metrics`, `list_recent_runs`, `propose_trade`) were added; see "F2: four read-only MCP tools over saved runs" below. This section describes `analyze_stock`, which is unchanged.*

**What it does.** `tradingagents-mcp` serves one MCP tool, `analyze_stock(ticker, trade_date?, analysts?,
include_reports?)`. It returns the rating, whether it needs review, the decision, the research plan and the
trader plan (each truncated to `TRADINGAGENTS_MCP_MAX_REPORT_CHARS`, default 4000), usage, E1 switch count
and E3 cost (`estimated_cost_usd` with `cost_basis`, `partial_cost_usd` and `cost_note`; see "E3 cost
reporting in the dashboard and MCP" below). It runs through the shared service with `source="mcp"`.

- **Read-only.** The module never imports the execution layer or Alpaca (a test checks every import,
  including those inside functions). Tool annotations: `readOnlyHint=true`, `destructiveHint=false`.
  It writes only its own analysis records and its own memory log.
- **Authentication.** stdio (the default) is launched by the assistant with your environment. HTTP refuses
  to start without `TRADINGAGENTS_MCP_TOKEN` (16+ characters), checked in constant time as
  `Authorization: Bearer <token>`, and binds to 127.0.0.1 by default.
- **Long runs and timeouts.** Progress notifications as each report is filed. After
  `TRADINGAGENTS_MCP_TIMEOUT_SECONDS` (default 900) the tool returns an error and the run stops at its next
  step, freeing the server. A client that cancels also stops the run.
- **Errors.** Bad input, busy server, timeout and provider failures come back as MCP tool errors with a short
  reason; nothing is printed to stdout.

| File | Change |
| --- | --- |
| `tradingagents/mcp_server.py` | **New.** `build_request`, `analyze_stock_sync`, `summarize`, `create_server`, `api_key_verifier`, `main` |
| `tests/test_mcp_server.py` | **New.** Validation, answer shape and size, layering (read-only), token check; protocol tests via FastMCP's in-memory client |

**How to use it.** `pip install ".[mcp]"`, then point an MCP client at the command `tradingagents-mcp`
(stdio). For HTTP: `TRADINGAGENTS_MCP_TOKEN=<secret> tradingagents-mcp --transport http --port 8765`.

## Phase 3 hardening (limitations resolved)

### F1: limit orders only, market hours, final cap check

- **Limit orders only.** Market orders were evaluated and disabled: a market order's fill price is unbounded,
  so no check made before sending can keep it under `TRADINGAGENTS_MAX_ORDER_NOTIONAL` (the earlier 2% slippage
  buffer narrowed the gap but could not close it, e.g. on a gap open). A buy limit never fills above its limit,
  so `qty x limit price <= cap` is a hard bound. Cost: a limit order may not fill if the price moves past it;
  for an immediate fill, set the limit at or above the last price (it still never pays more than the limit).
  Market orders are refused at every layer: `check_order`/`approve` (`MARKET_ORDERS_DISABLED`), the broker
  (`_order_request` builds only `LimitOrderRequest`) and the final check (a non-limit request is refused).
  The dashboard offers no order-type choice, and `TRADINGAGENTS_MARKET_ORDER_BUFFER_PCT` is gone.
- An order that names no limit price is refused ("Enter a limit price").
  `qty x limit price <= TRADINGAGENTS_MAX_ORDER_NOTIONAL` is compared in exact decimals (`order_policy.exceeds`),
  so binary-float rounding cannot let an order past the cap or wrongly refuse one at it (checked on 4,000 seeded
  random orders and the boundary cases).
- A buy limit is valued at its limit, so a jump in the quote cannot push it past the cap. A sell limit is valued
  at the higher of its limit and the latest price. Limit prices must be whole cents (four decimals under $1).
  The limit price is part of the signed approval; changing it voids the approval.
- **Regular session only**: DAY orders with `extended_hours=False`. A limit order placed after hours waits for
  the next open (the ledger records `market_open` and `next_open`).
- **Final cap check** (`alpaca_paper._final_cap_check`): immediately before `submit_order`, independently of
  the other checks, the cap is re-read from the environment and the worst-case value recomputed from the
  request object actually being sent; a request that differs from the approved order (quantity, side, type,
  limit, client order ID) is refused.
- Bug found and fixed while testing with real alpaca-py request objects: alpaca-py stores `qty` as a float,
  which an integer-only check would have rejected for every real order.

### F1: signed-in approvers (`tradingagents/execution/identity.py`)

- The OS username and the approval checkbox are no longer treated as identity. A paper order needs a verified
  sign-in unless `TRADINGAGENTS_REQUIRE_ORDER_AUTH=false` (then it is recorded as `auth_method: none`).
- **Password** sign-in: a local users file of scrypt hashes (`TRADINGAGENTS_DASHBOARD_USERS`), constant-time
  comparison, the same answer and timing for an unknown user as for a wrong password, a lockout after five
  failures. This is the only sign-in method: the dashboard serves localhost only, so no external identity
  provider is used (an earlier OIDC option was removed as unnecessary and untestable offline).
- Identities are HMAC-signed with a per-process key and lapse after 8 hours. **A dashboard restart voids every
  earlier sign-in and pending approval** (a new key; tested by signing in a separate process). `approve()` and the broker both
  verify them; the approver and method go into the signed approval and every ledger line.
- What this proves: the browser session belongs to someone who signed in. It does not defend against code
  already running inside the dashboard process, which could call the sign-in functions itself.

### F1: the dashboard's trust boundary (single process)

The dashboard is one local process, serving 127.0.0.1 only (the launcher refuses any other
`--server.address`). Inside that boundary:

- **Trusted:** everything running in the dashboard's Python process: Streamlit, the page code, the shared
  analysis service, the execution layer, and the per-process keys that sign sign-ins and approvals. Code in
  this process can call the sign-in and approval functions itself; the safeguards do not defend against it.
- **Not trusted, and checked:** the browser session. It must sign in (scrypt password file); it can only
  submit through `trade.submit` → `approve()` → `AlpacaPaperBroker.submit()`, which re-checks the approval
  signature, the signed-in approver, the cap, buying power, holdings, the market clock and duplicates with fresh
  data, then sends a limit order to the paper endpoint only.
- **Outside the boundary:** other local users and processes are kept out by the loopback bind and the sign-in,
  not by the OS; anyone who can run code as the dashboard's OS user is inside the boundary. Alpaca keys and the
  users file are read from the environment and disk with the OS user's permissions.
- **Sessions are server-side** (`identity.SessionStore`, SQLite, mode 0600): the browser session holds a random
  token, the store keeps only its hash, and every check reads it: 8 h absolute and 30 min idle expiry, revocation
  at sign-out, and a per-start instance ID, so a restart voids every session and pending approval. Failed
  sign-ins are limited in the same store (5 per user and 30 overall per 15 min), so the limit survives restarts.
- **Approving an order needs the password again** (step-up, valid 5 minutes): a signed-in but unattended browser
  cannot approve. The approval is bound to the session, and the broker re-checks at submission that the
  session is still active (signing out between approval and submission stops the order).
- **Order submission stays in the dashboard process.** A separate order process running as the same OS user
  was evaluated and not built: it could still read `.env` and the users file, so it would add an IPC protocol
  without moving the trust boundary, which is the OS account. Real isolation needs a separate OS user (a
  deployment decision). OIDC was evaluated and not added: the dashboard is single-user on localhost.

### F2: HTTP exposure

stdio remains the recommended transport. HTTP binds to 127.0.0.1 and needs a 16+ character token. Any other
address (including `0.0.0.0`, `::`, LAN addresses and host names) is refused unless **all** of
`--allow-remote`, `--tls-cert` + `--tls-key` (HTTPS via uvicorn) and a 32+ character token are given.
The stdio transport rejects the HTTP-only flags.

### E3 timing boundaries (exact)

| Metric | Phase 2 (CLI) | Phase 3 CLI | Phase 3 dashboard / MCP |
| --- | --- | --- | --- |
| E3 `wall_time_seconds` | E3 session created (after the setup questions, before the graph is built) to the `atexit` save at process exit, after the report prompts | session created (after the setup questions, before the graph is built) to the explicit save right after the report prompts. Same span; the Phase 3 start is a few ms later (the live view opens first) and the end a few ms earlier (no interpreter teardown) | session created to the end of the analysis (no prompts exist) |
| `token_usage.json` `elapsed_seconds` | after graph construction to the end of the run loop | after graph construction to the decision recorded: unchanged | same |
| E3 `pre_analysis_seconds` (new) | n/a | session created to graph construction start | same |
| E3 `analysis_seconds` (new) | n/a | graph construction start to the decision recorded and the checkpoint closed | same |
| E3 `post_analysis_seconds` (new) | n/a | analysis end to the save (report prompts, saving reports) | ~0 |

Each summary also carries `timing_boundaries` with these definitions. Existing E3 fields (names, schema
`tradingagents-e3-summary-v1`, per-call and per-agent attribution, token counts, cost fields) are unchanged;
a test pins that every Phase 2 field is still present. Phase 2 benchmark numbers remain comparable with Phase 3
CLI runs; dashboard and MCP `wall_time_seconds` exclude interaction because there is none.

### Cooperative cancellation

`cancel` is checked before the run starts, before graph construction, before the instrument lookup, before
the workflow, between workflow steps and before the decision is recorded. Inside the graph,
`service.CancellationGuard` refuses every new model or tool call once cancel is set, including inside
parallel analysts and the memory step. It raises a `BaseException` subclass so neither the agents'
`except Exception` fallbacks nor E1's `with_fallbacks` treat a cancel as a model failure (the backup model is
not called; nothing is written to the E1 log). **A model call already in flight is not interrupted**: no
provider SDK used here can abort a request. LangGraph waits for running tasks while the graph unwinds, so the
service keeps its process lock until every in-flight call has returned (it is billed; its result is
discarded): no second analysis can start before the first has released its resources, which a deterministic
test pins (a call held in flight after the cancel; a second run is refused as busy until it returns). The MCP
tool still answers its timeout at once and refuses new requests as busy until the drain ends.
`AnalysisCancelled` reports the calls that were in flight, the calls refused, and `released_after_seconds`
(the wait); E3 records `status: cancelled` with the calls that ran.

### E3 cost when E1 falls back (additive fix)

`estimated_cost_usd` is `null` whenever any call failed without reporting usage, which is every run where E1
switched to the backup: 7 of the 17 E3 summaries saved under `eval/`, `consistency_evaluation/` and
`e3_live_comparison/` (e.g. `eval/phase2/05_MSFT`: 4 errors, no estimate). It stays strict (a failed call can
still be billed). Two fields are added: `failed_calls_without_usage` and `estimated_cost_usd_successful_calls`
(set when every successful call is priced). Replaying the stored Phase 2 records through the new code leaves
every existing `estimated_cost_usd` unchanged and fills the new field for all 6 runs that had failures.
How the dashboard and MCP server report these fields is described under "E3 cost reporting in the
dashboard and MCP" below.

### E2 vendor provenance (defect fix)

**Defect:** the cache key was `(tool, request)` with no vendor, so after switching `data_vendors` or
`tool_vendors` a repeated past-dated request was answered with the previous vendor's data
(`tests/test_e2_vendor_key.py` fails on the Phase 2 code). A Phase 2 row cannot say which vendor wrote it.

**Fix:** every key now carries the resolved vendor chain (`{"vendors": "yfinance"}` inside the `request`
column), including the default chain; `e2_cache` resolves it itself, so no caller can write or read a
vendor-less key, and `dataflows/router.py` is back to its Phase 2 code. **Phase 2 rows are never served
on trust:** their keys cannot match, so a Phase 2 cache starts cold once under Phase 3.

`python -m tradingagents.e2_cache adopt-legacy --cache-dir D --results-dir R` re-keys Phase 2 rows only
when the runs' own records account for them: every state log under `R` records the same `data_vendors` and
`tool_vendors`; no run folder there shows activity without a state log; no row was saved after the newest
state log; and the operator confirms `--only-these-runs-used-this-cache` (files can show consistency, not
prove that nothing else used the cache). Otherwise nothing is adopted. Rows are adopted under the proven
vendors only, never under the defaults by assumption; legacy rows are left in place, never deleted. Run on a
copy of `eval/tracing_31`, adoption is **refused**: its second run (`02_NVDA_trace_off`) used the cache
without leaving a state log, and the cache rows predate the one recorded run, so none of its 17 rows is
served. The eval folders were not modified.

Schema, the past-dates-only rule and the counters are unchanged. Offline benchmark (14 requests, median of
30, three repeats): warm 4.0-5.4 ms vs 3.6-4.0 ms for the Phase 2 code (resolving the vendor chain reads the
config once per lookup, under 0.1 ms per request); cold 10-12 ms vs 10-11 ms. Phase 2's live benchmark for
the same 14 requests was 2.67 s cold (network).

**Cache accounting differs from Phase 2 (counters only).** In the matched offline replay (E1 fallback on, E2
cold then warm, the same scripted models and data), every token count, per-agent figure, provider call and the
E1 fallback log (30 lines) are identical between the Phase 2 and Phase 3 code. Only the warm run's E2 counters
differ: Phase 2 reports 13 hits and 11 misses (24 lookups, 54.2%), Phase 3 reports 12 hits and 0 misses (100%).
Because the provider calls are identical, the same data reached the agents in both; the difference is in how
lookups are counted now that the key carries the vendor chain. E2 hit rates from Phase 2 and Phase 3 runs are
therefore not directly comparable; compare provider calls or data time instead.

### E2

Otherwise unchanged: same cache schema (pinned by a test), the same past-dates-only eligibility rule, the same counters. Today's requests
are still never cached; that is deliberate (today's data can still change) and was not altered to raise hit
rates. A same-day, in-run cache was investigated and not built: across 26 saved runs (99 tool calls) no
identical request repeated within a run, so it would save nothing; reusing today's answers across runs would
serve prices that are still moving. Router calls are all positional, so equal requests always make equal keys.

## Cancellation in a worker process (dashboard and MCP)

**Weakness:** cancellation was cooperative only. No new step or call started after a cancel, but a provider call
already in flight could not be interrupted (the agent nodes are synchronous, so neither asyncio cancellation nor
LangGraph can stop them), and the server's one analysis slot stayed taken until it returned: up to the request
timeout (600 s by default for Gemini), and never for a call that hangs.

**Evaluated:** async cancellation (needs every agent rewritten as async; rejected); lower provider timeouts
(still waits for each call, risks failing legitimately slow calls, changes E1's validated client settings; kept
as the existing optional `TRADINGAGENTS_LLM_TIMEOUT`, default unchanged); a process-isolated worker (chosen).

**Design** (`tradingagents/worker.py`): the dashboard and MCP server run each analysis in a spawned child
process. On cancel the parent asks the child to stop cooperatively (the usual path: the child finishes its
in-flight calls, saves its own E3 summary and exits). After `TRADINGAGENTS_CANCEL_GRACE_SECONDS` (default 10)
it terminates the child: the OS reclaims its threads, sockets and file locks and the slot is released. The
server's own state is never touched by the run. The parent resolves the request's configuration and passes its
environment, so a warm worker runs exactly as a fresh one would. One warm worker is kept ready, so the child's
import time is paid ahead of the next request. The CLI stays in-process (Ctrl-C ends the process anyway).

**Recording a terminated run accurately:** the child streams every model call's start and end to the parent;
the parent writes the E3 summary (`status: cancelled`, `terminated: true`, the calls in flight named,
`langfuse_flushed: false`). Calls in flight were abandoned, not undone: **the provider may still bill them**,
and the summary says so.

**Measured** (offline, a model call that never returns; `tests/test_worker.py` pins the behavior):

| | Before (in-process) | After (worker) |
| --- | --- | --- |
| Call returns 8 s after the cancel | slot released after 8.08 s | released after the grace period at most |
| Call never returns | never released (the process could not even exit) | 2.07-2.14 s (grace 2 s), 10.08-10.13 s (grace 10 s) |
| Second analysis while it is held | refused as busy | refused as busy until released, then runs |
| Worker start-up | none | cold 2.4-2.8 s to the first event; warm 0.08-0.12 s |

### Worker reliability and E3 on termination

- **Readiness:** a worker reports `ready` (with its start-up time) before it takes a request; the parent waits at
  most `TRADINGAGENTS_WORKER_READY_TIMEOUT` (60 s), logs and replaces a worker that does not report or crashes
  during start-up, retries once, then raises `WorkerStartupError` with each worker's reason. The slot is freed.
- **Watchdog:** the child sends a heartbeat every second during a run; one silent for
  `TRADINGAGENTS_WORKER_HEARTBEAT_TIMEOUT` (30 s) is terminated (`WorkerUnresponsive`; E3 status `failed`).
  A model call that merely hangs does not trip it (the process is alive); cancellation handles that case.
- **Warm spare:** a dead or unready spare is logged and replaced on use. Starting a worker imports code only: no
  model client is built and no Langfuse client is made before a request (tested), so a spare costs no API calls.
- **E3 when terminated:** every model call's start and end reaches the parent as it happens; the parent's records
  match E3's own per-call records exactly (tested). Completed calls keep their reported tokens and cost; calls in
  flight are `complete: false` with no tokens (nothing estimated), and the summary carries
  `trace_complete: false`, `incomplete_calls` and the billing note.
- **Langfuse:** on cancel the child flushes at once, bounded to half the grace period, so completed calls' traces
  are exported before any termination (`langfuse_flush: ok`); a slow Langfuse is cut off (`timed_out`). E3's own
  save bounds its flush too (`E3_FLUSH_TIMEOUT_SECONDS`, 10 s) and records the outcome. Spans of calls still in
  flight stay unfinished in Langfuse: no "incomplete" marker is written there; the E3 summary is the record.

Measured offline (`tests/worker_fakes.py` models): cold start to ready 2.4-2.7 s (median 2.4-2.5 s, n=5); warm
spare hand-over 0.00 s; spare ready again 2.4-2.7 s after a run; cooperative cancel to release 1.2-2.0 s with
0.3 s model calls; hung call with a 2 s grace released after 2.08-2.12 s.

### Phase 2 vs Phase 3 NVDA latency (offline, deterministic replay)

Reference runs: `consistency_evaluation/nvda/run_1-2` (cached data: 27.3-28.1 s) and `nvda_live/run_1`
(live data: 36.7-40.6 s); market analyst only, 1 debate / 1 risk round, `gemini-3.5-flash-lite`, E2 off. Their
LLM time was 26.1-26.9 s in every run; live data added 9.8-14.2 s of non-LLM time.

Replay: each model call sleeps the latency recorded for that call in `run_1` and reports its recorded tokens
(12 calls; the market analyst's 3 tool rounds and report); data requests answer offline at once ("cached") or
after 2.0 s each ("live": 6 requests, matching the reference runs' ~12 s). Same inputs for both code trees.

| Condition | Phase 2 analysis | Phase 3 CLI | Phase 3 worker (warm / cold) |
| --- | --- | --- | --- |
| Cached data, E2 off | 27.1-27.3 s | 27.1 s | 27.2-27.3 s analysis; +0.14 s warm, +2.3 s cold |
| Live data, E2 off | 39.2 s | 39.2 s | - |
| Live data, E2 cold -> warm | 39.3 -> 29.1 s | 39.4 -> 29.3 s | - |

Every run: 12 LLM calls, 34,504 input / 5,360 output tokens, $0.023751 (the reference run's own figure), E3
complete (all calls attributed, E3 calls and tokens equal token_usage, status success). E2: 5/5 hits warm in both
trees; the one remaining data request is the verified snapshot, which reads prices outside the cached router.

Fixed on the way: the server waited ~1.3 s for each finished worker's interpreter teardown before returning;
the slot is now released once the result is in and the child exits in the background (warm overhead 1.46 s ->
0.14 s). Also fixed: a pool closed while its spare was still starting kept that spare alive until the
process exited; a closed pool now stops it. Remaining: a request arriving within ~2.5 s of the previous
one pays the next spare's start-up.

## Final-validation evidence (offline)

**Which models Phase 2 used** (from the E3 per-call records, which record the model that answered, and
`token_usage.json`): `eval/phase2`, `phase2-rerun`, `tracing`, `final_validation`, `consistency_*` and
`e3_live_comparison` ran `gemini-3.5-flash-lite` on both tiers; every E1 switch went to `gemini-3.1-flash-lite`.
`eval/tracing_31` is a separate run with `gemini-3.1-flash-lite` on both tiers. `eval/forced_failure` used a
missing model with `gemini-3.1-flash-lite` answering all 10 calls. The only mixed-routing run is
`hybrid_msft_test` (deep `gemini-3.5-flash`): deep-agent LLM time 24.6 s vs 3.7-4.3 s in the same-model MSFT
runs; its cost cannot be computed because `e3_pricing.json` has no `gemini-3.5-flash` price (its
`known_usage_cost_usd` covers the quick calls only). One run says nothing about report quality.

**E1 routing benchmark** (`scripts/e1_routing_benchmark.py`, designed, not run): arm A same model, arm B a
stronger deep model on the two judgment agents only; NVDA/AAPL/MSFT on 2026-10-23, 3 repeats per arm per ticker,
interleaved; shared warmed E2 cache so market data is identical and untimed; E1 backup off; per-run model
override (configuration unchanged); hard per-analysis budget. Measures cost, deep-agent and total LLM latency,
tokens, rating stability, decision completeness, and exports shuffled decisions for a blind human review. Its
pre-flight refuses to run without a price for both models. `--analyze` works offline on finished runs.

**E3 cost replay:** all 32 saved summaries recomputed with the final code: the strict `estimated_cost_usd`
is unchanged wherever pricing was supplied; 13 runs that had no figure now get
`estimated_cost_usd_successful_calls`, each matching a hand calculation; the mixed run stays `null` (unpriced
deep model; nothing is invented).

**E3 metrics, local vs Langfuse:**

| Verifiable locally (E3 JSON, `token_usage.json`, tests) | Depends on Langfuse (needs the live smoke test) |
| --- | --- |
| per-call model, latency, tokens, errors; per-agent attribution | that traces are ingested and visible |
| E3 vs token_usage call and token totals | grouping by `session_id` = run ID, and the `source:` tag |
| cost estimates from `e3_pricing.json`; fallback cost | Langfuse's own cost figures (its model price table) |
| timing phases, run ID, source, status (success/failed/cancelled) | the trace tree (spans per node) and UI latency |
| that the metadata keys are sent with the right types (checked against the installed handler's source) | export reliability and flush on exit in a live process |

**`.gitignore`:** four narrow rules: `/results/phase3_smoke/` (smoke-test output, anchored),
`dashboard_users*.json` (password hashes), `dashboard_auth.sqlite*` (sessions and sign-in limits) and
`paper_orders.jsonl` (order ledgers). `tests/test_sessions.py` checks they match those files and nothing under
`eval/`, the E2 benchmark CSVs or E3 summaries. Note that Phase 2's existing `*.log` and report rules already
exclude parts of `eval/tracing_31` (`console.log`, `message_tool.log`, report `.md` files) from git.

## Real-interface validation and fixes (2026-11-23)

Both interfaces were run through their real entry points (`tradingagents-dashboard` in headless Chromium,
`tradingagents-mcp` over stdio with a real MCP client), offline: started from an empty folder with no
`.env` and no API keys, scripted models (a main model that always fails, so E1 switches to a priced
backup), offline market data, a stand-in Langfuse and, for the dashboard, an in-memory paper broker that
records any order submission. Analyses ran in the default worker-process mode, not the inline mode the
unit tests use. This found one product bug and one cost-reporting defect, both fixed below.

### Dashboard: every analysis worker ran the whole page (fixed)

Streamlit runs `app.py` as `__main__`; spawn re-imports the parent's `__main__` file in each worker as
`__mp_main__`, and `app.py` called `st.set_page_config(...)` and `main()` unconditionally. Every worker and
warm spare therefore ran the page in bare mode: it built the Alpaca broker from the environment, opened the
session store and rendered widgets into nothing, logging about 100 Streamlit warnings each (218 in one short
session). The unit tests did not see it because `tests/conftest.py` runs analyses inline. **Fix:** both
calls are now under `if __name__ == "__main__":` (Streamlit and AppTest still run the page; workers only
define it). `tests/test_dashboard_logic.py` loads the page as `__mp_main__` and requires that no page code
runs (it fails on the previous code). After the fix: 0 bare-mode warnings.

### E3 cost reporting in the dashboard and MCP (fixed)

Both interfaces showed `estimated_cost_usd or known_usage_cost_usd`. After an E1 fallback the strict field is
`null`, so they showed `known_usage_cost_usd`, which sums only the calls that could be priced: with an
unpriced model it is a partial figure, and it was labelled as the estimate (for a partly priced test run,
$0.0012 shown as the cost). Both now use one rule, `service.e3_cost()`:

| Run | Reported estimate | `basis` |
| --- | --- | --- |
| every call priced and reported tokens | `estimated_cost_usd` | `all_calls` |
| every successful call priced; only failed calls without tokens (E1 switches) left out | `estimated_cost_usd_successful_calls`, with a note that those calls are excluded and may still be billed | `successful_calls` |
| some successful call unpriced or without tokens (partially priced or unpriced) | none; any priced subtotal is `partial_cost_usd`, labelled a lower bound, not the cost | `incomplete` |
| E3 tracing off (or its summary unreadable) | none | `unavailable` |

The dashboard prints the estimate with its note, or "not available" with the reason. The MCP answer keeps
`estimated_cost_usd` (now `null` unless an estimate exists) and adds `cost_basis`, `partial_cost_usd` and
`cost_note`. The E3 summary fields themselves are unchanged. `tests/test_e3_cost_reporting.py` (13 tests)
builds real E3 summaries for every case (including a zero-cost run and a Phase 2 summary without the newer
field) and checks the rule, the dashboard text and the MCP answer.

### Test fakes on Python 3.14 (fixed)

`tests/worker_fakes.py` imported `Runnable` inside a function but annotated the scripted model's
`bind_tools` with `-> Runnable`. E1 with a backup model calls `with_fallbacks(...).bind_tools(...)`, which
evaluates that annotation; Python 3.14 resolves it in the module's globals, so it raised `NameError`. The
worker tests never set a backup, so they did not reach it. The import is now at module level, and
`tests/test_worker.py` binds tools through a fallback pair (it fails on the previous file). Product code
was not affected.

### Results

| Interface | Verified through the real interface |
| --- | --- |
| Dashboard (Streamlit, headless Chromium) | page loads with no console or page errors, no failed requests and no request to any host other than 127.0.0.1; sidebar shows models, E1 backups, E2 on, E3 on and the $1,000 cap; sign-in refuses a wrong password; an analysis completes through the worker with all report tabs; Run metrics shows the E1 switches (15), E2 hit rate (cold, then 100% warm) and the E3 cost with its basis ("not available" with an unpriced backup); trade panel: quantity starts at 0, Submit stays disabled until valid, an over-cap order shows the backend's limit message, changing the order clears the approval, an old analysis date needs an extra acknowledgement, a wrong step-up password is refused with nothing reaching the broker; a second browser is not signed in, sees none of the first one's results and cannot approve; a mid-run cancel is reported and recorded as `cancelled`, and the form works again; stopping the server leaves no worker processes |
| MCP (stdio, FastMCP client) | one tool at the time of this test (2026-11-22), `analyze_stock`; `ticker` required; read-only, non-destructive; no resources or prompts; a call returns rating, run ID, `source: mcp`, usage with E2 counters, fallback count and the cost with its basis (`successful_calls`, or `incomplete` with an unpriced backup); reports only on request; progress notifications; empty, path-like, future, malformed, unknown-analyst, wrong-type, missing and extra arguments are refused without internal details and the server keeps working; a second call during a run is refused as busy; a call cancelled after the analysis started frees the server and is recorded as `cancelled`; separate sessions get separate servers and results; worker output goes to stderr, never the protocol stream; only the MCP memory log is written; no server or worker process remains after a client disconnects; no order was submitted |

A run cancelled before it starts (for example while waiting for a fresh worker) leaves no E3 summary: the
service checks for a cancel before it creates the E3 session, and such a run made no model calls.

**Observed limitation (cause not confirmed):** one cold worker start took about 28 s before the analysis
began; other cold starts in the same sessions took about 2-7 s, and a bare interpreter start with the test
fakes takes about 2.2 s. It did not recur in later restarts. Possible contributors are recompiling edited
modules or a cold file cache under WSL, but neither was confirmed.

**Limits of this validation:** scripted models and data (real latency and report content not exercised); a
fake paper broker, and the only Submit click used a wrong step-up password; headless Chromium on Playwright's
Ubuntu 24.04 fallback build (Ubuntu 26.04 is not supported by Playwright 1.52); no live Gemini, Langfuse or
Alpaca. The test harness (bootstrap, browser and MCP scripts) is not part of the repository.

## Per-run LLM limits for dashboard analyses (2026-11-23)

The dashboard had no per-run ceiling: its only brake was the Cancel button. The smoke script's `BudgetGuard`
(scripts/smoke) bounded only that script. `tradingagents/run_budget.py` now gives the shared service an
opt-in limit, a maximum number of LLM calls and an estimated-cost ceiling, carried on the request
(`AnalysisRequest.budget`, a `RunBudget`) so it is enforced inside the worker process that makes the calls.

**Defaults.** Dashboard runs: 15 LLM calls and $0.04, set by `TRADINGAGENTS_DASHBOARD_MAX_LLM_CALLS` and
`TRADINGAGENTS_DASHBOARD_MAX_COST_USD` (a number, or `0`/`off` to switch one off; anything else is refused, so a
typo never removes a limit). These defaults suit a one-analyst, one-round run like the validation below; a
four-analyst run makes about 15 calls on its own and needs a higher call limit. The CLI sets no budget, so it
behaves as before; a request without a budget adds no callback and no output fields. *Correction (2026-11-24):*
at the time of this section the MCP server also set no budget (`budget=None`); it has had its own limits since
"F2: per-run LLM limits for MCP analyses" below.

**How it is enforced** (`BudgetGuard`, first in the run's callbacks, before the cancel guard, statistics and E3):

- *Admission at call start, atomically.* Each model call is admitted or refused at its own start under one
  lock, so parallel analysts cannot pass a limit together (the smoke guard's design). A refusal raises the
  service's cancel signal (a BaseException, so E1 cannot send the refused call to the backup model) and sets the
  run's cancel event, so no further agent step, model call or tool call starts. A refused call never starts, so
  it appears in no count, E3 record or trace.
- *What counts as a call.* Every call LangChain starts: E1 primary calls, E1 backup calls after a primary
  failure, and failed calls. Retries inside a provider SDK are not visible (see limitations).
- *Cost: reserved, then reconciled.* At admission a call reserves an estimate: its prompt at 3 characters per
  token plus 2,000 output tokens, at its model's price. A call is refused if the cost already charged, plus the
  reservations of calls still running, plus its own would pass the ceiling, so calls in flight are counted
  before they return. When a call ends, its reported usage replaces the reservation. A successful call without
  usage keeps its full reservation; a failed call without usage is charged its input estimate (a failed request
  may still be billed for input).
- *Unpriced is never free.* A model missing from `E3_PRICING_JSON` is charged at the highest price in the file.
  A cost ceiling with no pricing at all is refused before the run starts (the dashboard shows why).

Compared with the smoke guard, this one also counts calls still running, charges calls without usage and
failed calls, prices unknown models conservatively, and reports its stop separately. The smoke script keeps its
own guard unchanged, so its recorded runs stay reproducible.

**Reported separately from a cancel or a failure.** A stop by the limit raises `AnalysisBudgetExceeded` (a
subclass of `AnalysisCancelled`, so every existing stop path still releases the run and the worker). Its
message names the limit ("was stopped by its per-run limit (call limit reached: 15 of 15 LLM calls used)").
E3 records `status: "budget_exceeded"` with the limit's accounting in a new `run_budget` block, and the
dashboard job gets status `budget_exceeded`. Through the worker, the child reports the trip at once, so the
parent starts its grace timer. If a model call hangs, the worker is terminated after the grace period, exactly
as for a cancel, and the stop is still reported as a budget stop. A completed run records the same accounting
under `run_budget` in `token_usage.json` and its E3 summary.

**Dashboard.** The sidebar shows the limits ("Per-run limits: 15 LLM calls, $0.04 estimated cost"), or the
configuration error. A run in progress shows them. Run metrics shows the use ("Per-run limits: 9 of 15 LLM
calls; $0.0201… of $0.04 budget estimate"), next to the E3 cost line. A run stopped by the limit says "Stopped
by the per-run limit, not by you and not by a model error", with the accounting and the run's E3 cost.

**Tests** (`tests/test_run_budget.py`, 24): the guard call by call (completion, call limit, cost limit with
calls in flight, reconciliation, unpriced models, missing usage, E1 fallback, 20 concurrent starts against a
limit of 5, a cancel by the person, no pricing, settings); the real graph through the service (completion,
call limit, a cost limit computed to stop at exactly 4 calls, fallback, calls without usage, cancel by the
person, four parallel analysts against a limit of 3, no budget = no change for the CLI); the worker
process (a budget stop crossing the process boundary; a budget stop with a hung call terminating the worker);
the dashboard (job status; the real page through AppTest showing the limits and a budget stop). Two existing
page tests now switch the limits off, because they test other things without a pricing file (which a cost
limit needs): `test_dashboard_page_runs_an_analysis_with_e1_e2_e3` (two analysts with a failing main model,
over 15 calls) and `test_dashboard_paper_order_needs_a_signed_in_user` (sign-in and orders).

**Limitations.** The ceiling bounds the budget *estimate*, not the provider's bill:

- a call that writes more than 2,000 output tokens can take the estimate past the ceiling by the difference
  (no `max_tokens` is set);
- calls already running when a limit trips are not interrupted (no provider SDK here can abort a request);
  they are charged when they return;
- retries inside the provider SDK happen within one counted call, so the call limit cannot refuse them. For
  Gemini they are now bounded for the dashboard and MCP (2 backup attempts) and counted as
  `sdk_retries_observed` (see the follow-up section); a retried 429 or 503 is normally not billed;
- the 3-characters-per-token input estimate is approximate, and prices are only as current as
  `E3_PRICING_JSON`.

## Failed-run reporting, and the live F1 Gemini 503 incident (2026-11-23)

**The incident.** The one authorized live dashboard analysis (`results/phase3_smoke/dashboard_live_20261123_113727`,
run `20261123-113910-4266de44`; NVDA, 2026-10-23, Market analyst, Shallow; limits 15 calls / $0.04; Alpaca keys
blanked) did not complete. The Market Analyst's two calls succeeded (7,021 input / 1,464 output tokens). Then the
Bull Researcher's call to `gemini-3.5-flash-lite` returned **HTTP 503 UNAVAILABLE** ("This model is currently
experiencing high demand"), E1 switched to `gemini-3.1-flash-lite`, and the backup returned 503 as well, after
36.1 s inside the Gemini client's own retries. The run stopped as a model failure after 47.9 s: 4 calls,
successful-calls estimate $0.005766, neither limit reached, E3 status `failed`, Langfuse flush `ok`, 25 Langfuse
observations under the run's session. The cause was provider capacity, not the application; it was not retried.

It exposed three gaps, fixed here:

1. *A failed run kept almost nothing.* Only the E3 summary was written; there was no `token_usage.json`, no E2
   counters, no budget accounting, and the dashboard showed only the error, without the run ID.
2. *The browser driver could not see a failure.* It waited only for a result, so it waited the full 300 s.
3. *Unattended start-up could block.* Started under a real home folder, Streamlit stopped at its first-run
   e-mail prompt and exited before serving (this happened once before the run; no analysis had started).

**Failed runs now keep what they recorded** (`service._record_failure`). On any failure that is not a cancel or a
budget stop, the service still saves the E3 summary, now with a sanitized error and the budget block, and writes
`token_usage.json` with `status: "failed"`:

- `run_id`, `source`, `error` (one line, at most 500 characters, with credential-looking values and the values of
  `*KEY*`/`*SECRET*`/`*TOKEN*`/`*PASSWORD*` variables replaced by `[redacted]`);
- `llm_calls`, `llm_calls_succeeded`, `llm_calls_failed`, `tool_calls`, and `tokens_in`/`tokens_out` *as
  reported* (a failed call reports none; `tokens_note` says so; nothing is estimated);
- `e1_fallback_events` (this run's lines from the E1 fallback log);
- `e2_cache` with hits, misses, skipped and `saved_entries` (the cache's row count before and after the run);
- `run_budget` (limits, calls started, refused, cost charged, reservations still in flight);
- `e3_cost` (the full estimate, or the successful-calls estimate with its basis, or none with a labelled lower
  bound, per `service.e3_cost`), plus `per_agent`, `timing` and `langfuse_flush` from the E3 summary.

Recording never hides the original error: it is attached to the exception (`analysis_failure`, `usage_path`,
`e3_summary_path`) and the exception is re-raised unchanged. Completed runs, cancels and budget stops write
exactly what they did before (tested). Like a completed run's, the file is per ticker and date, so the latest
run replaces it; E3 summaries are per run ID and are never replaced. `scripts/phase2_eval.py` ignores a record
marked `failed`, so Phase 2's tables still count completed runs only.

**Through the worker.** The child sends its failure record with the error, and the parent attaches it to the
exception it raises. When the worker itself fails, the parent writes the record from the calls the child streamed
(completed calls with their reported tokens, calls in flight counted as such, E2 counters `null` because they
lived in the child): after the heartbeat watchdog terminates an unresponsive worker, and now also when the worker
dies, which also gets an E3 summary. **Bug fixed:** a worker that died mid-run was not detected as dead. Its closed
pipe kept reading as "ready", so the parent spun until the 30 s heartbeat limit and reported "no heartbeat …
terminated". It is now reported at once as "exited unexpectedly (exit code N)".

**Dashboard.** A failed run shows "The analysis failed: <error>" (keeping the provider's error type rather than
the worker's `RuntimeError` wrapper), the **run ID**, and what it used before failing: calls succeeded and failed,
tokens reported, E1 switches, E2 hits, misses and saved entries, the E3 cost line, and the budget line.

**Live-test launcher** (`scripts/smoke/live_dashboard.py`, replacing the uncommitted driver used for the run).
It prints its plan and exits 2 without `--approve-live-llm`. It starts `tradingagents-dashboard` with
`STREAMLIT_SERVER_HEADLESS=true` and stdin closed, so nothing can block on a prompt; the interactive
`tradingagents-dashboard` command is unchanged. It refuses to click Run unless every pre-run check passes, clicks
it once, and recognises the four terminal states from the page (completed, failed, cancelled, budget_exceeded).
On any of them it stops at once and saves the message (main-area alerts only), a screenshot, the run ID, the
metrics shown, and the run's E3 summary, `token_usage.json` and fallback log. It clicks Cancel only if the run is
still running at the time limit, and never retries.

**Tests:** `tests/test_failed_run_reporting.py` (12) covers the incident on the real graph:
- the full record, the primary and backup failures, E2 counters and budget accounting surviving a failure;
- credentials removed from the saved error, and a failure before any call reporting zero with nothing invented;
- completed runs unchanged, and cancel and budget stops unchanged;
- a provider failure crossing the worker with its record, and the finished worker exiting;
- a worker dying mid-call, reported at once with a parent-side record;
- the dashboard job and the real page through AppTest.

`tests/test_live_dashboard_script.py` (12) covers headless start-up, the environment handed to the dashboard, the
unchanged interactive launcher, plan-only mode with no process or network call, all four terminal states, never
cancelling a finished run, and detecting a failure at once.

**Real local test, scripted models.** The committed script drove the real dashboard and worker, offline:
- *scripted 503 after two calls:* detected as `failed` in 12-16 s (not 300 s); run ID, message, screenshot and
  "4 LLM call(s) (2 succeeded, 2 failed)", 2 E1 switches, "0 hits, 2 misses, 2 saved", the E3 basis and the
  budget line shown and saved;
- *success:* completed in 11 s, all tabs rendered, `token_usage.json` unchanged;
- in both, no browser errors, no outside hosts, no order, server and workers gone afterwards.

**Remaining limitations** (as of this change; see the follow-up section below for what changed since):
- E2 counters and tool calls were lost when a worker died; they are now kept from worker checkpoints.
- A failed call's tokens are unknown when the provider reports none. This is still so; reported usage is now
  kept.
- The budget estimate is more conservative than E3's; it is now split and reconciled.
- The MCP tool's failure message lacked the run ID; it now has the ID, a category and partial metrics.
- Gemini's internal retries were invisible; they are now bounded for the dashboard and MCP, and counted.

## Reliability and observability follow-up (2026-11-23)

Six findings left open after the live 503 incident, each investigated against the installed libraries
(langchain-google-genai 4.5.0, google-genai 3.3.0, langchain-core 1.8.0, tenacity 9.1.4). Tests:
`tests/test_reliability_observability.py` (20). Changes are uncommitted, pending review.

| # | Finding | Status |
| --- | --- | --- |
| 1 | E2 and tool metrics lost when a worker terminates | Resolved |
| 2 | Token usage of failed LLM calls | Resolved for what providers report; a failed request that reports nothing cannot be measured |
| 3 | Budget accounting vs E3 cost | Resolved |
| 4 | Hidden Gemini SDK retries and timeouts | Resolved for Gemini (bounded and counted); backoff timing is fixed by the SDK |
| 5 | MCP failure reporting | Resolved |
| 6 | Reproducible Playwright setup | Resolved |

**1. Worker checkpoints.** The child now sends a checkpoint after every tool call and with every heartbeat
(each second). A checkpoint holds the run's E2 hits, misses and skipped, the cache's saved entries (a row-count
difference) and tool calls started, finished and failed. The parent keeps the latest one. When a worker dies,
stops responding or is terminated after a cancel, the record carries it as `worker_checkpoint`, with how many
seconds before the failure it was taken. `tool_calls` and `e2_cache` (marked `complete: false`) come from it,
and `metrics_basis` separates the two kinds of count: LLM calls and tokens are confirmed per call, while tool and
E2 counts are the last checkpoint, a lower bound. They are `null` only if the worker sent no checkpoint at all.
A completed run's record is unchanged. One checkpoint costs about 0.7 ms (a SQLite count).

Tested: death mid-call (exit code 3); a frozen worker (SIGSTOP, so no messages: a communication failure) found
by the watchdog; a terminated cancel with a hung call; and a normal completion.

Note: under WSL2 a thread that sends its own process SIGSTOP ran on briefly before stopping (shown with a
standalone probe), so the freeze fake also blocks after stopping.

**2. Usage of failed calls.** On a failed call LangChain passes `response=LLMResult(...)` with whatever was
generated before the error. A *streamed* call that breaks mid-way can carry the provider's `usage_metadata`
there; a non-streamed failure, such as Gemini's 503 rejecting the request, gets an empty result. The Google SDK's
`APIError` carries `code`, `status`, `message` and `details`, but no usage. E3, the budget guard and the
worker's call stream now read that `response`:
- usage that was reported is recorded and priced (`failed_calls_with_usage`);
- usage that was not reported stays `null`, never 0, with `usage_reported: false` per call;
- each failed call records its HTTP `error_code` (from the error or its cause; nothing is parsed from text).

*Technically unavoidable:* the usage of a failed request that reports none cannot be known. It remains
"not reported", and the budget charges its input estimate.

**3. Budget accounting reconciled with E3.** The budget figure is now split by source:
- `reported_usage_cost_usd`: reported tokens at the model's price;
- `unpriced_usage_estimate_usd`: reported tokens of an unpriced model at the highest known price;
- `unreported_usage_estimate_usd`: calls that reported no usage, charged their reservation (successful) or input
  estimate (failed);
- `reserved_in_flight_usd`, `budget_cost_usd` (the three charged parts), and
  `conservative_total_usd` (charged plus in flight).

`reported_usage_cost_usd` is computed exactly as E3's `known_usage_cost_usd`, and tests show the two equal on
real-graph runs with and without E1 fallbacks. Everything else is labelled an estimate. The dashboard line
reads "budget accounting $X of $0.04 (conservative, not a bill: $a from reported tokens + $b estimated for calls
that reported no usage …)". MCP reports the same fields under `run_budget`, never as a cost.

In the scripted 503 run the budget's reported part, $0.000140, equals E3's estimate of $0.000140. The other
$0.000734 is the input estimate of the two failed calls.

**4. Gemini SDK retries and timeouts.** `max_retries` becomes `HttpRetryOptions(attempts=max_retries)`, the
number of attempts **including** the first request; in google-genai 3.3.0 `attempts=0` is treated as 1.
langchain-google-genai's docstring says 0 means "Google's default", which is out of date for this SDK. The SDK:
- retries HTTP 408, 429, 500, 502, 503 and 504, plus transient network errors;
- waits 1 s, doubling, up to 60 s, plus up to 1 s of jitter;
- logs each retry at INFO on `google_genai._api_client`;
- has a per-request timeout (`timeout`, unset by default).

What that meant in the live run: E1's primary already had 1 attempt (immediate failover). The backup used
LangChain's default `max_retries=6`, so 6 attempts with about 31-36 s of backoff. That is exactly the backup's
measured 36.1 s (reproduced offline: 5 waits of 1, 2, 4, 8, 16 s plus jitter, 31-36 s in total).

Controls added (*superseded:* see "Deadlines, provider-aware retries ..." below; interactive runs now use one provider-aware backup retry, a 30 s request timeout, and step and analysis deadlines):
- **Bounded calls for the dashboard and MCP** (`service.BOUNDED_SOURCES`): the backup gets 2 attempts
  (`llm_backup_max_retries=2`, so one retry) and every request a 60 s timeout (`llm_timeout=60`). An explicit
  `TRADINGAGENTS_LLM_BACKUP_MAX_RETRIES` / `TRADINGAGENTS_LLM_TIMEOUT` wins, and the CLI keeps Phase 2's
  settings. The dashboard sidebar states them.
- **Hidden retries observed:** during each run a handler on `google_genai._api_client` counts the SDK's
  "Retrying …" records, reported as `sdk_retries_observed` in E3, the budget accounting and failure records.
  They are not separate LLM calls: no callback sees them, the call limit cannot refuse them, and a retried 429
  or 503 is normally not billed.

Bounds, all tested against the real Gemini client with only the HTTP request replaced:
- a persistent 503 or 429 makes 1 primary attempt and 2 backup attempts and fails over in under 4 s, counting
  1 hidden retry;
- with requests that hang, the worst case per model step was 60 s (primary) + 2 x 60 s (backup) + about 2 s
  of backoff. It is now bounded by the 60 s step deadline (stopped at about 67 s, measured).

The backoff timing itself (1 s, doubling) is the SDK's own: langchain-google-genai exposes only attempts and
timeout. Other providers count `max_retries` as retries, not attempts, and their retries are not observed.

**5. MCP failure reporting.** The tool assigns the run ID before the run starts. A failed or stopped run
returns a standard MCP tool error:
- `analysis failed [<category>] run <id>: <sanitized message>`, then
- a JSON line of `partial metrics` (calls, tokens, tool calls, E1 switches, E2, the E3 cost with its basis, the
  budget accounting, hidden SDK retries), labelled "not complete".

Categories (`service.error_category`): `provider_unavailable`, `rate_limited`, `timeout`, `invalid_request`,
`provider_rejected`, `worker_failure`, `cancelled`, `budget_exceeded`, `busy`, `error`. File paths, stack traces
(server log only) and credentials are never included. When both E1 models fail, LangChain raises the
*primary's* error, so the category is the primary's; the backup's error is in the E1 fallback events. The
dashboard shows the same category. The tool is still read-only, and its result shape is unchanged for successes.

**6. Playwright.**
- An optional `e2e` extra (`playwright==1.52.0`), the only place Playwright appears, so production never
  needs it.
- `scripts/smoke/setup_playwright.sh`: installs that pin into a chosen Python, downloads headless Chromium,
  applies the Ubuntu 26.04 override, and checks Chromium's libraries (prints the `apt` line, or `--user-libs`
  unpacks them without root into the git-ignored `.playwright-libs/`).
- The live script's pre-flight launches the browser and names the fix when it cannot.
- Verified from a fresh virtualenv: setup in 23 s, then pre-flight ready.

**Verification.**
- Real dashboard via the script, scripted models: the 503 case was detected in 7.7 s, with category, run ID and
  the labelled budget line; the success case is unchanged.
- Real MCP over stdio: 46 checks, including the failure case (category, run ID, partial metrics; no paths,
  traces or keys).
- Worker cold start 2.5 s (2.9 s before these changes); warm runs 0.34-0.45 s (0.27-0.47 s before, the same
  range); a finished worker exits at once.

## Deadlines, provider-aware retries, full-analysis limits, and the 41 s objective (2026-11-23)

Uncommitted, pending review. Tests: `tests/test_deadlines_and_limits.py` (19), plus updated tests elsewhere.
Benchmark: `scripts/benchmark_phase3_latency.py` (offline, results below).

### 1. Bounded worst-case time (deadlines)

`tradingagents/run_deadline.py` adds a `RunDeadline` to the request (dashboard and MCP; the CLI has none):

| Setting | Default | Meaning |
| --- | --- | --- |
| `TRADINGAGENTS_ANALYSIS_DEADLINE_SECONDS` | 180 | from the request (worker start-up included) to the result |
| `TRADINGAGENTS_STEP_DEADLINE_SECONDS` | 60 | one model step: primary + E1 backup + SDK retries + backoff |
| `TRADINGAGENTS_DEADLINE_GRACE_SECONDS` | 5 | after a deadline, how long the worker may finish before it is terminated |
| `TRADINGAGENTS_DASHBOARD_TARGET_SECONDS` | 41 | the performance objective: reported as met or missed, never enforced |

Each takes a number or an explicit `off`; anything else is refused.

**Enforcement.**
- *In the worker* (`DeadlineGuard`, first callback): a model call is refused at its start when the analysis
  deadline has passed, or when its step started 60 s ago. Primary and backup calls of one step share a LangChain
  parent run (`with_fallbacks`), so a backup that would start after a slow primary is refused. The deadline is
  also checked between workflow stages.
- *In the parent* (`run_in_worker`), which a blocked SDK call cannot hold up: at the analysis deadline, or when
  a call has been in flight more than its step allows plus 2 s of slack (so the worker's guard acts first), the
  parent cancels and terminates the worker after the 5 s grace.
- *Worker start-up* counts: a deadline that passes before a worker is ready starts nothing.
- *No valid result is discarded:* one that arrives within the grace is still returned. Nothing is retried or
  restarted.

**Outcomes stay distinct.** A deadline stop raises `AnalysisDeadlineExceeded` (category `timeout`, E3 status
`deadline_exceeded`), separate from a cancel by the person, a budget stop and a provider failure. When both a
cancel and a deadline apply, the first wins. The run keeps its partial record:
- `token_usage.json` with status `deadline_exceeded` and a `run_deadline` block;
- E1 events, E2 counts, budget accounting and the E3 summary;
- the worker checkpoint, if the worker had to be terminated.

A completed dashboard or MCP run records `run_deadline.elapsed_seconds` and `target_met`; the dashboard shows
"Finished X s after Run (target 41 s: met)".

**Worst-case bounds (defaults).**
- A model step: about 60 s + 2 s slack + 5 s grace, so **at most ~67 s** before the run is stopped.
- An analysis: **at most ~185 s** (180 s + 5 s grace).

Both were measured in the benchmark below. The trade-off: 180 s fits a four-analyst Shallow run (about 1-2
minutes of model time) with room to spare. Deeper or longer analyses need the setting raised, and a lower value
makes a slow provider stop runs sooner.

### 2. Provider-aware retries and timeouts

`tradingagents/llm_clients/retry_policy.py` records what `max_retries` means for each installed SDK, checked
against its source:

| Provider | `max_retries` counts | Backoff | Retries observable |
| --- | --- | --- | --- |
| Google Gemini (google-genai 3.3) | attempts, including the first (0 is treated as 1) | 1 s doubling to 60 s, plus up to 1 s jitter | INFO "Retrying ..." on `google_genai._api_client` |
| OpenAI, compatible endpoints, Azure (openai 3.51) | additional retries (default 2) | 0.5 s up to 8 s | INFO "Retrying request in ..." on `openai._base_client` |
| Anthropic (anthropic 2.8) | additional retries (default 2) | 0.5 s up to 8 s | INFO "Retrying request to ..." on `anthropic._base_client` |
| Bedrock, others | unknown (integration not installed) | unknown | unavailable |

- *E1 primary:* "no retries" is set per provider (Gemini 1 attempt, the others 0) instead of relying on the
  SDK's handling of 0.
- *Interactive backup:* a new provider-neutral setting, `llm_backup_retries` (retries after the first
  request), is translated per tier provider. Dashboard and MCP runs set it to 1, with a **30 s** per-request
  timeout. 30 s is above the slowest successful call in the saved evidence: 23.1 s; p99 16.4 s; p95 9.3 s over
  381 calls.
- *The CLI is unchanged:* its raw `llm_backup_max_retries` is still passed through, and an explicit setting
  always wins.
- *Retry counts:* retries are counted for all three SDK families (`sdk_retries_observed`). If a run uses a
  provider that does not expose them, the count is `null` with `sdk_retries_observability: "unavailable for
  <provider>"`, never 0.

Fault injection, using the real SDK clients with only their HTTP layer replaced (`httpx2` for OpenAI and
Anthropic, google-genai's request function for Gemini) and all non-local network blocked in the tests:
- persistent **503** and **429** from **Gemini, OpenAI and Anthropic**: each makes 1 primary attempt and 2
  backup attempts, counts 1 observed retry, and stops in **under 4 s** with the right category;
- hung calls are covered by the deadline tests.

### 3. Full-analysis budget

The dashboard's LLM-call limit now follows the chosen analysts and depth: the calls the workflow needs when
every model answers first time, x 1.5 (`run_budget.call_limit_for`).
- Market only, Shallow: 10 expected, so **15**, the validated configuration.
- All four analysts, Shallow: 16 so **24**. Medium: 39. Deep: 54.

`TRADINGAGENTS_DASHBOARD_MAX_LLM_CALLS` still sets a fixed limit when given.

The **cost ceiling stays $0.04** and does not grow with the analysts. A higher ceiling for one run needs an
explicit approval tick in the sidebar, and without it the run does not start (tested on the page). Switching
the limit off takes `TRADINGAGENTS_DASHBOARD_MAX_COST_USD=off`, shown as a warning. The sidebar shows, before
any run, the deadline and target, the cost ceiling, and the call-limit table by analysts and depth; a running
analysis shows its own limits.

Tested:
- a scripted four-analyst run (15 calls) completes under its derived limit of 24;
- the same run with every primary failing (30 calls needed) is stopped as a *budget* stop at 24, not reported
  as a failure.

Trade-off: the 15-call validation limit absorbs up to 5 E1 fallbacks in a market-only run. If every primary
fails, the run needs 20 calls and stops as a budget stop after about 7 steps.

### 4. Performance

- **Warm worker at start-up:** the dashboard starts a spare analysis worker when the page first loads, and the
  MCP server when it starts, so the first run does not pay the ~2.5 s cold start.
- **Faster result display:** the progress view refreshes every 1 s instead of 2 s, so a finished result appears
  up to 1 s sooner.

**Benchmark** (`scripts/benchmark_phase3_latency.py`, offline). The real worker, E1, E2, E3, checkpoints,
budget (15 calls / $0.04) and deadlines run with scripted models that **replay the per-call latencies of saved
real runs**:
- 10-call: the approved Phase 3 smoke run, 24.12 s of model time, 0.26 s of data per tool;
- 12-call: Phase 2 `nvda_live/run_1`, 26.41 s of model time, 3 tool rounds, 4.44 s of live-data delay per tool.

Times run from request creation to the result, so worker start-up is included. "Non-model" is the end-to-end
time minus the replayed model time.

| Scenario | n | median | p95 | worst | outcome | non-model |
| --- | --- | --- | --- | --- | --- | --- |
| 10-call, warm worker, E2 cold | 5 | 24.52 s | 24.57 s | 24.57 s | completed | 0.39 s |
| 10-call, warm worker, E2 warm | 3 | 24.28 s | 24.32 s | 24.32 s | completed | 0.16 s |
| 10-call, cold worker, E2 cold | 3 | 25.83 s | 25.92 s | 25.92 s | completed | 1.70 s |
| 12-call, warm worker, E2 cold (live-data delays) | 5 | 35.51 s | 35.54 s | 35.54 s | completed | 9.10 s |
| 12-call, warm worker, E2 warm | 3 | 26.61 s | 26.61 s | 26.61 s | completed | 0.20 s |
| 10-call, primary 503 on its first 2 calls (2.8 s each, as live) | 3 | 30.17 s | 30.20 s | 30.20 s | completed | |
| 10-call, every primary call 503 (before the E1 circuit breaker; now 27.44 s, completed) | 3 | 40.36 s | 40.37 s | 40.37 s | budget stop at 15 calls | |
| 10-call, primary and backup 503 | 3 | 10.18 s | 10.37 s | 10.37 s | failed (provider_unavailable) | |
| 12-call, cancelled at 5 s | 3 | 5.41 s | 5.45 s | 5.45 s | cancelled (0.4 s after the click) | |
| 12-call, 20 s analysis deadline | 2 | 21.96 s | 23.89 s | 23.89 s | deadline (analysis) | |
| hung primary, default deadlines | 1 | 67.13 s | | 67.13 s | deadline (step): 60 + 2 + 5 s | |
| primary 503 then hung backup, defaults | 1 | 67.08 s | | 67.08 s | deadline (step) | |

**Real dashboard** (headless Chromium via `scripts/smoke/live_dashboard.py`, the 10-call replay): the result was
visible **26.0, 26.0 and 25.9 s after Run**. The page adds about 1.5 s to the worker's 24.5 s (1 s polling plus
rendering).
**MCP over stdio:** the first call answered in 1.0 s with the prewarmed worker (2.2 s before).

**Against the 41 s objective.**
- The normal workflow meets it in every run measured: 10-call 24.3-25.9 s and 12-call 26.6-35.5 s end to end;
  26 s from the dashboard click.
- References: Phase 1 NVDA runs took 34.0-43.3 s, Phase 2's 12-call live runs 36.7-40.6 s (27.3-28.1 s on cached
  data) and its 10-call runs 23.0-26.7 s.
- The replay reproduces those references within their own variation: the 12-call live-data replay took 35.5 s
  against 36.7-40.6 s. Phase 3 adds about 0.2-0.4 s with a warm worker, or about 1.7 s cold.
- Results stay under 41 s with up to about 5 primary failures (2.8 s each). If every primary call fails, the
  15-call limit stops the run at about 40 s.

**This is scripted performance, not provider latency.** The model latencies are real measurements replayed. The
provider's own variation is not reproduced: the slowest real call seen took 23.1 s, and a run with several such
calls would exceed 41 s. Neither are network variation or Gemini incidents such as the 503 of 2026-11-23. Live
data fetching is replayed at Phase 2's measured 4.4 s per tool.

**Measured overheads.**
- Warm-worker hand-off: about 0.2-0.4 s, inside the < 1 s aim.
- Cold worker: +1.3 s, now avoided by prewarming.
- E2 warm: saves about 8.9 s on the live-data profile.
- Checkpoints: about 0.7 ms each.
- Deadline and budget guards: in the noise.


## Performance against the actual Phase 2 baseline (2026-11-23)

All figures below are **offline replay**: no provider, network or API key. Scripted models replay the
per-call latencies saved in `eval/phase2/runs.csv`'s E3 records, and data tools replay their measured delays.
No live measurement was taken for this section.

### Matched comparison (same workload, same replayed latencies, same machine)

`01_NVDA` (E2 cold) and `03_NVDA` (E2 warm) are two of the Phase 2 baseline runs. Each is a 10-call,
single-round Market-only workflow. Every row runs the same replay; only the code differs. Phase 2 is commit
`aa763f7`, "committed" is `6547d48`, and "current" is the uncommitted working tree. Every imported
`tradingagents` module was checked to come from its own tree. A module missing from an older tree would
otherwise be served by the editable install, which invalidated a first attempt.

| Layer | Code | 01_NVDA (n=5) median / max | 03_NVDA, E2 warm (n=5) median / max |
| --- | --- | --- | --- |
| Analysis sequence (as the Phase 2 CLI) | Phase 2 | 21.93 / 21.95 s | 25.92 / 26.00 s |
| Analysis sequence | current | 21.95 / 21.96 s | 25.92 / 27.57 s (one outlier) |
| Shared service, budget + deadline | current | 22.00 / 22.10 s | 25.93 / 26.01 s |
| Worker process (prewarmed) | committed | 22.04 / 22.07 s | 25.91 / 25.97 s |
| Worker process (prewarmed) | current | 21.96 / 22.05 s | 25.94 / 25.96 s |

Replayed model time: 21.52 s and 25.63 s. Framework overhead per run with zero model latency (n=30, medians):
- Phase 2 analysis sequence: 0.14 s (0.13 s without E3).
- Current analysis sequence: 0.16 s.
- Service: 0.15 s.
- Worker: 0.18 s (committed: 0.20 s).

**Reading.** Phase 3 adds no measurable analysis time over Phase 2 on the same workload: the differences are
0.0-0.1 s, within run-to-run noise. The reliability changes (deadlines, checkpoints, provider-aware retries)
cost nothing measurable either: current against committed, 21.96 s vs 22.04 s.

**Against the 29.6 s Phase 2 figure.** 29.6 s is the median *process* wall time of the Phase 2 runs, including
about 4 s of CLI start-up; their analysis time (`cli_elapsed`) was 22.6-26.4 s. The comparable Phase 3 times:

| Measure | Value |
| --- | --- |
| Analysis, in the worker | 22.0 / 25.9 s |
| Worker start-up | 0 s when prewarmed (the dashboard and MCP server prewarm); +1.3-1.7 s cold |
| Dashboard page | about 1.5 s (1 s polling plus rendering); 26.4 s click-to-result on the 10-call smoke replay |
| MCP stdio | first call 1.0 s plus the analysis |

Both targets are met in replay: 29.6 s or less for the comparable workflow, and 41 s or less for normal runs
including the worker.

### The 12-call workflow

Every 12-call NVDA run (the Market analyst making 4 calls over 3 tool rounds) dates from 2026-11-14,
15:29-18:24 UTC. That is before `baaa0f8` ("single-round Market Analyst", 19:43 UTC), which changed the prompt
so the analyst requests its tools in one parallel round. All baseline runs in `eval/phase2/` and all Phase 3
live runs are 10-call. So 12-call is an older Phase 2 configuration, not a Phase 3 regression. No reasoning
step was removed: the analysts, debate, trader, risk and portfolio steps are unchanged.

### Faster fallback: E1 circuit breaker (new, interactive runs only)

Before this change, every model step tried the main model first, even right after it returned 503, so a
persistent outage cost one failed call (2.8 s in the live incident) per step. Now, for dashboard and MCP runs:
1. An overload error (429/500/502/503/504, `UNAVAILABLE`, `RESOURCE_EXHAUSTED`) opens a per-run circuit.
2. While the circuit is open (60 s cooldown), that model's calls go straight to the backup.
3. After the cooldown, the main model is tried again.

Other details:
- Other errors (e.g. 400) do not open the circuit.
- Every trip and skipped call is written to `e1_fallback_log.txt`.
- Each run records `e1_breaker: {trips, skipped_calls}`, also on failed and deadline-stopped runs.
- `TRADINGAGENTS_E1_BREAKER_SECONDS=<n>|off` configures it.
- The CLI is unchanged.

| Scenario (replay, n=5) | No breaker (n=3) | 30 s cooldown | 60 s cooldown (default) |
| --- | --- | --- | --- |
| 10-call, every primary call 503 | 40.36 s, **budget stop** at 15 calls | 27.35 s, completed | **27.35 s, completed** (11 calls) |
| 10-call, primary 503 on its first 2 calls | 30.17 s | 27.35 s | **27.35 s** |
| 12-call, primary 503 on its first 2 calls | 41.37 s | 41.14 s | **38.30 s** |

**Why 60 s.** An interactive run takes about 25-40 s. With a 30 s cooldown, the 12-call run outlasted it, so
the main model was retried and failed a second time. With 60 s, the circuit stays open for the rest of the run:
one failed call per run instead of two, saving 2.8 s. The 10-call runs finish within 30 s, so they are
unchanged. The cost: if the main model recovers quickly, the rest of that run still uses the backup (a cheaper,
weaker model), which the replay does not show. Set `TRADINGAGENTS_E1_BREAKER_SECONDS` to change it per
deployment. The remaining 38.3 s is mostly the old 3-round profile's live-data delays, not the fallback.

### Validation deadline

`scripts/smoke/live_dashboard.py` now runs the dashboard with a **90 s** analysis deadline (`--deadline-seconds`).
That is about twice the slowest healthy replayed run (42.3 s), so no successful run is rejected, while a stuck
run stops in half the production default of 180 s. Verified on the real dashboard in replay: sidebar
"deadline 90 s", completed in 26.4 s, no console errors, no external hosts, all processes cleaned up.

### What helped, and what did not

- **Helped (measured):**
  - the E1 circuit breaker (above);
  - worker prewarming (removes 1.3-1.7 s cold start);
  - E2 warm cache (saves about 8.9 s on the live-data profile).
- **No change needed:** the analysis path, service and worker add 0.0-0.1 s over Phase 2, so there was no
  avoidable overhead left to remove there.
- **Not changed:** UI polling stays at 1 s. Going to 0.5 s would save at most about 0.5 s and double Streamlit
  reruns; the target is already met.
- **Remaining:**
  - the 12-call profile with 2 primary failures is 38.3 s (old configuration; 41.1 s with a 30 s cooldown);
  - live provider variance (slowest real call 23.1 s) is not represented by replay.

## Bug fixes from the 2026-11-24 live dashboard test

The live run (`20261124-084055-53b8fbf5`, Gemini 503 on main and backup) showed two display/logging bugs;
routing was correct.

1. **Switch count.** The dashboard (failure and success views) and the MCP server (`model_fallbacks`,
   partial `e1_fallback_switches`) counted every E1 log line, so one switch showed as 3. `e1_routing.
   summarize_events()` now classifies the four kinds of line (switch after a failure, breaker skip, circuit-open
   notice, backup failure) and counts only calls moved to the backup; the dashboard shows the breakdown
   ("1 (circuit opened 1x; backup failed 1x)") and the MCP answer adds `e1_events`.
2. **Breaker tier label.** The breaker is shared by both tiers but logged the tier that created it ("deep
   tier" for a quick-tier failure). It now logs the tier of the call that failed or was rerouted.

Tests: `tests/test_e1_event_counts.py` (8: the live run's exact lines, every line kind, look-alike error text,
both dashboard views, MCP answer and partial metrics, the shared breaker created by either tier). Two tests
that had asserted the old count were corrected. The saved 2026-11-24 records are accurate and read correctly
with the fix (1 switch); only that run's own "deep tier" label stays as written.

## Phase 2 limitations and common errors (2026-11-24)

Offline implementation of the seven Phase 2 limitations and the documented common errors. Full status,
evidence and what still needs approval: **PHASE3_GAP_REPORT.md**. All new behaviour is off by default or
only changes what happens after a failure, so a healthy run is unchanged (matched benchmark below).

| Setting (`.env`) | Default | Effect |
| --- | --- | --- |
| (none) | on | Quota exhaustion is classified (`quota_exhausted`, `rate_limited`, `unavailable`); a used-up quota sends the rest of the run to the backup, in every source |
| `TRADINGAGENTS_QUICK_FALLBACK_PROVIDER` / `_DEEP_` (+ `_BACKEND_URL`) | none | Backup on a second provider; refused before any call without its key |
| `TRADINGAGENTS_E1_ADAPTIVE` | false | Start a tier on its backup when recent E3 records show its main model failing |
| `TRADINGAGENTS_E2_MAX_AGE_DAYS` | none | Re-fetch saved answers older than this |
| `TRADINGAGENTS_E2_REFRESH` | false | Re-fetch and replace every cacheable answer |
| `cached_input_per_million_usd` in the pricing file | absent | Price provider-reported cached input at that rate (else full price) |

New tools (all offline): `tradingagents doctor` (setup check), `python -m tradingagents.e2_cache info |
invalidate`, `scripts/reconcile_billing.py`, `scripts/backtest_saved_decisions.py`, `scripts/evaluation_matrix.py`.
New error categories with advice in the dashboard and MCP: `quota_exhausted`, `model_not_found`, `auth`,
`configuration`.

**Backtest of saved decisions (offline, real saved prices to 2026-11-13).** 53 successful decisions in the
saved evidence (49 rated), but only 4 (ticker, date) cells: NVDA 2026-01-10 (20 runs), NVDA, AAPL and MSFT
2026-10-23. Hit rates 0.90 / 0.08 / 0.57 / 0.51 at 1 / 5 / 10 / 14 trading days swing with 3-4 market moves,
so they say nothing about decision quality yet. Consistency (separate): repeated runs agree 55-80 % (mean 65 %).

**Tests added:** `test_e1_quota_and_providers.py` (18), `test_e1_adaptive.py` (9), `test_e2_freshness.py` (8),
`test_cost_accounting.py` (7), `test_offline_backtest.py` (13), `test_common_errors.py` (13).

## F1: explicit trade rejection and approval-decision audit (2026-11-24)

Before this change a person declined a proposal only by not approving it, and nothing was recorded; a
validation failure was shown once ("Not sent: ...") and kept nowhere. Now:

- **Reject trade proposal** (beside Submit; needs the sign-in when sign-in is required). It never contacts
  Alpaca. The proposal (`ta-<run_id>-<side>`, the run's client order ID) is marked rejected; the page shows
  "Trade proposal rejected by <user> at <time> UTC. No order was sent." and hides the order form, so nothing
  can be approved or sent. `trade.submit` also refuses a rejected proposal (`proposal_rejected`).
- **Reconsider this proposal** reopens it (recorded). The approval starts from scratch: quantity, stale-date
  tick, approval tick and password are all empty again (the tick's key also counts reconsiderations).
- **Audit log** `<results_dir>/approval_decisions.jsonl` (`tradingagents/execution/decision_log.py`),
  append-only, separate from `paper_orders.jsonl`, whose meaning is unchanged (approved -> submitted | failed,
  only for orders that passed every check). Events: `rejected` (one per rejection: repeated clicks, reruns and
  concurrent requests write one line, under a file lock), `reconsidered`, `submission_failed` with a category:
  `proposal_rejected`, `missing_approval`, `invalid_order`, `password_failed`, `broker_validation`,
  `alpaca_api_error`. Each line: UTC time, run ID, proposal ID, ticker, side, quantity and limit as entered,
  user and sign-in method, reasons with credential-looking values removed. Never a password, token or key.
- Alpaca refusing a checked order now raises `SubmissionFailed`, a subclass of `BrokerError` (existing handlers
  unchanged), so it is recorded as `alpaca_api_error`; the ledger's `failed` line is unchanged.
- Unchanged: approval tick, step-up password, signed and expiring approvals, paper endpoint, fresh buying
  power/position/price checks, the cap and final cap check, duplicate protection, one order per run. Order
  cancellation is still not part of F1. F2 (MCP) does not import the execution layer.

Tests: `tests/test_trade_rejection.py` (8, including the page through Streamlit AppTest: reject, rerun,
reconsider, wrong password, approve, submit).

## F2: per-run LLM limits for MCP analyses, and MCP error labels (2026-11-24)

**Gap.** Until now MCP requests had `budget=None`: no LLM-call or cost limit, only time bounds (900 s tool
limit, 180 s analysis and 60 s step deadlines, 30 s request timeout, one backup retry, the 60 s breaker). The
offline evaluation matrix had given its MCP cells the dashboard's limits, so it overstated what F2 enforced.

**Change.** `run_budget.mcp_budget` gives every MCP run limits from the same `RunBudget` / `BudgetGuard`
architecture as F1: expected calls for the requested analysts and depth x 1.5 (market only 15, all four 24)
and $0.04 estimated cost. Only the server's environment sets them (`TRADINGAGENTS_MCP_MAX_LLM_CALLS`,
`TRADINGAGENTS_MCP_MAX_COST_USD`: a number, or `0`/`off`); the tool's arguments are unchanged, so an MCP client
cannot raise or remove them. A malformed value, or a cost limit without `E3_PRICING_JSON`, is refused before
any run as `invalid server setting: ...` (never "no limit"). A limit stop answers `analysis stopped
[budget_exceeded] run <id>: ...` with the budget's accounting in the partial metrics; a completed answer
carries `run_budget`. Deadlines, the read-only design (no `tradingagents.execution` import, one tool) and the
answer's other fields are unchanged. The CLI still sets no budget.

**MCP error labels (defect fix).** `analyze_stock` reported every `ValueError` as `invalid request: ...`,
including one raised inside the run and relayed by the worker, which then lost its run ID, category and partial
metrics. Now only argument validation reads as `invalid request`; anything later is `analysis failed
[<category>] run <id>: ...`. The worker also keeps `FallbackConfigError` across the process boundary, so a bad
backup-provider setting is reported as `configuration`, not `invalid_request`.

**Tests.** `tests/test_mcp_budget.py` (14): defaults, operator settings, off, malformed values, no pricing,
the tool schema (no limit argument, still read-only), a call-limit and a cost-limit stop through the real graph
and the protocol, a run within its limits. `tests/test_mcp_error_labels.py` (4). `tests/test_dashboard_input_errors.py`
(4): a bad ticker or no analyst refused on the F1 page with no run started. The suite's `conftest.py` switches
the MCP cost limit off (it clears E3 pricing for every test); its call limit stays on, and `test_mcp_budget.py`
tests the cost limit with prices. `test_run_budget.py::test_without_a_budget_nothing_changes` now expects the
MCP call limit.

**Evaluation matrix, MCP cells re-run with the MCP limits** (`results/phase3_eval_matrix_mcp_limits/`, offline
replay at speed 0.1; the original `results/phase3_eval_matrix/` is kept). 96 cells, limits 15 / 23 / 24 / 32
calls (market or all four x shallow or deeper) and $0.04: 72 completed, 24 `failed:provider_unavailable` (the
both-models-503 scenario, as intended), none over a limit. Outcomes and call counts are identical to the original
run's MCP cells in every cell, because `mcp_budget` has the same defaults as `dashboard_budget`. The replay models
report small token counts (budget accounting at most $0.0034), so the matrix exercises the call limit, not the cost
limit; the cost stop is covered by `test_mcp_budget.py`.

## F2: four read-only MCP tools over saved runs (2026-11-25)

The outline lists five MCP tools; only `analyze_stock` existed. Added, all read-only:

| Tool | Input | Output |
| --- | --- | --- |
| `get_report` | `run_id` (required), `section` (one of market, sentiment, news, fundamentals, investment_debate, investment_plan, trader_plan, risk_debate, final_decision; omit for all) | run identity (run_id, source, status, ticker, trade_date, analysts, rating, needs_review, recorded_at), `sections` (each truncated to `TRADINGAGENTS_MCP_MAX_REPORT_CHARS`, 60,000 characters per answer), `not_produced` |
| `get_run_metrics` | `run_id` | identity, `timing` (elapsed, deadline, E3 timing), `e1_routing` (summary, last 50 events, breaker, adaptive), `e2_cache`, `e3_usage` (calls, tokens, retries, models), `per_agent`, `cost` (`kind: estimate`, `actual_billing: null`, billing note), `run_budget`, `errors` |
| `list_recent_runs` | `limit` (1-50, default 10; above 50 is capped and reported) | `runs` (newest first: identity, elapsed, calls, tokens, estimated cost, error category, has_reports), `limit_applied` |
| `propose_trade` | `run_id` | identity, `proposal` (`buy_allowed`, `sell_only_if_long` or `no_trade`, side, reason), `order_rules` (limit/day, quantity typed by a person, max notional, client order ID, what approval needs, stale-date warning), `read_only: true` |

All five tools are annotated `readOnlyHint: true` and `destructiveHint: false`. The four new tools are also
`idempotentHint: true` and `openWorldHint: false`: they read local records only.

**Where the data comes from: `tradingagents/run_store.py` (new).** `token_usage.json` and the state log are
kept per ticker and date, and the next run of the same pair replaces them. So the service now also writes
`<results_dir>/runs/<run_id>.json` (atomic write) when a run ends:
- every interface and every outcome: completed, failed, deadline_exceeded, budget_exceeded, cancelled;
- a run whose worker was terminated is written by the parent process.

A record holds the run's identity, its reports and debates (completed runs only), and the usage already written
to token_usage.json, plus the E1 events and the E3 cost. Recording never raises.

**F1's rules, one definition: `tradingagents/trade_rules.py` (new).** `propose_order`, `is_paper_tradable`,
`client_order_id`, `Side`, `OrderProposal`, `OrderLimits` and the rating sets moved there unchanged from
`execution/order_policy.py`, which re-exports them as the same objects. F1 is unchanged (198 F1 and MCP tests
pass). MCP imports only `trade_rules`, so the pinned boundary still holds: no import of
`tradingagents.execution`, the dashboard or Alpaca from `mcp_server`, `mcp_read_tools`, `run_store`,
`trade_rules` or `service`. Nothing reachable from MCP can approve, sign, submit, cancel or execute an order.
The server cannot see the paper account, so a Sell/Underweight is reported as `sell_only_if_long`, and the
dashboard checks the position.

**Not exposed:** `approval_decisions.jsonl`, `paper_orders.jsonl`, user names, file paths, credentials and
stack traces (tested). `TRADINGAGENTS_MCP_RUN_SOURCES` can restrict the visible runs, e.g. to `mcp`; a hidden
run reads as not found.

**Errors:** `invalid request: …` (malformed run ID, section or limit), `run not found: …`,
`report unavailable: …` (the run failed, or that analyst was not run), `invalid server setting: …`. HTTP
authentication covers all five tools (tested in-process: no token, a wrong token, or the wrong scheme gets 401).

**Tests:** `tests/test_mcp_read_tools.py` (48), plus updates:
- `test_mcp_server.py`: five tools, all read-only; the boundary test now also covers the three new modules;
- `test_mcp_budget.py`: finds `analyze_stock` by name.

The new tests cover:
- a real scripted MCP analysis read back through all four tools;
- records read after the worker processes exit (a completed and a terminated run);
- a budget stop;
- path-like run IDs refused;
- the size caps, and the limit validation and cap;
- every F1 rating and a non-US symbol;
- that the rules are the dashboard's own objects;
- the secret and audit-log exclusion.

**Limitations:**
- runs recorded before this change have no record;
- the tools read one results folder (the server's `results_dir`);
- records are not pruned;
- failed runs keep metrics but not partial reports;
- not run live.

## Live smoke tests

Completed on 2026-11-22: see `PHASE3_SMOKE_TESTS.md` (`scripts/smoke/`) for the commands, safeguards and full
results. Each run was approved on its own, run once, and not retried; outputs are kept in
`results/phase3_smoke/` (git-ignored).

| Run | Result | Key figures |
| --- | --- | --- |
| Gemini + Langfuse, Phase 2 baseline (`20261122_225216`) | 17 PASS, 2 WARN, 0 FAIL | 25.8 s workflow, 10 calls, 27,472 / 5,685 tokens, $0.022454 estimated, 33 Langfuse observations under the run's session; WARNs: `e1.routing` (one model on both tiers) and `phase2.wall_s` (faster than the 12-call references) |
| Alpaca paper, read-only (`alpaca_20261122_225613`) | 11 PASS, 0 WARN, 0 FAIL | paper endpoint, account ACTIVE, 5 read requests, no writes |
| Alpaca paper, one limit order (`alpaca_20261122_230206`) | 18 PASS, 0 WARN, 0 FAIL | 1 F limit buy at $5.58 accepted, duplicate prevented, final status canceled, filled quantity 0 |

An earlier Gemini run (`20261122_215240`) of uncertain provenance is kept but is not one of these validation
runs: its command was not recorded, and its one FAIL (`e1.routing`) was a defect in the check (one model on
both tiers), since corrected. After the Alpaca runs the request meter was changed to label the normal 404s from
`get_open_position` and `get_order_by_client_id` as `EXPECTED_NOT_FOUND` instead of "failed", while still
failing on any other error; the saved reports keep the original labels. Live limitations (one run each; E1
fallback, E2 warm hits and order fills tested offline only) are listed in `PHASE3_SMOKE_TESTS.md`.

## Packaging

`pyproject.toml`: extras `dashboard` (`streamlit`, `alpaca-py`) and `mcp` (`fastmcp>=4.2,<5`); scripts
`tradingagents-dashboard` and `tradingagents-mcp`. The core install is unchanged. Checked with a
`pip install --dry-run` on Python 3.14: streamlit 1.67.0, alpaca-py 0.45.0, fastmcp 4.2.4 resolve.
Streamlit pulls in pyarrow, which is why it stays in an extra.
