# Phase 2 changes (TradeDesk AI)

Every Phase 2 change to the original TradingAgents code, grouped by enhancement.
In the code, look for comments starting with `E1:`, `E2:` or `E3:`.
On GitHub, PRs carry the labels **E1 model routing**, **E2 data cache** and **E3 tracing**.

## E1: Model routing + backup model (owner: Nidhi; PRs #2, #3)

**What it does.** Each of the 12 agents is assigned a "quick" (cheaper) or "deep" (stronger) model in one table.
If the main model fails (rate limit 429, server error, unknown model), the same request goes to a backup model,
so the run finishes instead of crashing. Each switch is written to `<results_dir>/e1_fallback_log.txt`.

| File | Change |
| --- | --- |
| `tradingagents/e1_routing.py` | **New.** `ROUTING_TABLE` (agent → tier), `pick_model()`, `build_model()` (adds backup via LangChain `with_fallbacks`), `FallbackLogger` (log file lines for main and backup failures) |
| `tradingagents/graph/trading_graph.py` | Models built with `build_model()`; backup names saved in run settings |
| `tradingagents/graph/setup.py` | Agents get their model through `llm_for(agent)` → routing table |
| `tradingagents/default_config.py` | New settings `quick_fallback_llm`, `deep_fallback_llm` (default: none = old behavior) |
| `.env.example` | Documents `TRADINGAGENTS_QUICK_FALLBACK_LLM`, `TRADINGAGENTS_DEEP_FALLBACK_LLM` |
| `tests/test_e1_routing.py` | **New.** 4 offline tests (routing table, fake 429 → backup answers, no backup = unchanged, backup failure logged) |
| `tests/test_graph_end_to_end.py` | Points its fake model at `e1_routing` (where models are now built) |

**How to turn it on.** Add to `.env`:
```
TRADINGAGENTS_QUICK_FALLBACK_LLM=gemini-3.5-flash-lite
TRADINGAGENTS_DEEP_FALLBACK_LLM=gemini-3.5-flash-lite
```
**Tested (Nov 13).** Main model set to `gemini-does-not-exist`: 15 calls switched to the backup; the NVDA run finished
(rating Overweight, 33.4 s). `gemini-3.8-flash` as backup was too slow on the free tier.

## E2: Market-data cache (owner: Nidhi + Joe; PR #5)

**What it does.** Every data request (prices, indicators, fundamentals, statements, news, insider trades)
is saved in one SQLite file, `<data_cache_dir>/e2_cache.sqlite`. When an agent asks for the exact same
past-dated data again, the answer comes from that file instead of Yahoo / SEC.
Only requests whose dates are all in the past are cached (past data doesn't change); "today" is always
downloaded fresh. Error answers are never cached. The cache key includes the dates, so there is no look-ahead bias.

**Change from the Phase 1 plan.** We planned SQLite + Parquet. We used SQLite only: the price history for one
ticker is ~1,250 rows, so Parquet would add a dependency (pyarrow) with no measurable gain, and caching the
*tool answers* covers all data types, not just prices.

| File | Change |
| --- | --- |
| `tradingagents/e2_cache.py` | **New.** `lookup()` / `save()` on a SQLite table keyed by (tool, request); past-dates-only and no-errors rules; hit/miss counters |
| `tradingagents/dataflows/router.py` | `route_to_vendor()` checks the cache first; original logic moved to `_route_to_vendor_uncached()` |
| `tradingagents/default_config.py` | New setting `e2_cache_enabled` (default off = old behavior) |
| `.env.example` | Documents `TRADINGAGENTS_E2_CACHE=true` |
| `tests/test_e2_cache.py` | **New.** 4 offline tests (repeat comes from cache, other date not reused, today/errors not cached, off = unchanged) |
| `scripts/e2_benchmark.py` | **New.** Times the 14 data requests of one NVDA analysis: E2 off vs cold vs warm (no LLM calls) |

**How to turn it on.** Add `TRADINGAGENTS_E2_CACHE=true` to `.env`.

**Tested (Nov 13, NVDA 2026-10-23, 14 data requests, each run in a fresh process; 2 repeats).**

| Run | Data-load time | Cache hit rate |
| --- | --- | --- |
| E2 off (Phase 1) | 2.17–2.59 s | n/a |
| E2 on, cold cache | 1.72–2.28 s | 0% |
| E2 on, warm cache | 0.44–0.50 s | 92.9% (13/14; the miss is Yahoo news, which failed and is never cached) |

## E3: Tracing + cost monitoring (owner: Shubhan; PR #1)

**What it does.** Records every LLM call per agent (model, time, tokens, errors), sends traces to Langfuse,
and saves a JSON summary with an estimated cost at the end of each run. Turned on with `E3_TRACE_ENABLED=1`.

| File | Change |
| --- | --- |
| `tradingagents/e3_tracing.py` | **New.** `E3MetricsCallback` (per-call timing, tokens, errors) and the run summary + cost estimate |
| `tradingagents/e3_cli_hook.py` | **New.** `E3Session`: turns tracing on from `.env`, saves the JSON summary even if the run crashes |
| `e3_pricing.json` | **New.** Price per million input/output tokens, per model |
| `cli/run.py` | Attaches the E3 callbacks; marks the run completed with its final decision |
| `tradingagents/graph/trading_graph.py` | Passes callbacks into every graph step |
| `pyproject.toml` | Optional `langfuse` dependency |
| `test_e3_tracing.py` | **New.** Offline test of the metrics math |
| `results/phase2/` | Evidence: per-agent CSV, NVDA JSON summary, Langfuse screenshot |

**`.env` keys needed:** `E3_TRACE_ENABLED=1`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`,
optional `E3_PRICING_JSON=e3_pricing.json`, `E3_RESULTS_DIR=results/phase2`.
