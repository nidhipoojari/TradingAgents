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

**What it does.** `tradingagents-mcp` serves one MCP tool, `analyze_stock(ticker, trade_date?, analysts?,
include_reports?)`. It returns the rating, whether it needs review, the decision, the research plan and the
trader plan (each truncated to `TRADINGAGENTS_MCP_MAX_REPORT_CHARS`, default 4000), usage, E1 switch count
and E3 cost. It runs through the shared service with `source="mcp"`.

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
