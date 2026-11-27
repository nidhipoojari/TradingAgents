"""Phase 2 limitation 6, offline: scoring saved decisions against later prices without look-ahead."""

from __future__ import annotations

import json

import pytest

from tradingagents.offline_backtest import (
    DISCLAIMER,
    Decision,
    consistency,
    evaluate,
    load_closes,
    load_decisions,
    score,
)

# Trading days around a Friday decision date (2026-10-23), weekends skipped.
CLOSES = [("2026-10-21", 100.0), ("2026-10-22", 101.0), ("2026-10-23", 500.0),   # D's own close: never used
          ("2026-10-26", 100.0), ("2026-10-27", 102.0), ("2026-10-28", 104.0), ("2026-10-29", 99.0)]


def _d(rating, ticker="NVDA", day="2026-10-23", source="a"):
    return Decision(ticker, day, rating, source)


@pytest.mark.unit
def test_entry_is_the_first_close_after_the_decision_date():
    s = score(_d("Buy"), CLOSES, 1)
    assert (s.entry_date, s.exit_date, s.forward_return) == ("2026-10-26", "2026-10-27", 0.02)
    assert s.hit is True and s.signed_return == 0.02


@pytest.mark.unit
def test_no_price_on_or_before_the_decision_date_can_influence_the_score():
    poisoned = [(d, (1e9 if d <= "2026-10-23" else c)) for d, c in CLOSES]
    assert score(_d("Buy"), poisoned, 2).forward_return == score(_d("Buy"), CLOSES, 2).forward_return == 0.04


@pytest.mark.unit
def test_a_horizon_past_the_last_close_is_pending_not_extrapolated():
    s = score(_d("Buy"), CLOSES, 5)
    assert s.status == "pending" and s.forward_return is None and s.entry_date == "2026-10-26"
    assert score(_d("Buy", day="2026-10-29"), CLOSES, 1).status == "pending"


@pytest.mark.unit
@pytest.mark.parametrize("rating, horizon, hit, signed", [
    ("Overweight", 2, True, 0.02), ("Sell", 2, False, -0.04), ("Underweight", 3, True, 0.005),
    ("Hold", 1, True, 0.0), ("Hold", 2, False, 0.0),
])
def test_direction_hits_and_signed_returns(rating, horizon, hit, signed):
    s = score(_d(rating), CLOSES, horizon)
    assert (s.hit, s.signed_return) == (hit, signed)


@pytest.mark.unit
def test_unrated_and_unpriced_decisions_are_not_scored():
    assert score(_d("REVIEW"), CLOSES, 1).status == "unrated"
    assert score(_d("Buy"), None, 1).status == "no_prices"


@pytest.mark.unit
def test_alpha_against_a_benchmark():
    bench = [("2026-10-26", 50.0), ("2026-10-27", 50.5)]
    s = score(_d("Buy"), CLOSES, 1, benchmark=bench)
    assert (s.benchmark_return, s.alpha) == (0.01, 0.01)


@pytest.mark.unit
def test_consistency_is_reported_apart_from_accuracy():
    decisions = [_d("Buy", source="1"), _d("Buy", source="2"), _d("Hold", source="3"), _d("Sell", ticker="AAPL")]
    c = consistency(decisions)
    assert c["cells_with_repeats"] == 1 and c["per_cell"]["NVDA 2026-10-23"]["agreement"] == 0.667
    report = evaluate(decisions, {"NVDA": CLOSES}, horizons=(1,))
    assert report.accuracy["1"]["scored"] == 3 and report.accuracy["1"]["not_scored"] == {"no_prices": 1}
    assert report.accuracy["1"]["hit_rate"] == 1.0                       # +2 %: Buy right; Hold at the 2 % band edge
    assert evaluate(decisions, {"NVDA": CLOSES}, horizons=(2,)).accuracy["2"]["hit_rate"] == 0.667   # +4 %: Hold wrong
    assert report.disclaimer == DISCLAIMER and "does not show" in DISCLAIMER


@pytest.mark.unit
def test_decisions_come_from_successful_runs_once_each(tmp_path):
    def e3(folder, name, **summary):
        (tmp_path / folder).mkdir(exist_ok=True)
        (tmp_path / folder / name).write_text(json.dumps({"ticker": "nvda", "analysis_date": "2026-10-23",
                                                          **summary}))
    e3("a", "NVDA_x_e3.json", status="success", decision="**Rating**: Overweight\n\nWhy.")
    e3("b", "NVDA_x_e3.json", status="success", decision="**Rating**: Overweight\n\nWhy.")   # a copy
    e3("a", "NVDA_y_e3.json", status="failed", decision=None)
    e3("a", "NVDA_z_e3.json", status="success", decision="no rating line")
    found = load_decisions([tmp_path])
    assert [(d.ticker, d.rating) for d in found] == [("NVDA", "Overweight"), ("NVDA", "REVIEW")]


@pytest.mark.unit
def test_yfinance_csv_dates_with_offsets_are_read(tmp_path):
    path = tmp_path / "NVDA-YFin-data.csv"
    path.write_text("Date,Open,High,Low,Close,Volume\n2026-10-26 00:00:00-04:00,1,1,1,186.5,10\nbad,row\n")
    assert load_closes(path) == [("2026-10-26", 186.5)]
