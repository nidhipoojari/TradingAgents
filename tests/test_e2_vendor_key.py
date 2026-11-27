"""E2 vendor provenance: a cached answer is only served to a run configured for the same vendors.

Phase 2 keyed the cache by (tool, request) only, so after switching ``data_vendors``
a repeated past-dated request came back with the previous vendor's answer, and a
Phase 2 row cannot say which vendor wrote it. Now every key carries the vendor
chain, Phase 2 rows are never matched, and ``adopt_legacy`` re-keys them only when
the runs' own state logs prove which vendors produced them. Schema, the
past-dates-only rule and the counters are unchanged.
"""

import json
import sqlite3
from datetime import date

import pytest

from tradingagents import e2_cache
from tradingagents.dataflows import config as dataflows_config, router

PAST = ("NVDA", "2026-01-02", "2026-01-09")
DEFAULT_VENDORS = dict(dataflows_config._config["data_vendors"])


@pytest.fixture
def vendors(tmp_path, monkeypatch):
    """Both stock vendors answer offline, saying who they are; returns the call log."""
    calls = []
    for name in ("yfinance", "alpha_vantage"):
        monkeypatch.setitem(router.VENDOR_METHODS["get_stock_data"], name,
                            lambda *a, _n=name, **k: calls.append(_n) or f"{_n} rows")
    monkeypatch.setitem(dataflows_config._config, "data_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(dataflows_config._config, "e2_cache_enabled", True)
    return calls


def _use(monkeypatch, vendor):
    vendors_cfg = dict(dataflows_config._config["data_vendors"], core_stock_apis=vendor)
    monkeypatch.setitem(dataflows_config._config, "data_vendors", vendors_cfg)


def _legacy_row(cache_dir, result="phase2 rows"):
    """A row exactly as Phase 2 wrote it: the request with no vendor."""
    e2_cache._connect().close()
    with sqlite3.connect(cache_dir / "e2_cache.sqlite") as db:
        db.execute("INSERT INTO cache VALUES (?, ?, ?, ?)",
                   ("get_stock_data", json.dumps([list(PAST), {}], sort_keys=True), result, "2026-11-13T10:00:00"))


def _state_log(results_dir, run, data_vendors, tool_vendors=None, settings=True):
    log = results_dir / run / "TradingAgentsStrategy_logs" / "full_states_log_2026-01-09.json"
    log.parent.mkdir(parents=True, exist_ok=True)
    entry = {"company_of_interest": "NVDA"}
    if settings:
        entry["run_settings"] = {"data_vendors": data_vendors, "tool_vendors": tool_vendors or {}}
    log.write_text(json.dumps(entry))


# ------------------------------------------------------------------ vendor-keyed entries
@pytest.mark.unit
def test_switching_vendors_does_not_serve_the_other_vendors_answer(vendors, monkeypatch):
    assert router.route_to_vendor("get_stock_data", *PAST) == "yfinance rows"       # default vendor
    _use(monkeypatch, "alpha_vantage")
    assert router.route_to_vendor("get_stock_data", *PAST) == "alpha_vantage rows"  # not the cached yfinance
    assert router.route_to_vendor("get_stock_data", *PAST) == "alpha_vantage rows"  # its own entry, cached
    _use(monkeypatch, "yfinance")
    assert router.route_to_vendor("get_stock_data", *PAST) == "yfinance rows"       # the original entry
    assert vendors == ["yfinance", "alpha_vantage"]                                  # each fetched once


@pytest.mark.unit
def test_a_tool_level_vendor_override_is_kept_apart(vendors, monkeypatch):
    router.route_to_vendor("get_stock_data", *PAST)
    monkeypatch.setitem(dataflows_config._config, "tool_vendors", {"get_stock_data": "alpha_vantage"})
    assert router.route_to_vendor("get_stock_data", *PAST) == "alpha_vantage rows"


@pytest.mark.unit
def test_every_key_carries_its_vendor_chain(vendors, tmp_path):
    router.route_to_vendor("get_stock_data", *PAST)
    with sqlite3.connect(tmp_path / "cache" / "e2_cache.sqlite") as db:
        [(tool, request)] = db.execute("SELECT tool, request FROM cache").fetchall()
    assert tool == "get_stock_data"
    assert json.loads(request) == [list(PAST), {}, {"vendors": "yfinance"}]


@pytest.mark.unit
def test_direct_cache_calls_are_vendor_keyed_too(vendors):
    """No caller can write or read a vendor-less key by leaving the vendor out."""
    e2_cache.save("get_stock_data", PAST, {}, "rows")
    assert e2_cache.lookup("get_stock_data", PAST, {}) == "rows"
    key = e2_cache._request_key(PAST, {}, e2_cache.vendor_chain("get_stock_data"))
    assert key != e2_cache._legacy_request_key(PAST, {})


@pytest.mark.unit
def test_equivalent_vendor_spellings_share_a_chain():
    assert e2_cache.vendor_chain("get_balance_sheet", {"fundamental_data": "sec_edgar, yfinance"}, {}) == \
        e2_cache.vendor_chain("get_balance_sheet", {"fundamental_data": "sec_edgar,yfinance"}, {})
    assert e2_cache.vendor_chain("get_balance_sheet", {"fundamental_data": "yfinance"}, {}) == "yfinance"
    assert e2_cache.vendor_chain("get_stock_data", {}, {"get_stock_data": "alpha_vantage"}) == "alpha_vantage"
    assert e2_cache.vendor_chain("not_a_tool", {}, {}) == "default"


@pytest.mark.unit
def test_vendor_keys_keep_the_historical_only_rule(vendors, monkeypatch):
    _use(monkeypatch, "alpha_vantage")
    today = date.today().isoformat()
    e2_cache.reset_stats()
    router.route_to_vendor("get_stock_data", "NVDA", "2026-01-02", today)
    router.route_to_vendor("get_stock_data", "NVDA", "2026-01-02", today)
    assert vendors == ["alpha_vantage", "alpha_vantage"]            # today: never cached, any vendor
    assert e2_cache.stats_snapshot()["skipped"] == 2


# ------------------------------------------------------------------ Phase 2 (vendor-less) rows
@pytest.mark.unit
@pytest.mark.parametrize("vendor", ["yfinance", "alpha_vantage"])
def test_a_phase2_row_is_never_served_on_trust(vendors, tmp_path, monkeypatch, vendor):
    _legacy_row(tmp_path / "cache")
    _use(monkeypatch, vendor)
    e2_cache.reset_stats()
    assert router.route_to_vendor("get_stock_data", *PAST) == f"{vendor} rows"     # fetched, not the legacy row
    assert vendors == [vendor]
    assert e2_cache.stats_snapshot()["misses"] == 1


@pytest.mark.unit
def test_a_phase2_row_is_adopted_when_the_runs_prove_their_vendors(vendors, tmp_path, monkeypatch):
    _legacy_row(tmp_path / "cache")
    _state_log(tmp_path / "results", "NVDA", DEFAULT_VENDORS)
    _state_log(tmp_path / "results", "AAPL", DEFAULT_VENDORS)

    report = e2_cache.adopt_legacy(tmp_path / "cache", tmp_path / "results", only_these_runs=True)

    assert report["verified"] and report["state_logs"] == 2
    assert (report["legacy_rows"], report["adopted"]) == (1, 1)
    assert router.route_to_vendor("get_stock_data", *PAST) == "phase2 rows"        # under the proven vendors
    _use(monkeypatch, "alpha_vantage")
    assert router.route_to_vendor("get_stock_data", *PAST) == "alpha_vantage rows"  # never under another
    assert vendors == ["alpha_vantage"]
    again = e2_cache.adopt_legacy(tmp_path / "cache", tmp_path / "results", only_these_runs=True)
    assert (again["adopted"], again["already_present"]) == (0, 1)                   # idempotent


@pytest.mark.unit
def test_adoption_follows_the_proven_vendor_not_the_default(vendors, tmp_path, monkeypatch):
    """Rows a Phase 2 run wrote under alpha_vantage are adopted as alpha_vantage's."""
    _legacy_row(tmp_path / "cache", result="alpha_vantage rows from phase 2")
    _state_log(tmp_path / "results", "NVDA", dict(DEFAULT_VENDORS, core_stock_apis="alpha_vantage"))
    assert e2_cache.adopt_legacy(tmp_path / "cache", tmp_path / "results", only_these_runs=True)["adopted"] == 1
    assert router.route_to_vendor("get_stock_data", *PAST) == "yfinance rows"       # default: not served
    _use(monkeypatch, "alpha_vantage")
    assert router.route_to_vendor("get_stock_data", *PAST) == "alpha_vantage rows from phase 2"


@pytest.mark.unit
@pytest.mark.parametrize("logs, reason", [
    ([], "no state logs"),
    ([("NVDA", DEFAULT_VENDORS, False)], "record no vendor settings"),
    ([("NVDA", DEFAULT_VENDORS, True),
      ("AAPL", dict(DEFAULT_VENDORS, core_stock_apis="alpha_vantage"), True)], "different vendor configurations"),
])
def test_unproven_rows_are_not_adopted(vendors, tmp_path, logs, reason):
    _legacy_row(tmp_path / "cache")
    for run, data_vendors, settings in logs:
        _state_log(tmp_path / "results", run, data_vendors, settings=settings)

    report = e2_cache.adopt_legacy(tmp_path / "cache", tmp_path / "results", only_these_runs=True)

    assert not report["verified"] and reason in report["reason"] and report["adopted"] == 0
    assert router.route_to_vendor("get_stock_data", *PAST) == "yfinance rows"       # still not served
    with sqlite3.connect(tmp_path / "cache" / "e2_cache.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 2          # legacy kept + new entry


@pytest.mark.unit
def test_a_dry_run_changes_nothing(vendors, tmp_path):
    _legacy_row(tmp_path / "cache")
    _state_log(tmp_path / "results", "NVDA", DEFAULT_VENDORS)
    report = e2_cache.adopt_legacy(tmp_path / "cache", tmp_path / "results", only_these_runs=True, dry_run=True)
    assert report["verified"] and report["adopted"] == 1
    assert router.route_to_vendor("get_stock_data", *PAST) == "yfinance rows"


@pytest.mark.unit
def test_the_adopt_command_reports_and_signals_refusal(vendors, tmp_path, capsys):
    _legacy_row(tmp_path / "cache")
    assert e2_cache._main(["adopt-legacy", "--cache-dir", str(tmp_path / "cache"),
                           "--results-dir", str(tmp_path / "results")]) == 1
    assert '"verified": false' in capsys.readouterr().out
    _state_log(tmp_path / "results", "NVDA", DEFAULT_VENDORS)
    assert e2_cache._main(["adopt-legacy", "--cache-dir", str(tmp_path / "cache"),
                           "--results-dir", str(tmp_path / "results")]) == 1       # not confirmed
    assert e2_cache._main(["adopt-legacy", "--cache-dir", str(tmp_path / "cache"),
                           "--results-dir", str(tmp_path / "results"), "--only-these-runs-used-this-cache"]) == 0


@pytest.mark.unit
def test_consistent_records_still_need_the_operators_confirmation(vendors, tmp_path):
    _legacy_row(tmp_path / "cache")
    _state_log(tmp_path / "results", "NVDA", DEFAULT_VENDORS)
    report = e2_cache.adopt_legacy(tmp_path / "cache", tmp_path / "results")
    assert not report["verified"] and "confirm" in report["reason"] and report["adopted"] == 0
    assert router.route_to_vendor("get_stock_data", *PAST) == "yfinance rows"


@pytest.mark.unit
def test_a_run_without_a_state_log_blocks_adoption(vendors, tmp_path):
    """As in eval/tracing_31: a second run used the cache but left no state log."""
    _legacy_row(tmp_path / "cache")
    _state_log(tmp_path / "results", "NVDA", DEFAULT_VENDORS)
    unlogged = tmp_path / "results" / "AAPL" / "2026-01-09"
    unlogged.mkdir(parents=True)
    (unlogged / "message_tool.log").write_text("10:00:00 [Tool Call] get_stock_data(...)\n")
    report = e2_cache.adopt_legacy(tmp_path / "cache", tmp_path / "results", only_these_runs=True)
    assert not report["verified"] and "without leaving a state log" in report["reason"]
    assert report["adopted"] == 0


@pytest.mark.unit
def test_a_row_saved_after_the_last_recorded_run_blocks_adoption(vendors, tmp_path):
    e2_cache._connect().close()
    with sqlite3.connect(tmp_path / "cache" / "e2_cache.sqlite") as db:
        db.execute("INSERT INTO cache VALUES (?, ?, ?, ?)",
                   ("get_stock_data", json.dumps([list(PAST), {}]), "late rows", "2099-01-01T00:00:00"))
    _state_log(tmp_path / "results", "NVDA", DEFAULT_VENDORS)
    report = e2_cache.adopt_legacy(tmp_path / "cache", tmp_path / "results", only_these_runs=True)
    assert not report["verified"] and "after the last recorded run" in report["reason"]
