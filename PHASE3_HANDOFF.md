# Phase 3 handoff: state at the 2026-11-27 checkpoint

Branch `feature/phase3-f1-f2`. This is a checkpoint so that the team can finish testing; it is **not** ready to merge
into `main`. `origin/main` has moved since this branch started (PR #13, README), so a merge or rebase will be needed
later. The evidence is in `results/phase3_final_eval_20261127/` (see its README). Earlier evidence is in
`PHASE3_REPORT_SUPPORT.md`, `PHASE3_GAP_REPORT.md` and `PHASE3_CHANGES.md`.

Labels: **live** = real Gemini calls; **offline** = scripted models and offline data, with no provider and no
network; **replay** = scripted models replaying real per-call latencies.

## Verified

**Live (real Gemini, run by the team on 2026-11-27):**
- **F1 dashboard, NVDA** (`20261127-111852-5a8696f8`, 2026-10-23, four analysts, shallow, E2 off): completed,
  rating Overweight. 16 LLM calls, 52,260 tokens in / 7,692 out. E3 estimate $0.034445 (successful calls); budget
  accounting $0.035440 (conservative).
  - Workflow 28.0 s; click to result 31.1 s, so the **41 s target was met**.
- **E1 recovered from a real Gemini 429** in that run. The Portfolio Manager's call on gemini-3.5-flash-lite was
  refused (per-minute quota). The breaker opened, and gemini-3.1-flash-lite answered.
- **CLI, NVDA** (`20261127-113820-f97deed2`, same date, four analysts, E2 off): completed, Overweight. 15 LLM calls
  (no fallback), 49,129 in / 8,367 out, 24.7 s.

**F2:**
- **Production server against the live F1 record** (offline, stdio, unmodified `tradingagents-mcp`):
  - discovery of all 5 tools;
  - `get_report` (all 9 sections), `get_run_metrics`, `list_recent_runs`, `propose_trade` (same `proposal_id` as
    F1: `ta-20261127-111852-5a8696f8-buy`);
  - input validation, source filtering, 12 concurrent reads, and one audit line per call; no secrets returned.
- **Transports** (offline): stdio, and HTTP on loopback (token required; unsafe start-ups refused).
- Busy, timeout, budget and bad-input handling, through a real server process with scripted models (offline).

**Offline / replay:**
- Full suite: 2,083 passed, 1 skipped, before this session's fixes. See `12_full_suite_after_fixes/` for after.
- **Phase 2 vs Phase 3 on identical inputs** (replay of Phase 2's live `01_NVDA` / `03_NVDA`): within 0.2 s (≤0.75 %),
  with 10 LLM calls in every case, in-process, through the service and through the worker.
- **Prompts are unchanged:** every model call's agent and prompt size is identical between the Phase 2 tree and the
  current tree (market only: 10 calls; four analysts: 15 calls).
- CLI, F1 and F2 give identical results on an identical workload.
- Section 4.3 error cases (see below).

## Why the live F1 run used 16 calls and about 60k tokens, against Phase 2's 10 and 32k

**The workloads differ.** Phase 2's benchmark (`scripts/phase2_eval.py`) ran `--analysts market` with E2 on. The F1
run used all four analysts with E2 off.

- **Calls: 16 = 10 + 5 + 1.**
  - 10 for the Phase 2 workload;
  - 5 for the three extra analysts (news 2, fundamentals 2, sentiment 1);
  - 1 E1 retry after the 429.

  The live CLI run on the same four-analyst workload, with no 429, made 15.
- **Tokens:**
  - The three extra analysts used 14,764 in / 2,942 out ($0.0118).
  - The nine agents both runs share used 37,496 in against Phase 2's mean of 26,734. All of the increase is in the
    bull, bear and three risk analysts, about +2.3k each, because they read the three extra reports.
  - Research Manager, Trader and Portfolio Manager are flat. Outputs are slightly lower.
- **The Market Analyst optimisation is still active:** the first prompt is identical (1,853 tokens), with one tool
  round and 2 calls.
- **Not yet measured live:** a matched run (market only, E2 on, the same date). This is the remaining live
  comparison.

## Fixed in this checkpoint (each with a test that fails before the fix)

1. **F2:** `analyze_stock` accepted `include_reports: "yes"` / `"1"` / `1` (lax coercion) and started a full,
   paid analysis. It is now a strict boolean.
2. **Run records stored a relative E3 summary path.** An MCP server started from another directory silently lost
   the per-agent table and the E3 timing. The path is now absolute.
3. **CLI run records never linked their E3 summary** (it is saved after the record), so `get_run_metrics` reported
   their cost as unavailable. The CLI now attaches it after saving.
4. **The dashboard showed the E2 tile as "n/a" when E2 was off.** It now shows `off`,
   `n/a (no cacheable requests)`, or the rate with its counts.
5. **`get_run_metrics` left out the failed-call accounting** behind the budget figure (`failed_calls`,
   `calls_without_usage`, `basis`), and returned `e2_cache: null` when E2 was off. Both are now shown.
6. **E3's raw `cost_note` asked for model prices** when the actual cause was a failed call with no usage. It now
   names the cause. Phase 2's fields are unchanged, and no token counts are invented.

## Section 4.3 error cases (offline, end to end through the worker and service)

- **Network:**
  - market data down with E2 warm: completed, 100 % hits;
  - market data down with E2 cold: completed, with "vendor unavailable" markers;
  - LLM network down: stops cleanly with the record kept. It gets category `error` with no hint; a dedicated
    category is open below.
- **Provider errors:**
  - 429, quota exhausted or 503 on the main model: the backup completes the run (1 switch, breaker opens);
  - both models 429 / quota / 503: stops as `rate_limited` / `quota_exhausted` / `provider_unavailable`, with
    advice.
- **Langfuse:**
  - not configured: tracing is off and the run succeeds;
  - E3 explicitly on without keys: refused before the run, with a clear message, and no run record is written.
- **F1:** oversized orders, a broker refusal, market closed (limit orders rest until the open; market orders are
  refused), a user rejection, and missing Alpaca keys are covered by named unit and integration tests against a
  fake Alpaca (`07_f1_named_tests/`).
- **MCP:** bad input, busy, timeout and per-run limits are tested over the protocol. A requests-per-minute rate
  limit is **not implemented**: the limits are one analysis at a time, the per-run call and cost limits, and the
  timeout.

## Still pending

1. **F2 end-to-end live `analyze_stock` through a real MCP client** (Claude Code; Claude Desktop and MCP Inspector
   are not tested). This needs approval for the Gemini cost:
   - **market only:** about 10 calls (11 with a fallback), about 32k tokens, about $0.021, limited to 15 calls /
     $0.04;
   - **four analysts:** 15–16 calls, about 60k tokens, about $0.035. That is 89 % of the $0.04 MCP cost limit, so
     a run with longer reports can be stopped by the budget.
2. **A matched live comparison with Phase 2:** market only, E2 on, NVDA 2026-10-23, ideally 3 runs, followed by a
   warm-E2 run.
3. **Screenshots** for the report: Claude Code `/mcp` and tool calls, and the F1 dashboard.
4. **A Langfuse check of the `source:` tag through its API** (a read request; needs approval).
5. **A paper order placed through the dashboard UI live.** Only the smoke script's order exists (placed and
   cancelled).
6. **Sections 6.1–6.3** (cloc, lizard and hotspots): to be done **after** the merge into `main`, as agreed.
7. **Decisions for the team:**
   - the `e3` extra pins `langfuse>=3.0,<5.0`, but 5.2.0 is installed and tested; widen to `<6`?
   - add a `network` error category for LLM connection failures?

## Connecting Claude Code as the MCP client

Run these from the project root: the server reads `.env` from its working directory, and the F1 run's record was
written before fix 2. `--mcp-config` applies to this session only; `--strict-mcp-config` hides your other MCP
servers.

```bash
cd ~/Projects/TradingAgents-cli-startup
# Read-only phase: analyze_stock is refused before any run (TRADINGAGENTS_MCP_MAX_LLM_CALLS=-1, no Gemini key)
claude --mcp-config results/phase3_final_eval_20261127/13_claude_code_client/tradingagents_readonly.json --strict-mcp-config
#   then: /mcp   (should list "tradingagents" with 5 tools)
#   ask:  "Use list_recent_runs, then get_run_metrics, get_report (section final_decision) and propose_trade
#          for run 20261127-111852-5a8696f8."
# Live phase (only after approval; uses .env's Gemini key, E2 on, no Alpaca keys):
MCP_TOOL_TIMEOUT=900000 claude --mcp-config results/phase3_final_eval_20261127/13_claude_code_client/tradingagents_live.json --strict-mcp-config
#   ask:  "Call analyze_stock with ticker NVDA, trade_date 2026-10-23, analysts [\"market\"]."
```

**Timing a live F2 call.** Take the server-side end-to-end time from `elapsed_seconds` in
`~/.tradingagents/logs/mcp_requests.jsonl` (the analyze_stock line). Take the workflow time from the answer's
`usage.elapsed_seconds`, and the deadline clock from `get_run_metrics` → `timing.run_deadline.elapsed_seconds`.
Claude Code's own wait includes the model's turn, so it is not the F2 figure.
