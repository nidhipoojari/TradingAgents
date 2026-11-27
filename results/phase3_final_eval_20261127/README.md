# Phase 3 final evaluation evidence (2026-11-27)

Every folder is labelled by kind:
- **offline**: scripted models and offline data, with no provider, no network and no orders;
- **replay**: scripted models replaying real per-call latencies;
- **live-record**: reads saved records of real runs, makes no new calls;
- **live**: real Gemini calls, made by the team, not by these scripts.

`*.log` files are git-ignored by the repository's rules, so the logs named below exist only on the machine that
ran them. The JSON and JSONL results beside them are committed. Harnesses are in `harness/` (run with the
project's `.venv`). Per-run `work/` folders, SQLite caches and lock files are not committed.

| Folder | Kind | What | Result |
| --- | --- | --- | --- |
| `01_full_suite/` | offline | full pytest before this session's fixes (`junit.xml`) | 2,083 passed, 1 skipped (Bedrock SDK not installed), 0 failed |
| `02_mcp_stdio/` | offline | real FastMCP stdio client against the real server process (scripted models in its worker): all 5 tools, 10 invalid `analyze_stock` inputs, `limit` edge cases, unknown tools, busy, timeout, budget, missing prices, Langfuse off | all as designed, except the defect below; 0 order/approval files |
| `02_mcp_http/` | offline | streamable HTTP on 127.0.0.1: no or wrong token refused; correct token answers all tools; 4 unsafe start-ups refused (exit 2) | pass |
| `03_fix_strict_include_reports/` | offline | **defect fixed:** `include_reports: "yes"` / `"1"` / `1` / `"on"` was coerced to True and started a full analysis | test fails before (6 cases) and passes after |
| `04_benchmark_ab/ab.jsonl` | replay | Phase 2 code (`aa763f7`) vs the current tree, replaying the Phase 2 live runs `01_NVDA` / `03_NVDA`, E2 cold and warm, n=5 (n=30 for zero latency) | within 0.2 s (≤0.75 %); 10 LLM calls in every row |
| `05_interface_consistency/` | offline | the same scripted workload through the real CLI entry, the F1 job path and the F2 tool path | identical status, rating, report hash, calls, tool calls, tokens |
| `06_error_scenarios/` | offline | Section 4.3 provider, network and tracing faults end to end (14 scenarios) | as designed; see the table in `PHASE3_HANDOFF.md` |
| `06_error_scenarios_run1_*`, `_run2_*` | offline | superseded runs with harness bugs (kept; see `SUPERSEDED.txt`) | not product defects |
| `07_f1_named_tests/` | offline | 167 named F1 safety, approval, rejection, market-closed and no-keys tests | 167 passed |
| `08_live_f1_analysis/` | live-record | copies of the live F1 run `20261127-111852-5a8696f8` (record, E3 summary, proposal) and the live CLI run `20261127-113820-f97deed2`; `token_breakdown.txt` compares the per-agent figures with Phase 2 | see `PHASE3_HANDOFF.md` |
| `09_fix_e3_path/` | offline | **defects fixed:** a relative E3 path in run records (F2 lost per-agent data from another working directory); the E2 tile showed "n/a" when E2 was off; the E3 cost note blamed pricing for a call with no usage; `get_run_metrics` hid the failed-call accounting | each test fails before and passes after |
| `10_prompt_compare/` | offline | every model call's agent and prompt size, Phase 2 tree vs current, market only and all four analysts | identical: 10 and 15 calls; 33,831 and 46,367 prompt characters |
| `11_f2_live_record/` | live-record | the **production** `tradingagents-mcp` (unmodified, stdio) against a copy of the live F1 record: 40 calls, source filtering, 12 concurrent reads, auditing; `analyze_stock` guarded so that it cannot run | all correct; 36 audit lines; no secrets; source record unchanged (sha256) |
| `12_full_suite_after_fixes/` | offline | full pytest and Ruff after fixes 1, 2, 4, 5 and 6 (before the CLI fix in `14_`) | 2,097 passed, 1 skipped, 0 failed; Ruff clean |
| `13_claude_code_client/` | config | session-only `--mcp-config` files for connecting Claude Code as the MCP client (read-only guard; live) | not yet run by a person |
| `14_fix_cli_e3_link/` | offline | **defect fixed:** CLI run records never linked their E3 summary, which is saved after the record, so the cost showed as unavailable | test added; `consistency_after/`: the real CLI entry now links it ($0.00018, as F1 and F2) |
| `15_final_suite/` | offline | final full pytest and Ruff after every fix, on exactly the code in checkpoint `68c10a8` | 2,098 passed, 1 skipped (Bedrock SDK not installed), 0 failed in 222.5 s; Ruff clean |
