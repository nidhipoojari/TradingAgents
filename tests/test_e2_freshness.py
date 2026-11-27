"""Phase 2 limitation 5, offline: E2 expiry, explicit refresh, inspection and invalidation."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import pytest

from tradingagents import e2_cache
from tradingagents.dataflows import router
from tradingagents.dataflows.config import get_config, set_config

REQ = ("get_stock_data", "NVDA", "2026-07-23", "2026-10-23")


@pytest.fixture
def cache(tmp_path, monkeypatch):
    original = dict(get_config())

    def configure(**extra):
        set_config({**original, "e2_cache_enabled": True, "data_cache_dir": str(tmp_path), **extra})
        e2_cache.reset_stats()

    answers = iter(["prices v1", "prices v2", "prices v3", "prices v4"])
    calls = []

    def vendor(method, *args, **kwargs):
        calls.append((method, args))
        return next(answers)

    monkeypatch.setattr(router, "_route_to_vendor_uncached", vendor)
    configure()
    yield configure, calls, tmp_path
    set_config(original)


def _age(tmp_path, days):
    """Make every saved row ``days`` old."""
    when = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with closing(sqlite3.connect(tmp_path / "e2_cache.sqlite")) as db, db:
        db.execute("UPDATE cache SET saved_at = ?", (when,))


@pytest.mark.unit
def test_without_settings_saved_answers_never_expire(cache):
    configure, calls, tmp_path = cache
    router.route_to_vendor(*REQ)
    _age(tmp_path, 3650)
    assert router.route_to_vendor(*REQ) == "prices v1" and len(calls) == 1      # a hit: no external request
    assert e2_cache.stats_snapshot() == {"hits": 1, "misses": 1, "skipped": 0, "hit_rate_pct": 50.0}


@pytest.mark.unit
def test_an_answer_older_than_the_maximum_age_is_fetched_again_and_replaced(cache):
    configure, calls, tmp_path = cache
    configure(e2_cache_max_age_days=7)
    router.route_to_vendor(*REQ)
    _age(tmp_path, 3)
    assert router.route_to_vendor(*REQ) == "prices v1" and len(calls) == 1      # 3 days < 7: still a hit
    _age(tmp_path, 8)
    assert router.route_to_vendor(*REQ) == "prices v2" and len(calls) == 2      # expired: fetched again
    assert router.route_to_vendor(*REQ) == "prices v2" and len(calls) == 2      # and the new answer is reused
    snap = e2_cache.stats_snapshot()
    assert (snap["hits"], snap["misses"], snap["expired"]) == (2, 2, 1)


@pytest.mark.unit
def test_refresh_fetches_again_and_replaces_with_a_new_timestamp(cache):
    configure, calls, tmp_path = cache
    router.route_to_vendor(*REQ)
    _age(tmp_path, 30)
    configure(e2_cache_refresh=True)
    assert router.route_to_vendor(*REQ) == "prices v2" and len(calls) == 2
    configure()                                                                  # refresh off again
    assert router.route_to_vendor(*REQ) == "prices v2" and len(calls) == 2      # the replacement is served
    newest = e2_cache.info(tmp_path)["newest_saved_at"]
    assert datetime.fromisoformat(newest) > datetime.now() - timedelta(minutes=1)


@pytest.mark.unit
def test_refresh_still_never_caches_today_or_errors(cache):
    configure, calls, tmp_path = cache
    configure(e2_cache_refresh=True, e2_cache_max_age_days=1)
    router.route_to_vendor("get_insider_transactions", "NVDA", None)
    assert e2_cache.info(tmp_path)["rows"] == 0


@pytest.mark.unit
def test_info_and_invalidate_by_ticker_tool_and_date(cache):
    configure, calls, tmp_path = cache
    router.route_to_vendor(*REQ)
    router.route_to_vendor("get_fundamentals", "NVDA", "2026-10-23")
    router.route_to_vendor("get_stock_data", "AAPL", "2026-07-23", "2026-10-23")
    report = e2_cache.info(tmp_path)
    assert report["rows"] == 3 and report["by_ticker"] == {"NVDA": 2, "AAPL": 1}
    assert report["by_tool"] == {"get_stock_data": 2, "get_fundamentals": 1} and report["rows_without_vendor"] == 0

    assert e2_cache.invalidate(tmp_path, ticker="nvda", tool="get_fundamentals", dry_run=True) == \
        {"matched": 1, "deleted": 0, "dry_run": True}
    assert e2_cache.info(tmp_path)["rows"] == 3                                 # a dry run changes nothing
    assert e2_cache.invalidate(tmp_path, ticker="NVDA")["deleted"] == 2
    assert e2_cache.info(tmp_path)["by_ticker"] == {"AAPL": 1}
    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    assert e2_cache.invalidate(tmp_path, saved_before=tomorrow)["deleted"] == 1
    with pytest.raises(ValueError, match="at least one"):
        e2_cache.invalidate(tmp_path)
    with pytest.raises(ValueError):
        e2_cache.invalidate(tmp_path, saved_before="23/10/2026")


@pytest.mark.unit
def test_the_command_line(cache, capsys):
    configure, calls, tmp_path = cache
    router.route_to_vendor(*REQ)
    assert e2_cache._main(["info", "--cache-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == 1
    assert e2_cache._main(["invalidate", "--cache-dir", str(tmp_path), "--ticker", "NVDA"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] == 1


@pytest.mark.unit
def test_a_bad_maximum_age_fails_loudly(cache):
    configure, calls, tmp_path = cache
    configure(e2_cache_max_age_days=-1)
    with pytest.raises(ValueError, match="must be >= 0"):
        router.route_to_vendor(*REQ)


@pytest.mark.unit
def test_the_run_record_shows_freshness_only_when_it_happened():
    from tradingagents.service import _e2_delta

    before = {"hits": 0, "misses": 0, "skipped": 0}
    assert _e2_delta(before, {"hits": 2, "misses": 0, "skipped": 0}) == \
        {"hits": 2, "misses": 0, "skipped": 0, "hit_rate_pct": 100.0}
    assert _e2_delta(before, {"hits": 1, "misses": 1, "skipped": 0, "expired": 1})["expired"] == 1
