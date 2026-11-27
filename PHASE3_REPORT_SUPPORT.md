# Phase 3 report support: Sections 4.1-4.3, and the Phase 2 benchmark reconciliation

Draft material for the Phase 3 report (2026-11-25). It does not replace `eval/report_tables.html`, which is
unchanged. Each statement is marked:

- **[verified]**: measured or tested, with the evidence named;
- **[pending live]**: needs a live run that has not been done (needs approval);
- **[unresolved]**: a known gap or an open question.

"Offline replay" means scripted models replaying the per-call latencies of saved real runs: no provider, no
network, no key. "Scripted integration" means the real code paths (worker, service, Streamlit page, MCP
protocol) with scripted models and a fake Alpaca. "Live" means real Gemini, Langfuse or Alpaca paper calls.

---

## 1. Phase 2 benchmark reconciliation: 29.6 s vs 21.94 s

**The original 29.6 s baseline stands unchanged** (`eval/phase2/runs.csv`, `results/phase2/benchmarks.csv`).
The 21.94 s figure is a separate, controlled replay. The two are **not directly comparable**.

| | 29.6 s (Phase 2 baseline) | 21.94 s (controlled replay) |
| --- | --- | --- |
| Kind | **live** | **offline replay** |
| Source | `eval/phase2/runs.csv` (`scripts/phase2_eval.py`) | `results/phase3_benchmarks_raw_20261122-24/replay_ab/final_ab.jsonl` |
| Code | `adffdd1` plus uncommitted changes (`eval/phase2/manifest.json`), not `aa763f7` | `aa763f7` (Phase 2 merge) vs current tree |
| Model | Gemini 3.5 Flash Lite, 2026-11-14 | scripted; replays run `01_NVDA`'s per-call latencies (21.52 s total) |
| Runs | NVDA x3: 26.825 / 29.635 / 30.201 s; median 29.6 s | 5 replays of one run; median 21.94 s |
| Timing boundary | process start to exit, including about 4 s of start-up (wall minus analysis: 4.2 / 4.0 / 3.8 s) | graph construction to decision saved, in-process |
| Model time | live and variable: 21.5 / 24.8 / 25.6 s | fixed at 21.52 s |
| E2 cache | cold, then 4 of 5 hits, then 5 of 5 | cold; data time simulated at 0.195 s per tool |
| E1 backup | run 2 used it once (11 calls) | none (10 calls) |

- **[verified] The like-for-like live figure** is the analysis time (`cli_elapsed`): 22.6 / 25.6 / 26.4 s.
  Run `01_NVDA` took 22.6 s live against 21.9 s replayed; `03_NVDA` 26.4 s live against 25.9 s replayed. The
  gap is live data fetching and network time.
- **[verified] Within the replay**, Phase 2 and Phase 3 code are directly comparable (same inputs, same
  machine): the differences are 0.0-0.1 s (PHASE3_CHANGES.md, "Performance against the actual Phase 2
  baseline").
- **The 28.7 s figure** in the report's 4.2 table is a different set: the median of 5 runs, NVDA x3 plus the
  AAPL and MSFT reruns.
- **[pending live]** No live Phase 3 run through F1 or F2 has completed, so no live Phase 3 figure can be put
  beside 29.6 s yet. The 41 s target is **met in replay only**.

---

## 2. Section 4.1: how F1 and F2 use E1, E2, E3 and the shared service

**[verified] Shared path.** Both interfaces build `AnalysisRequest(source="dashboard" | "mcp")`.
`worker.run_in_worker` then runs `service.run_analysis` in a separate worker process. Both get the interactive
settings from `config_for_source`: 30 s request timeout, 1 backup retry and a 60 s E1 breaker; MCP also has its
own memory log. Each run also gets an analysis deadline (180 s by default), a model-step deadline (60 s) and
per-run limits (calls and estimated cost: 15 / $0.04 for market-only, 24 / $0.04 for all four analysts).
Tests: `tests/test_phase3_integration.py`, `tests/test_analysis_service.py`, `tests/test_worker.py`.

**[verified] Inside each run:**
- **E1:** `e1_routing` builds each tier's main model with its backup and the circuit breaker.
- **E2:** the cache folder is shared, with counters kept per run.
- **E3:** one session per run, which writes the E3 summary. For Langfuse it sets
  `langfuse_session_id = run_id`, the tag `source:<interface>`, and metadata `tradingagents_run_id` and
  `tradingagents_source` (`tradingagents/service.py:668-674`).

**Metrics by interface:**

| | F1 page | F2 `analyze_stock` answer | Langfuse |
| --- | --- | --- | --- |
| Rating, decision, run ID | yes | yes, plus `needs_review` and the plans | trace output |
| Tokens in / out, tool calls | yes | yes, plus total tokens, elapsed time and LLM calls | per call |
| E2 hit rate and counters | yes | `usage.e2_cache` | no |
| E1 switches and events | yes | `model_fallbacks`, `e1_events` | backup calls show as their own generations |
| Estimated cost and its basis | yes | `estimated_cost_usd`, `cost_basis`, `cost_note` | Langfuse's own pricing, not E3's |
| Per-agent table | yes | no | by span |
| Budget accounting | yes | `run_budget` | no |
| Time against the 41 s target | yes | no | trace duration |
| Saved-run retrieval (reports, per-agent table, timing, E1 event lines) | run history on disk | via `get_report` / `get_run_metrics` (no model cost) | traces |

- **[verified] Five MCP tools are registered**: `analyze_stock`, `get_report`, `get_run_metrics`,
  `list_recent_runs` and `propose_trade`, all annotated read-only (test
  `test_the_server_lists_five_read_only_tools`). `get_run_metrics` returns the recorded E1, E2, E3, cost,
  budget, error and timing data of a saved run (section 6). The four read tools were added on 2026-11-25;
  before that, only `analyze_stock` existed.
- **[verified live, 2026-11-22]** Langfuse ingestion and session grouping, from direct service runs (source
  `api`).
- **[pending live]** The `source:` tag has not been checked through Langfuse's API, and no completed dashboard or
  MCP trace exists.

---

## 3. Section 4.2: regression testing

| Type | What | n | Latency | Tokens | Est. cost | Success |
| --- | --- | --- | --- | --- | --- | --- |
| Offline | full pytest (2026-11-25, final: after the MCP tools, the clock fixes and the test fixes) | 2,044 | — | — | — | **[verified]** 2,043 passed, 1 skipped (Bedrock SDK not installed), 0 failed; log `results/phase3_verification_20261125/final_20261125/full_pytest_after_plugins.log` (earlier: 2,008 after the MCP tools, `full_pytest.log`) |
| Scripted integration | 17 files: F1 page, MCP protocol (all five tools, HTTP auth), worker, Alpaca fake, deadlines, budget | 356 | — | — | — | **[verified]** 356/356 (2026-11-25); log `integration_17_files.log`; the earlier intermittent failure is explained in section 5 |
| Offline replay | Phase 2 vs current, `01_NVDA` | 5 each | 21.94 vs 21.95 (graph) / 22.00 (service) / 21.96 s (worker) | not measured | not measured | 5/5 each |
| Offline replay | framework overhead, zero model latency | 30 each | 0.13 vs 0.15 / 0.16 / 0.21 s | — | — | 30/30 |
| Offline replay | normal and failure scenarios | 3-5 each | 10-call 24.67; 12-call 35.71; 2 primary failures 38.49; every primary 503 27.53 s | not measured | not measured | all completed |
| Offline matrix | 192 cells, plus the 96-cell MCP re-run | 288 | scaled x0.1, not timing | not measured | not measured | 144/192 and 72/96 completed; the rest failed as designed (both models 503) |
| Live (Phase 2 baseline) | CLI, NVDA x3, 2026-11-14 | 3 | median 29.6 s (process) | 31,933 mean | $0.0210 mean | 3/3 |
| Live (Phase 3) | service smoke, source `api`, not through F1 or F2 | 2 | analysis 24.2 / 25.3 s | 29,912 / 33,157 | $0.0194 / $0.0225 (estimated) | 2/2 |
| Live (Phase 3) | F1 dashboard, 11-23 and 11-24 | 2 | not measured (failed after 47.9 s and 8.4 s) | partial | not available | 0/2 (Gemini 503 on main and backup) |
| Live (Phase 3) | F2 MCP | 0 | not measured | not measured | not measured | **[pending live]** not run |
| Live (Phase 3) | Alpaca paper: read-only, then one limit order | 2 | 570 / 633 ms | — | $0 | 11/11 and 18/18 PASS; order cancelled, 0 filled |

Costs are E3 **estimates** (reported tokens x `E3_PRICING_JSON`), not provider billing.
`scripts/reconcile_billing.py` compares them with a billing export when one is available. **[unresolved]**
No billing export has been reconciled yet.

---

## 4. Section 4.3: error scenarios

**[unresolved]** The team outline's own list of 12 scenarios was not found in the repository or the session.
The 12 below were derived from the proposal's concerns; remap them if the outline differs.

| # | Scenario | Implemented behaviour | Offline tests [verified] | Untested live or external |
| --- | --- | --- | --- | --- |
| 1 | Invalid ticker, date or analysts | F2 returns "invalid request"; F1 shows an error and starts no run; the date picker stops at today | `test_mcp_server`, `test_common_errors`, `test_dashboard_input_errors` (4) | — |
| 2 | Missing or invalid LLM key | `doctor` names the missing key; a 401 becomes `auth` with advice | `test_common_errors`, `test_e1_quota_and_providers` | Gemini's real reply to a bad key |
| 3 | 429 rate limit or used-up quota | classified; a used-up quota sends the rest of the run to the backup | `test_e1_quota_and_providers` (12), `test_reliability_observability` | a real quota exhaustion |
| 4 | Main model 503 | E1 backup plus the 60 s breaker | `test_e1_breaker` (6), `test_failed_run_reporting` | a successful live fallback |
| 5 | Both models fail | stops as `provider_unavailable`, keeping its record and partial metrics | `test_failed_run_reporting` (12), the matrix | seen live 2026-11-24, behaved as designed |
| 6 | Hung or slow call | 60 s step and 180 s run deadlines; the parent ends the worker | `test_deadlines_and_limits` (22, incl. the new slow-start regression test), `test_worker` | — |
| 7 | Call or cost limit exceeded | budget stop in F1 and F2 | `test_run_budget` (24), `test_mcp_budget` (14) | — |
| 8 | A second request while one runs | refused at once | `test_mcp_server`, `test_dashboard_logic` | — |
| 9 | Market data fails | a "no data" marker and the next vendor; the run continues | `test_no_data_handling`, `test_sec_edgar` | a real Yahoo outage |
| 10 | Langfuse or E3 problem | clear message for a missing package or keys; time-bounded flush | `test_common_errors`, `test_worker` | a Langfuse outage |
| 11 | MCP authentication, exposure, timeout, cancel; read-tool misuse | bearer token for all five tools, loopback only by default, TLS for remote; 900 s limit; cancel. Read tools: path-like run IDs refused, missing run / missing report errors, limit capped at 50, no audit logs or secrets returned | `test_mcp_server`, `test_mcp_read_tools` (48), `test_worker`, `test_phase3_timing_and_cancel` | a third-party MCP client |
| 12 | F1 order failures | no approval, invalid order, wrong password, stale analysis, broker checks, Alpaca refusal, rejection; each logged | `test_alpaca_paper` (31), `test_trade_rejection` (8), `test_dashboard_logic` | a live Alpaca refusal |

---

## 5. The intermittent worker step-deadline test (resolved 2026-11-25)

`test_a_hung_call_is_ended_by_the_parent_at_the_step_deadline` failed once (2026-11-24, 1 of 267) and passed
on reruns.

- **[verified] Reproduced** offline under heavy CPU load (42 busy processes on 14 cores): 3/3 failures, all on
  `took < 11 s` (took 15.3 / 16.4 / 22.5 s).
- **[verified] Root cause: the test's timing assumption, not the deadline code.**
  - The bound measured from the request, so it included the cold worker start-up: 1.2-1.5 s idle, 2.2 s in the
    combined test order, 7.7-14.8 s under load.
  - Worker start-up is not part of a model step.
  - In every run, loaded or idle, the parent ended the hung call 4.0 s after the call began (2 s step + 2 s
    slack), within the 1 s grace.
- **Ruled out by measurement:**
  - children of earlier tests lingering: they exited within 1.0 s, across 16 runs;
  - slow DNS for the fake Langfuse host: 0.09 s;
  - the child reporting itself first: it has no such path while the call hangs.
- **Fix (test code only; `tradingagents/` unchanged):**
  - The test now times from the hung call's start, which is the clock the step deadline uses.
  - It checks the stop is never earlier than the allowance, and at most 3 s later.
  - It also checks that the grace was respected.
- **Regression test:** `test_a_slow_worker_start_does_not_use_up_the_step_deadline` (a 4 s worker start-up,
  then a hung call). A deliberately broken step clock that starts at the request makes it fail (stopped
  1.06 s after the call began).
- **[verified] After the fix:**
  - 3/3 passes idle and 3/3 under the same load;
  - 267/267 integration, and later 356/356 with the MCP tools;
  - the full suite passed.

  Evidence is in `results/phase3_verification_20261125/step_deadline_flake/`: the phase timings before the
  fix, and the probe and mutant plugins.
- **This is a verified fix, not a pass on rerun.** The failure was reproduced on demand, its cause was measured,
  the test was changed to the correct clock, and the result was re-checked under the reproducing load. The new
  regression test was shown to fail when the defect it guards against is put in.
- **A separate one-off (2026-11-25, investigated): 4 of 5 failures explained and fixed; 1 unidentified.**
  - **What failed:** a full run reported 5 failures, of which only two names were captured
    (`test_trade_rejection.py`: `test_each_kind_of_failure_is_recorded_with_its_category`,
    `test_an_alpaca_refusal_is_recorded_in_both_logs_without_secrets`).
  - **[verified] Cause:**
    - the system wall clock stepped 185.7 s forward during the run. `systemd-resolved` logged "Clock change
      detected" at 09:20:34 on the stepped clock, which is 09:17:29 on the unstepped clock: 19.9 s after the
      failing command began, so about 12-16 s into the tests, after collection. (An earlier note placed the step
      "at about the point where test_trade_rejection runs"; that was wrong. The file runs at about 67% of the
      suite, much later.) The tool's own timestamps agree: 421.7 s of wall time against about 230 s of run time;
    - five test files shared a signed-in identity issued at collection;
    - its password confirmation is valid for 5 minutes of wall-clock time, so it had expired.
  - **[verified] Reproduced:** both with an emulated +396 s jump and with a real 310 s wait (no patching), with
    the same result each time: tests 3-6 of that file fail with "Re-enter your password"; tests 1, 2, 7 and 8
    pass. CPU load alone does not cause it (8/8 x 3).
  - **Fix (tests only):**
    - a fresh identity per test in the five files;
    - a default argument bound at import, found by the same reproduction, was also fixed.
  - **[verified] After the fix:** 153/153 under the emulated jump and under the real wait.
  - **The product is unchanged:** a clock step makes it refuse orders, never accept them.
  - **[unresolved] The fifth failure is still unidentified: an unresolved historical failure.** Its name was never
    captured, and it ran before these tests. What is now known (`results/phase3_verification_20261125/fifth_failure/`):
    - **Timing:** a per-test timeline of the suite puts the step's moment (about 12-16 s in) among
      `test_dashboard_input_errors`, `test_dashboard_logic` and the first tests of `test_deadlines_and_limits`
      (`window_analysis.txt`).
    - **Before today's clock fix, the deadlines were wall-clock**, and a +185.7 s step during a deadline test fails
      it. On a copy of the tree with the pre-fix deadline code, a step at each test's start failed 8 of 78 tests in
      that window, all in `test_deadlines_and_limits`; a step 0.3 s into each test failed 6 of 22. The dashboard,
      date and cost tests all passed. A deadline test spanning the step is therefore the most likely candidate, but
      this is not proven: the name was never recorded.
    - **`test_sessions.py` and `test_social_lookahead.py` are not supported as the cause.** Under a +185.7 s step at
      the start of, or 0.05 s into, each test, both pass (31/31 each time). Under a −185.7 s step 0.05 s in, one
      session test sees a different refusal reason, "the clock changed" instead of "restarted". It is still
      refused. A backward step did not happen in that run, so it cannot be the historical failure.
  - **Found on the way: a pre-existing race in two deadline tests (fixed in tests only).**
    - **Where:** `test_a_cancel_before_the_deadline_stays_a_cancel` and
      `test_the_deadline_reached_while_calls_run_stops_the_worker_cooperatively`.
    - **Cause:** the suite clears `TRADINGAGENTS_*`, so its provider is the built-in default, `openai`. A fresh
      worker then imports `openai_client` in `describe_for` before its first cancel check, which took 0.33-0.98 s
      in this session. The tests assumed the run started within 1 s (or 2 s).
    - **Evidence it predates today's changes:** the same failures occur on the pre-fix code (4 of 9 file runs);
      the per-worker timings are in `child_probe_*.jsonl`.
    - **Fix:** the cancel waits for the hung call to start, the deadline is 3.5 s, and these two tests use the
      `google` provider, as the live configuration does. A precondition message now names a too-slow machine.
    - **After the fix:** 8 of 8 file runs passed (`deadline_tests_final/`), and 4 of 4 under ±185.7 s steps.
    - **Production impact:** none on the Gemini path; that import only happens for OpenAI-compatible providers.
  - **Evidence:** `results/phase3_verification_20261125/trade_rejection_flake/` and `.../fifth_failure/`.
---

## 6. F2: the five MCP tools (Sections 3.2 and 7.3(b))

**[verified offline]** Registered schemas (from `list_tools`). Every output schema is a JSON object; the
fields are listed in PHASE3_CHANGES.md, "F2: four read-only MCP tools over saved runs".

| Tool | Input schema | readOnly / destructive / idempotent / openWorld |
| --- | --- | --- |
| `analyze_stock` | `ticker` string (required); `trade_date` string or null; `analysts` string array or null; `include_reports` boolean (false) | true / false / false / true |
| `get_report` | `run_id` string (required); `section` string or null | true / false / true / false |
| `get_run_metrics` | `run_id` string (required) | true / false / true / false |
| `list_recent_runs` | `limit` integer, strict (default 10; `minimum: 1`; above 50 capped at 50) | true / false / true / false |
| `propose_trade` | `run_id` string (required) | true / false / true / false |

**Updated F2 workflow:**
1. An assistant calls `analyze_stock`. The request is validated and given the MCP limits (15/24 calls, $0.04)
   and the deadlines.
2. The run goes through the worker and the shared service, with E1, E2 and E3 inside it.
3. When the run ends, its record is saved (`<results_dir>/runs/<run_id>.json`), whatever the outcome. The
   answer, or the error, carries the run ID.
4. Later, at no model cost, the assistant can:
   - call `list_recent_runs` to find runs;
   - call `get_report` for the reports and debates;
   - call `get_run_metrics` for the E1 events, E2 counts, E3 tokens, estimated cost (labelled an estimate,
     with no billing figure), limits, errors and timing.
5. `propose_trade` says what F1's rules allow: `buy_allowed`, `sell_only_if_long` or `no_trade`, with the
   order rules.
6. Only a signed-in person in the F1 dashboard can approve and send a paper order. No MCP tool can approve,
   sign, submit, cancel or execute one, and no MCP module imports the execution layer (tested).

**`list_recent_runs` limit (fixed 2026-11-25).**
- **The problem:** FastMCP's lax validation turned `"10"` into 10, `2.0` into 2 and `true` into 1 before the
  tool saw them.
- **The fix:** the parameter is now strict (`Annotated[int, Field(strict=True)]`, schema `minimum: 1`).
- **[verified] Over the protocol:**

| Input | Result |
| --- | --- |
| omitted | 10 |
| 50, 51, 500, 10^6 | capped at 50 (`limit_requested` echoes the request) |
| 0, -1, -50 | `invalid request: limit must be a whole number from 1 to 50` |
| `"10"`, `"ten"`, true, false, 2.0, 1.5, null, `[5]`, an object | refused as not a valid integer; never converted |
| no runs (no `runs/` folder, or an empty one) | `runs: []`, not an error |

Tests: 19 new in `tests/test_mcp_read_tools.py`. All MCP tests: 133 passed. All five tools are still read-only
(`results/phase3_verification_20261125/mcp_limit_validation/`).

**[unresolved / limitations]**
- runs from before 2026-11-25 have no record;
- the tools read only the server's own results folder;
- records are never pruned;
- failed runs keep metrics, not partial reports;
- by default every source is visible; `TRADINGAGENTS_MCP_RUN_SOURCES` can narrow that.

**[pending live]** None of the five tools has been used with a live model or a third-party MCP client.

### Phase 3 test inventory (tests collected per file, 2026-11-25)

| Area | Files (tests) |
| --- | --- |
| F1 dashboard and trading | `test_dashboard_logic` (15), `test_dashboard_input_errors` (4), `test_trade_rejection` (8), `test_alpaca_paper` (49), `test_order_policy` (37), `test_limit_orders_and_identity` (44), `test_sessions` (16), `test_live_dashboard_script` (12) |
| F2 MCP | `test_mcp_server` (48), `test_mcp_budget` (14), `test_mcp_error_labels` (4), `test_mcp_read_tools` (67) |
| Shared service, worker, limits | `test_analysis_service` (20), `test_worker` (20), `test_run_budget` (24), `test_deadlines_and_limits` (22), `test_failed_run_reporting` (12), `test_reliability_observability` (20) |
| Integration and validation | `test_phase3_integration` (3), `test_phase3_validation` (15), `test_phase3_timing_and_cancel` (13), `test_smoke_scripts` (42) |
| E1 | `test_e1_breaker` (6), `test_e1_adaptive` (9), `test_e1_event_counts` (8), `test_e1_quota_and_providers` (18), `test_e1_routing_benchmark` (7) |
| E2 | `test_e2_freshness` (8), `test_e2_vendor_key` (18) |
| E3 and cost | `test_e3_cost_reporting` (13), `test_cost_accounting` (7), `test_e1_e2_e3_audit` (8) |
| Common errors and evaluation | `test_common_errors` (13), `test_offline_backtest` (13) |
| Clock steps | `test_clock_jumps` (16); opt-in investigation plugins in `tests/plugins/` (loaded with `-p tests.plugins.<name>`) |

---

## 7. Production clock audit (2026-11-25)

Prompted by the measured 185.7 s WSL2 step. Each area, the clock it uses, and the result:

| Area | Clock before | Finding | Now |
| --- | --- | --- | --- |
| Worker deadlines (analysis, step), cancel and grace timers | **wall** for deadlines; monotonic for grace, heartbeat and cancel release | **Defect.** A forward step ended runs early: a +185.7 s step tripped a 180 s deadline at once. A backward step disabled the deadlines: a hung run was ended only by the test's watchdog. The time against the 41 s target included the step. | Monotonic. The parent sends elapsed time, the worker rebases it, and the parent times steps on its own clock. `started_at` stays wall-clock, for records. |
| E1 breaker cooldown, retry delays | monotonic; delays are relative seconds | correct | unchanged |
| Sign-in lifetime (8 h), idle (30 min), password confirmation (5 min), server-side sessions | **wall only** | **Defect.** A backward step extended every window by the step's size. A forward step expired them early, which is safe. | Both clocks; the stricter one wins, so no step can extend a window. Wall time is kept, because a suspended laptop's sleep must count. A sign-in time in the future is refused. |
| Signed paper-order approval (5 min) | **wall only** | **Defect, the serious one.** An approval 5 min 10 s old reached `broker.submit` after a backward step. | A signed monotonic approval time; the older age wins. The same case is now refused and nothing is sent. |
| Persistent sign-in limits (15 min window, across restarts) | wall | must survive restarts; a large forward step can shorten the window; the in-process 60 s lockout is monotonic | unchanged (documented) |
| E2 cache age | wall (`saved_at`, persistent) | a step moves day-scale expiry by its size | unchanged (documented) |
| E3, audit and run-record timestamps | wall for timestamps; durations used `perf_counter` / `time.time` | durations in records (`elapsed_seconds`) included steps | durations are monotonic; timestamps stay wall-clock |

- **[verified] Tests:** `tests/test_clock_jumps.py` (16).
  - 11 of its first 14 tests failed on the old code (`clock_audit/clock_jumps_before_fix.log`).
  - After the fix: 14/14 (`clock_jumps_after_fix.log`); later 16/16, adding the dashboard-click case and a check
    on the investigation plugins.
- **No security control was weakened:** windows can now only get shorter, never longer.
- **One test was changed because it bypasses `approve()`:** the property test of the order cap now supplies the
  approval's monotonic time.
- **Found by simulation:** the deadline's monotonic start was derived from a wall-clock age. It is now read
  directly; only callers that give a past `started_at` get an age-based start, and the dashboard passes its
  click's monotonic time.

---

## 8. Readiness for one controlled live F1 analysis

**Recommendation: ready, when Gemini is healthy.** The offline evidence is complete.

**What changed on the F1 path on 2026-11-25:**
- the service now also writes a per-run record (`run_store`, never raises);
- F1's proposal rules moved, unchanged, to `trade_rules`;
- deadlines and durations now use the monotonic clock;
- sign-in, confirmation, session and approval windows take the stricter of the wall and monotonic clocks
  (section 7).

**Evidence for those changes:**
- 2,043 passed, 0 failed, in the final full suite;
- the clock-jump tests;
- the deadline file passing 8 of 8 runs, and 4 of 4 under ±185.7 s steps.

The live run is also the first live use of the new clock handling.

**Settings for the run:** market analyst, shallow depth, 15 calls / $0.04, no retry, Alpaca disabled.

**[pending live]** That run is needed for the live 41 s figure. It also exercises the new run record end to
end: afterwards `list_recent_runs` / `get_run_metrics` can read it offline, at no cost.

**Remaining risk:** Gemini availability. Both earlier dashboard attempts failed on its 503s.
