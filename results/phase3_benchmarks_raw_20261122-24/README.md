# Phase 3 raw benchmark outputs (2026-11-22 to 2026-11-24)

Copied byte-for-byte on 2026-11-25 from the Claude Code session scratchpad, where the runs wrote them, so they
survive a reboot. Nothing was re-run or edited. All of it is **offline replay**: scripted models replaying the
per-call latencies of saved real runs, offline data vendors, no API key, no network, no orders.

**Not here, unchanged:** the Phase 2 baseline (`eval/phase2/`, `results/phase2/benchmarks.csv`; the
29.6 s live median) and the evaluation matrices (`results/phase3_eval_matrix/`,
`results/phase3_eval_matrix_mcp_limits/`).

## latency/ (`scripts/benchmark_phase3_latency.py`)
Times run from request creation to the result, worker start-up included. Each `benchmark.json` has
`profiles` (the replayed latencies) and `scenarios` (n, median, p95, max, outcome, per-run seconds);
`work/` holds each run's own records (E3 summary, token_usage.json, E1 log).

| Folder | When | What |
| --- | --- | --- |
| `bench_check/` | 11-23 13:36 | 1-run sanity check |
| `bench_full/` (+ `bench_full.log`) | 11-23 13:52 | The main table in PHASE3_CHANGES.md "4. Performance" |
| `bench_partial/` | 11-23 13:55 | 10-call, primary 503 on its first 2 calls |
| `bench_rest/` (+ log) | 11-23 14:50 | 12-call cold, 12-call partial outage, hung primary (30 s and 60 s step deadlines) |
| `bench_breaker/` | 11-23 16:06 | E1 circuit breaker, first measurement |
| `bench_cool30/`, `bench_cool60/` | 11-23 18:03, 18:12 | Breaker cooldown 30 s vs 60 s (n=5): the cooldown table |
| `final_bench/` | 11-24 10:17 | Final re-check on the current tree |
| `ab_profiles.json` | | The 10-call and 12-call latency profiles |

## replay_ab/: Phase 2 code vs Phase 3 code on the same replay
"Performance against the actual Phase 2 baseline" in PHASE3_CHANGES.md. Rows: label (`phase2` = `aa763f7`,
`committed` = `6547d48`, `current` = working tree), mode (graph / service / worker), profile, E2, E3, n,
median, min, max, p95.
- `p2_01_NVDA.json`, `p2_03_NVDA.json`: the latencies of Phase 2 runs `01_NVDA` and `03_NVDA`.
- `zero.json`: zero latency, for framework overhead.
- `final_ab.jsonl`: the source of the **21.94 s** figure (`phase2`, graph, `p2_01_NVDA`, n=5).

**21.94 s is not comparable with 29.6 s.** The 21.94 s figure is in-process time (graph construction to
decision saved) replaying 21.52 s of model latency. The 29.6 s figure is the live Phase 2 *process* wall time
(NVDA x3, median), which includes about 4 s of start-up. The like-for-like live figure is `cli_elapsed`,
22.6 s for `01_NVDA`. See `PHASE3_REPORT_SUPPORT.md`, section 1.

## cancel/: `bench_1.out` ... `bench_10.out` (11-22)
Cancel and release timing of a worker whose call never returns (2.0 s grace).

## e2/ (11-22)
E2 cold and warm timings, old vs new code (`e2_{old,new}_{cold,warm}.json`), with their cache folders.

## matrix_logs/
Console logs of the 192-cell matrix (`matrix_full.log`) and of the 96-cell MCP re-run (`mcp_matrix.log`),
plus the 1-cell smoke check (`matrix_smoke/`). The results themselves are in `results/phase3_eval_matrix*/`.

## scripts/
The scratch harnesses that produced the files above (`phase_ab.py`, `ab_bench.py`, `phase_matrix.sh`,
`cancel_bench*.py`, `worker_measure.py`, `e2_offline_bench.py`, `bench_worker.py`). They are kept as
evidence, not as maintained tools. They were written to run from the scratchpad against the paths of that
time.

Secret scan before copying (Google, Alpaca and Langfuse key patterns): no matches.
