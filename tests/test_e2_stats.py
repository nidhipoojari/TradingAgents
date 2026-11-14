"""E2 counters stay exact under parallel tool calls and report a hit rate."""

from concurrent.futures import ThreadPoolExecutor

from tradingagents import e2_cache


def test_counts_exact_under_threads_and_hit_rate():
    e2_cache.reset_stats()
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: e2_cache._count("hits" if i % 4 else "misses"), range(4000)))

    snapshot = e2_cache.stats_snapshot()
    assert snapshot["hits"] == 3000 and snapshot["misses"] == 1000
    assert snapshot["hit_rate_pct"] == 75.0
    e2_cache.reset_stats()
    assert e2_cache.stats_snapshot()["hit_rate_pct"] is None
