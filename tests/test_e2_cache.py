"""E2 tests: the SQLite data cache. Offline, no downloads."""
from tradingagents import e2_cache
from tradingagents.dataflows import router
from tradingagents.dataflows.config import get_config, set_config


def _turn_on(tmp_path, enabled=True):
    set_config({**get_config(), "e2_cache_enabled": enabled, "data_cache_dir": str(tmp_path)})
    e2_cache.reset_stats()


def _fake_vendor(monkeypatch, answer):
    calls = []

    def fake(method, *args, **kwargs):
        calls.append(method)
        return answer
    monkeypatch.setattr(router, "_route_to_vendor_uncached", fake)
    return calls


def test_second_request_comes_from_cache(monkeypatch, tmp_path):
    _turn_on(tmp_path)
    calls = _fake_vendor(monkeypatch, "NVDA prices ...")

    first = router.route_to_vendor("get_stock_data", "NVDA", "2026-07-23", "2026-10-23")
    second = router.route_to_vendor("get_stock_data", "NVDA", "2026-07-23", "2026-10-23")

    assert first == second == "NVDA prices ..."
    assert calls == ["get_stock_data"]            # downloaded only once
    assert e2_cache.STATS == {"hits": 1, "misses": 1, "skipped": 0}


def test_different_date_is_not_reused(monkeypatch, tmp_path):
    """No look-ahead: another analysis date never gets this date's answer."""
    _turn_on(tmp_path)
    calls = _fake_vendor(monkeypatch, "fundamentals")

    router.route_to_vendor("get_fundamentals", "NVDA", "2026-10-23")
    router.route_to_vendor("get_fundamentals", "NVDA", "2026-10-24")

    assert len(calls) == 2


def test_today_and_errors_are_never_cached(monkeypatch, tmp_path):
    _turn_on(tmp_path)
    calls = _fake_vendor(monkeypatch, "DATA_UNAVAILABLE: vendor down")
    router.route_to_vendor("get_news", "NVDA", "2026-10-16", "2026-10-23")
    router.route_to_vendor("get_news", "NVDA", "2026-10-16", "2026-10-23")
    assert len(calls) == 2                         # error answer not saved

    calls = _fake_vendor(monkeypatch, "news")
    router.route_to_vendor("get_insider_transactions", "NVDA", None)   # no date = "now"
    router.route_to_vendor("get_insider_transactions", "NVDA", None)
    assert len(calls) == 2


def test_off_means_old_behavior(monkeypatch, tmp_path):
    _turn_on(tmp_path, enabled=False)
    calls = _fake_vendor(monkeypatch, "prices")
    router.route_to_vendor("get_stock_data", "NVDA", "2026-07-23", "2026-10-23")
    router.route_to_vendor("get_stock_data", "NVDA", "2026-07-23", "2026-10-23")
    assert len(calls) == 2
    assert not (tmp_path / "e2_cache.sqlite").exists()
