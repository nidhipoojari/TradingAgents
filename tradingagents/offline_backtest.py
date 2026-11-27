"""Score saved decisions against later market prices, offline (Phase 3; Phase 2 limitation 6).

``tradingagents/backtest.py`` runs the whole LLM pipeline for every (ticker, date) cell, which costs a paid
call per agent. This module needs no model at all: it scores decisions that runs have already saved (their
E3 summaries carry ticker, analysis date, status and the final decision) against price histories already on
disk (the ``<TICKER>-YFin-data.csv`` files the data layer writes), so it can be re-run any time for free.

No look-ahead
  A decision for date D was made from data up to D. It is entered at the close of the FIRST trading day
  AFTER D and held for ``h`` trading days; only closes after D are ever read. A horizon that runs past the
  last saved close is "pending", never extrapolated.

Metrics (per horizon, kept apart from consistency)
  - forward return of the instrument, and of the benchmark when its prices are available (alpha);
  - direction hit: Buy/Overweight right when the return is > 0, Sell/Underweight when < 0, Hold when
    |return| <= ``hold_band`` (default 2 %);
  - signed return: +1 Buy, +0.5 Overweight, 0 Hold, -0.5 Underweight, -1 Sell, times the return. A per-
    decision score, not a portfolio: no position sizing, costs or compounding (see backtest.py's scope note).
  Consistency (do repeated runs of the same ticker and date agree?) is reported separately: a model can be
  consistent and wrong, or right by chance.

Historical scores do not show that the system will be profitable in the future: the sample is small, the
period is short, and the market regime may change. They measure past decisions only.
"""

from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tradingagents.agents.rating import RATING_REVIEW, parse_rating

DIRECTION = {"Buy": 1.0, "Overweight": 0.5, "Hold": 0.0, "Underweight": -0.5, "Sell": -1.0}
DISCLAIMER = ("Historical backtesting of past decisions; it does not show that the system will be profitable "
              "in the future (small sample, short period, one market regime).")


@dataclass(frozen=True)
class Decision:
    ticker: str
    date: str                          # YYYY-MM-DD, the analysis date
    rating: str
    source: str                        # the E3 summary it came from


@dataclass
class Score:
    decision: Decision
    horizon: int
    status: str                        # scored | pending | no_prices | unrated
    entry_date: str | None = None
    exit_date: str | None = None
    forward_return: float | None = None
    benchmark_return: float | None = None
    alpha: float | None = None
    hit: bool | None = None
    signed_return: float | None = None


def load_decisions(paths: list[Path]) -> list[Decision]:
    """Successful runs' decisions from E3 summaries (files or folders searched recursively), one per run:
    copies of the same summary (same file name) are counted once."""
    found: dict[str, Decision] = {}
    for root in paths:
        files = [root] if Path(root).is_file() else sorted(Path(root).rglob("*_e3.json"))
        for path in files:
            if path.name in found:
                continue
            try:
                summary = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if summary.get("status") != "success" or not summary.get("decision"):
                continue
            found[path.name] = Decision(str(summary["ticker"]).upper(), str(summary["analysis_date"])[:10],
                                        parse_rating(summary["decision"]), str(path))
    return sorted(found.values(), key=lambda d: (d.ticker, d.date, d.source))


def load_closes(path: Path) -> list[tuple[str, float]]:
    """(date, close) rows, oldest first, from a YFin-style CSV (Date, ..., Close, ...)."""
    rows = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                rows.append((row["Date"][:10], float(row["Close"])))
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(rows)


def _forward(closes: list[tuple[str, float]], after: str, horizon: int):
    """Entry: the first close strictly after ``after``; exit: ``horizon`` trading days later."""
    later = [(d, c) for d, c in closes if d > after]          # never a close on or before the decision date
    if not later:
        return None
    if len(later) <= horizon:
        return later[0], None
    return later[0], later[horizon]


def score(decision: Decision, closes: list[tuple[str, float]] | None, horizon: int, *,
          benchmark: list[tuple[str, float]] | None = None, hold_band: float = 0.02) -> Score:
    if decision.rating == RATING_REVIEW or decision.rating not in DIRECTION:
        return Score(decision, horizon, "unrated")
    if not closes:
        return Score(decision, horizon, "no_prices")
    window = _forward(closes, decision.date, horizon)
    if window is None or window[1] is None:
        return Score(decision, horizon, "pending", entry_date=window[0][0] if window else None)
    (entry_date, entry), (exit_date, exit_) = window
    ret = round(exit_ / entry - 1, 6)          # rounded first: float noise must not decide a hit at the band edge
    bench = None
    if benchmark:
        b = dict(benchmark)
        if entry_date in b and exit_date in b:
            bench = b[exit_date] / b[entry_date] - 1
    direction = DIRECTION[decision.rating]
    hit = (abs(ret) <= hold_band) if direction == 0 else (ret > 0) == (direction > 0)
    return Score(decision, horizon, "scored", entry_date, exit_date, ret,
                 None if bench is None else round(bench, 6), None if bench is None else round(ret - bench, 6),
                 hit, round(direction * ret, 6))


@dataclass
class Report:
    horizons: list[int]
    decisions: int
    accuracy: dict = field(default_factory=dict)
    consistency: dict = field(default_factory=dict)
    scores: list[dict] = field(default_factory=list)
    disclaimer: str = DISCLAIMER


def consistency(decisions: list[Decision]) -> dict:
    """Agreement among repeated runs of the same (ticker, date), independent of whether they were right."""
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for d in decisions:
        groups[(d.ticker, d.date)].append(d.rating)
    repeated = {k: v for k, v in groups.items() if len(v) > 1}
    per_cell = {f"{t} {day}": {"runs": len(v), "ratings": dict(Counter(v)),
                               "agreement": round(Counter(v).most_common(1)[0][1] / len(v), 3)}
                for (t, day), v in sorted(repeated.items())}
    return {"cells_with_repeats": len(repeated), "per_cell": per_cell,
            "mean_agreement": round(statistics.mean(c["agreement"] for c in per_cell.values()), 3) if per_cell else None}


def evaluate(decisions: list[Decision], prices: dict[str, list[tuple[str, float]]], horizons=(1, 5, 10), *,
             benchmark: list[tuple[str, float]] | None = None, hold_band: float = 0.02) -> Report:
    report = Report(list(horizons), len(decisions), consistency=consistency(decisions))
    for h in horizons:
        scores = [score(d, prices.get(d.ticker), h, benchmark=benchmark, hold_band=hold_band) for d in decisions]
        done = [s for s in scores if s.status == "scored"]
        by_rating: dict[str, list[float]] = defaultdict(list)
        for s in done:
            by_rating[s.decision.rating].append(s.forward_return)
        report.accuracy[str(h)] = {
            "scored": len(done),
            "not_scored": dict(Counter(s.status for s in scores if s.status != "scored")),
            "hit_rate": round(sum(s.hit for s in done) / len(done), 3) if done else None,
            "mean_signed_return": round(statistics.mean(s.signed_return for s in done), 6) if done else None,
            "mean_return_by_rating": {r: round(statistics.mean(v), 6) for r, v in sorted(by_rating.items())},
            "mean_alpha": (round(statistics.mean(s.alpha for s in done if s.alpha is not None), 6)
                           if any(s.alpha is not None for s in done) else None),
        }
        report.scores.extend({**asdict(s), "decision": asdict(s.decision)} for s in scores)
    return report


def prices_from_dir(directory: Path, tickers: set[str]) -> dict[str, list[tuple[str, float]]]:
    out = {}
    for ticker in tickers:
        path = Path(directory) / f"{ticker}-YFin-data.csv"
        if path.is_file():
            out[ticker] = load_closes(path)
    return out
