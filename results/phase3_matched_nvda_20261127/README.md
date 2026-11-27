# Phase 3 vs Phase 2: matched live NVDA run (2026-11-27)

The Phase 3 tree was run once, live (real Gemini calls), under Phase 2's exact Section 4 conditions:
`scripts/phase2_eval.py` plan `phase2` limited to one NVDA run. The conditions were NVDA, 2026-10-23, market analyst
only, gemini-3.5-flash-lite (backup gemini-3.1-flash-lite), E2 cache on (cold), E3 tracing on, 1 debate round and
1 risk round.

| NVDA, same conditions | Phase 2 (`eval/phase2/runs.csv`, 3 runs) | Phase 3 (this run) |
| --- | --- | --- |
| Wall time (CLI start to exit) | 26.8 / 29.6 / 30.2 s (median 29.6) | **27.4 s** |
| LLM calls | 10 / 11 / 10 | **10** |
| Tokens | 31,341 / 31,664 / 32,793 | **32,393** |
| Estimated cost | $0.0204 / $0.0209 / $0.0217 | **$0.0213** |
| E1 fallbacks | - | 0 |
| Rating | Overweight (runs 1 and 3) | Hold |

Result: no regression. Every measure is inside Phase 2's range. The rating differs from Phase 2's because LLM output
varies from run to run; the F1 live run (four analysts) on the same date gave Overweight.

Files: `runs.csv` and `manifest.json` (harness output, keys shown only as `<set>`), `token_usage.json`, the E3 summary,
the run record, and `final_trade_decision.md`.

Setup fix found by this run: `E3_TRACE_ENABLED=1` failed on a fresh `pip install ".[e3]"` because Langfuse's LangChain
callback needs the `langchain` package. It is now in the `e3` extra.
