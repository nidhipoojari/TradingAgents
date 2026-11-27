"""E2 cache cost alone, offline: 14 requests like one NVDA run, routed with a stub vendor.
cold = first pass (lookup miss + vendor + save); warm = second pass (lookup hits). Median of N."""
import statistics, sys, tempfile, time
from tradingagents import e2_cache
from tradingagents.dataflows import router
from tradingagents.dataflows.config import set_config
REQS = [("get_stock_data", ("NVDA", "2026-09-23", "2026-10-23"))] + \
       [("get_indicators", ("NVDA", ind, "2026-10-23", 30)) for ind in ("rsi", "macd", "boll", "atr", "close_50_sma", "close_200_sma")] + \
       [("get_fundamentals", ("NVDA", "2026-10-23")), ("get_balance_sheet", ("NVDA", "quarterly", "2026-10-23")),
        ("get_cashflow", ("NVDA", "quarterly", "2026-10-23")), ("get_income_statement", ("NVDA", "quarterly", "2026-10-23")),
        ("get_news", ("NVDA", "2026-10-16", "2026-10-23")), ("get_global_news", ("2026-10-23", 7, 10)),
        ("get_insider_transactions", ("NVDA", "2026-10-23"))]
for method, vendors in router.VENDOR_METHODS.items():
    for v in vendors: vendors[v] = (lambda *a, _m=method, **k: f"{_m} " + "x" * 4000)
cold, warm = [], []
for _ in range(int(sys.argv[1])):
    set_config({"data_cache_dir": tempfile.mkdtemp(), "e2_cache_enabled": True})
    for bucket in (cold, warm):
        t = time.perf_counter()
        for m, a in REQS: router.route_to_vendor(m, *a)
        bucket.append(time.perf_counter() - t)
print(f"14 requests: cold median {statistics.median(cold)*1000:.2f} ms, warm median {statistics.median(warm)*1000:.2f} ms, stats {e2_cache.stats_snapshot()['hit_rate_pct']}%")
