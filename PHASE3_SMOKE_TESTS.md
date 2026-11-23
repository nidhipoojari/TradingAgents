# Phase 3 live smoke tests

Two separate live checks. Each needs its own explicit approval flag; without one, a script prints its plan
and an offline pre-flight, makes no network call, and exits 2. Both are excluded from pytest;
`tests/test_smoke_scripts.py` checks offline that they do nothing live without approval and that their
pass/fail checks behave correctly on correct and faulty runs.

**Status: all three approved validation runs completed on 2026-11-22 and passed** (see
[Results](#results-2026-11-22)).

Results go to `results/phase3_smoke/<timestamp>/` (`smoke_report.json` plus the run's own files; the folder is
git-ignored). A run never reuses a folder: an existing name gets a `_2`, `_3`, ... suffix. Every
`smoke_report.json` starts with a `run` block: local and UTC start time, the redacted command, Git branch and
commit, whether the tree had uncommitted changes, finish time and output folder.
Exit code: 0 all PASS (WARNs allowed), 1 any FAIL, 2 not run.

## 1. Gemini + Langfuse (`scripts/smoke/live_gemini_langfuse.py`)

**Approval:** `--approve-live-llm` (one analysis). `--phase2-baseline` runs exactly the Phase 2 configuration
(see the runbook below). `--warm-run` adds a second analysis (doubles the cost) to check the E2 warm path;
`--force-fallback` sets the quick model to a missing name, as in Phase 2's `eval/forced_failure` run, to
check E1 live.

**Pre-flight (offline):** `TRADINGAGENTS_LLM_PROVIDER=google`, `GOOGLE_API_KEY`, `E3_TRACE_ENABLED=1`,
the three `LANGFUSE_*` keys, and `E3_PRICING_JSON` with prices for the models used.

**What it runs:** NVDA on 2026-10-23 (the Phase 2 date), market analyst only, 1 debate and 1 risk round,
through the shared analysis service (`source: api`), E3 and Langfuse on. E2 is on with a fresh cache, or off
with `--phase2-baseline` (as in the Phase 2 `nvda_live` references).

**Expected.** The model is whatever `.env` configures; the Phase 2 baseline is `gemini-3.5-flash-lite` on both
tiers with `gemini-3.1-flash-lite` as the E1 backup (from the E3 per-call records of `eval/phase2`). References:
`eval/tracing_31/01_NVDA` is this exact shape (market analyst only) on `gemini-3.1-flash-lite`: 10 LLM calls,
3 tool calls, 28,354 tokens, $0.0136, 89.9 s. `eval/phase2/01-03_NVDA` are full four-analyst runs on
`gemini-3.5-flash-lite`: 10-11 calls, 31-33k tokens, $0.020-$0.022, 23-27 s.

| Measure | Expected | Fail / warn |
| --- | --- | --- |
| LLM calls | 9-14 (about +10 failed calls with `--force-fallback`) | must equal E3 `observed_llm_calls` |
| Tokens | 15k-45k | E3 == token_usage, both > 0 |
| Cost (`estimated_cost_usd_successful_calls`) | ~$0.01-$0.03 per analysis | FAIL above `--max-cost` (default $0.05) |
| Wall time | 30-180 s (Phase 2 live runs varied 6-17% run to run) | WARN above 300 s |
| Rating | one of Buy/Overweight/Hold/Underweight/Sell | WARN on REVIEW (model output) |

**Checks:** `run.completed`, `run.rating`, `files.token_usage`, `e3.summary`, `e3.attribution`,
`e3.calls_match`, `e3.tokens_match`, `e3.cost`, `e3.timing`, `e1.routing` (Research Manager and Portfolio
Manager on the deep model; a WARN, not a FAIL, when both tiers are configured with the same model, since routing
cannot be seen in model names then; a FAIL if any model outside the configuration answered), `e1.fallback`
(with `--force-fallback`), `e2.cold`, `e2.warm` (with `--warm-run`: 0 misses, 100%), `langfuse.session`
(observations found under `session_id == run_id`, polled up to 120 s), `time.wall`, and with
`--phase2-baseline` the `phase2.*` comparison rows.

**Hard bounds** (enforced during the run, not checked afterwards): a budget callback cancels the analysis as
soon as `--max-calls` (default 30) model calls have started or the priced cost reaches `--max-cost` (default
$0.05), through the shared service's cancellation, so no further model or tool call starts; the check and the
count are atomic, so parallel agents cannot overshoot it. A call already in flight finishes, so the worst case
is the ceiling plus one call (~$0.003). Unpriced models are charged at the highest known price. Langfuse is
polled at most 25 times over 120 s. A hit budget is a FAIL (`run.budget`). The ceiling is an estimate from
`e3_pricing.json`, not a billing cap.

**Credentials:** read from the environment only (`GOOGLE_API_KEY`, `LANGFUSE_*`); never printed or written.
Every printed line and `smoke_report.json` detail is redacted (`scripts/smoke/_safety.py`). The only variable
the script sets is `E3_RESULTS_DIR` (to its output folder). The run uses a fresh data folder under `--out`, so
legacy cache rows play no part.

## 2. Alpaca paper account (`scripts/smoke/live_alpaca_paper.py`)

Two levels, each approved on its own. Paper only: the pre-flight refuses missing paper keys,
`ALPACA_PAPER=false` and any non-paper base URL; the client is always built with `paper=True`, and
`read.endpoint` checks it points at `https://paper-api.alpaca.markets`.

**`--approve-read`** (no orders; 5 read requests to `paper-api.alpaca.markets` and the IEX feed).
Read-only is structural: the trading client is wrapped so every order, position and watchlist method raises,
and the broker's own `submit` is replaced by a refusal (`read.no_writes`):
`read.endpoint` (paper URL), `read.account` (ACTIVE, not blocked), `read.clock`, `read.price` (> 0),
`read.positions` (no short in the symbol), and three local refusals made before any request:
`local.cap` (over `TRADINGAGENTS_MAX_ORDER_NOTIONAL`), `local.market` (market orders are disabled),
`local.ledger` (the refusals wrote nothing). Expected: all PASS. Cost $0.

**`--approve-order --username <dashboard user>`** (the read checks, then ONE order): you are signed in
with your dashboard password (`order.signin`, a server-side session); the order is approved through the full
backend gate (cap, limit-only, signed and session-bound approval) and its shape is checked before you are asked
anything (`order.parameters`: 1 share, BUY, LIMIT at no more than 90% of last, under the cap, session-bound).
The order is shown in full and you must type it back exactly (`order.confirm`; anything else sends nothing).
Then a **limit buy of 1 share of F at half the last price** (about $5.58, expected to rest unfilled) is sent
once. Checks: `order.submitted` (status new/accepted/pending_new, client order ID `ta-<run_id>-buy`),
`order.ledger` (`approved` then `submitted`, with your user name), `order.duplicate` (the same approval again
is answered by the ledger without a request; Alpaca holds one order with that ID), `order.cancel_request`
(the cancel was accepted), `order.final_status` (`canceled`, polled up to 30 s; every terminal status is
recognised), `order.unfilled` (FAIL with "ACTION REQUIRED" on any fill, partial or full).

Nothing is ever retried and no second order is sent: if the submit errors, Alpaca is asked once by client
order ID whether the order exists and, if it does, it is cancelled; a failed cancel is reported with the final
status; an interrupt between submit and cancel still sends the cancel; **a fill is never sold automatically**
(close it in the Alpaca paper dashboard). Outside market hours the order rests until the open and is cancelled
the same way. Each outcome has an offline test.

**Request meter.** Both levels record every request's client, method, duration and outcome (never arguments
or responses) in the `requests` row. Outcomes are `ok`, `EXPECTED_NOT_FOUND`, or `ERROR: <type>`. A 404 is
*expected* only from `get_open_position` (no position held: the broker reads it as 0 shares) and
`get_order_by_client_id` (no earlier order with this client order ID: the duplicate check). A 404 from any
other method, and every other error, is `ERROR` and fails the row. The meter re-raises every exception
unchanged, so trading behaviour does not depend on it.

**Credentials:** paper keys from the environment only; the dashboard password through `getpass` (not echoed,
not stored); the account number is shown masked; every printed and saved detail is redacted.

## Approval

Run nothing here without approval for that specific test: test 1 makes billable Gemini calls and sends
traces to Langfuse; test 2 `--approve-read` reads the paper account; test 2 `--approve-order` places (and
cancels) one paper order.

## Final validation runbook (each step needs its own approval)

Run from this worktree with the team `.env` in place (provider `google`, `GOOGLE_API_KEY`, `E3_TRACE_ENABLED=1`,
the `LANGFUSE_*` keys, `E3_PRICING_JSON=e3_pricing.json`, quick/deep `gemini-3.5-flash-lite`, backups
`gemini-3.1-flash-lite`, Alpaca paper keys, `TRADINGAGENTS_DASHBOARD_USERS`). Every step first runs offline
without its approval flag to show its plan and pre-flight. Steps 1-3 were run on 2026-11-22 (results below).

**Why the counts differ.** The Phase 2 latency references (`consistency_evaluation/nvda`, `nvda_live`) are
market-analyst-only runs at depth 1/1: 12 calls (the market analyst took 3 tool rounds + its report = 4 calls;
then bull, bear, research manager, trader, 3 risk debaters, portfolio manager = 8). `eval/phase2` NVDA runs made
10 calls with the same configuration: the model asked for all its tools in one round (2 market calls). The
15-call offline runs are a different, synthetic workflow (all four analysts, scripted models) used for routing
and accounting tests, not for latency.

| # | Step | Command | Projected cost | Pass criteria |
| --- | --- | --- | --- | --- |
| 0 | Pre-flight (offline) | `python scripts/smoke/live_gemini_langfuse.py --phase2-baseline` and `python scripts/smoke/live_alpaca_paper.py` | $0 | both print "ready" |
| 1 | Real-latency comparison + Gemini/Langfuse smoke, one run | `python scripts/smoke/live_gemini_langfuse.py --approve-live-llm --phase2-baseline --max-cost 0.04` | ~$0.022-$0.025 (the references cost $0.0223 and $0.0249); hard stop at 30 calls or the `--max-cost` estimate plus at most one in-flight call | all E3/E1/Langfuse checks PASS (`e1.routing` WARN with one model on both tiers); `phase2.calls` 10-14; wall, LLM time, tokens, cost reported against the references (36.7-40.6 s wall, 26.4-26.9 s LLM, 32.8k-35.4k input tokens): WARN, not FAIL, outside +/-25% |
| 2 | Alpaca paper, read-only | `python scripts/smoke/live_alpaca_paper.py --approve-read` | $0 (paper; 5 read requests) | all `read.*`, `local.*` and `read.no_writes` PASS |
| 3 | Alpaca paper, one limit order | `python scripts/smoke/live_alpaca_paper.py --approve-order --username <dashboard user>` | $0 (paper; 1 limit buy of 1 F at 50% of last, cancelled; 14 requests in the normal case: 5 reads, the broker's fresh pre-submit checks, 1 submit, 1 duplicate lookup, 1 cancel, status polls) | every `order.*` row PASS |

Step 1 serves both purposes because `--phase2-baseline` runs exactly the Phase 2 configuration (market analyst,
1/1 rounds, the Phase 2 models, E2 off with a fresh data folder like `nvda_live`, default tool rounds and retries)
through the Phase 3 service, with E3 and Langfuse on; the budget guard bounds it without changing the workflow.
The Phase 2 references are only read.

## Results (2026-11-22)

All runs on branch `feature/phase3-f1-f2` at commit `aa763f7` with the Phase 3 changes uncommitted in the
working tree (recorded as `git_uncommitted_changes: true`). Output folders are under `results/phase3_smoke/`
(git-ignored, kept as evidence). No run was retried. A credential scan of every console log and output file
found none of the `.env` secret values, and no unmasked account number.

### Earlier run of uncertain provenance (not one of the validation runs)

`20261122_215240` (run ID `20261122-215242-b456cb94`) was a Gemini + Langfuse run with a $0.04 ceiling that was
not started by any recorded command of this validation; its command line could only be inferred, and it
predates the `run` provenance block (its report has none). It made 10 calls on `gemini-3.5-flash-lite`,
estimated $0.019351, wall 25.2 s, rating Hold, and reported 17 PASS, 1 WARN (`phase2.wall_s`), 1 FAIL
(`e1.routing`). That FAIL was a defect in the check, not in E1: with one model on both tiers the check could
not tell the tiers apart and failed every agent. The check was corrected (WARN when the tiers share a model,
FAIL only for a model outside the configuration), and provenance recording was added to both scripts because
of this run. It is kept as evidence and is not counted below.

### 1. Gemini + Langfuse, Phase 2 baseline: 17 PASS, 2 WARN, 0 FAIL

`20261122_225216`, run ID `20261122-225220-bf036521`, started 2026-11-22 22:52:20 -05:00, command
`python scripts/smoke/live_gemini_langfuse.py --approve-live-llm --phase2-baseline --max-cost 0.04`.

| Measure | Result |
| --- | --- |
| Workflow wall time | 25.8 s (E3 phases: pre 0.0 s, analysis 25.805 s, post 0.001 s) |
| LLM time / non-LLM time | 24.1 s / 1.7 s (0.87 s before the first call, 0.80 s between calls; 3 tool calls) |
| LLM calls | 10, all `gemini-3.5-flash-lite`, 0 errors; E3 == `token_usage.json` |
| Tokens | 27,472 in / 5,685 out |
| Estimated cost | $0.022454 (10 of 10 calls priced; strict and successful-call estimates equal), under the $0.04 ceiling |
| E1 | no fallback switches (the backup was not needed); `e1.routing` WARN: same model on both tiers |
| E2 | off, as in the Phase 2 baseline; data fetched live into a fresh folder |
| E3 | status success, 9 agents attributed, Langfuse flush `ok` |
| Langfuse | 33 observations under session ID = run ID (LLM, chain and tool spans) |
| Rating | Overweight |

`phase2.wall_s` WARN: 25.8 s against the 12-call references' 36.7-40.6 s. The run is in line with Phase 2's
own 10-call runs (`eval/phase2` 01-03: 23.0-26.7 s, 26.3k-27.4k input tokens). The differences from the 12-call
references come from the model batching its tool calls into one round (2 market-analyst calls instead of 4)
and from faster data retrieval (1.7 s against 9.8-14.2 s); they are not attributed to Phase 3. The offline
deterministic replay is the regression evidence: Phase 2 and Phase 3 code are identical on identical inputs.
The rating differs from the uncertain run's Hold; ratings vary run to run and one run cannot measure that.

### 2. Alpaca paper, read-only: 11 PASS, 0 WARN, 0 FAIL

`alpaca_20261122_225613`, started 2026-11-22 22:56:13 -05:00, command
`python scripts/smoke/live_alpaca_paper.py --approve-read`. Paper endpoint confirmed; account ACTIVE, buying power
$200,000.00; market closed (next open 2026-11-23 09:30 -05:00); F last $11.17; 0 positions. The over-cap
(90 shares) and market-order requests were refused locally with no ledger written. 5 requests, 570 ms in all,
none an order or position write. The `get_open_position` request returned the 404 Alpaca sends when no position
exists, which the broker reads as 0 shares held; that run's meter recorded it as "failed" (see below).

### 3. Alpaca paper, one limit order: 18 PASS, 0 WARN, 0 FAIL

`alpaca_20261122_230206`, started 2026-11-22 23:02:06 -05:00, command
`python scripts/smoke/live_alpaca_paper.py --approve-order --username sbhat`, run from the operator's own
terminal (it needs the dashboard password and the typed confirmation). Signed in as `sbhat` (session-bound);
BUY 1 F LIMIT $5.58, DAY, value $5.58 against the $1,000 cap; confirmation typed; **accepted** as order
`9c558cdb-c9ac-4144-a5a4-5c8694c09e2e` (client order ID `ta-20261122-230215-4c76f2b0-buy`); ledger `approved`
then `submitted`; the repeated submit was answered as a **duplicate** with the same order and no second order;
cancel sent; **final status `canceled`; `filled_qty` 0**, so no position was opened. 14 requests, 632.5 ms.

### `EXPECTED_NOT_FOUND` and the reporting improvement

Runs 2 and 3 used the first version of the request meter, which recorded only ok or failed. It labelled three
normal answers as "(failed)": in run 3, `get_open_position` twice (the read check and the broker's fresh
pre-submit check: no position held) and `get_order_by_client_id` once (no earlier order with this ID); in run 2
the one `get_open_position`. Every check passed because the broker handles these 404s as designed. After the
runs, the meter was changed to label them `EXPECTED_NOT_FOUND`, to keep every other error as `ERROR: <type>`,
and to fail the `requests` row on any unexpected error (offline tests cover both cases). The saved reports were
not rewritten, so runs 2 and 3 still show "(failed)" for these requests.

### Limitations of the live validation

- One live run per test: no run-to-run variance is measured, and the rating is model output.
- E1 fallback and E2 warm hits were not exercised live (no switch occurred; E2 was off in the baseline);
  `--force-fallback` and `--warm-run` were not run. Both are covered offline.
- Langfuse was checked for ingestion and session grouping; the `source:` tag was not checked through its API.
- The order was placed with the market closed, so it rested and could not fill; fill, partial-fill,
  failed-cancel and ambiguous-submit handling are verified offline only.
- The dashboard and MCP server were not driven live; they use the same shared service and broker, tested
  offline (Streamlit `AppTest`, an in-memory MCP client).
