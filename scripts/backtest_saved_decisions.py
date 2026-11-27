#!/usr/bin/env python3
"""Score saved decisions against later prices, offline: no model calls, no orders, no downloads.

    python scripts/backtest_saved_decisions.py --decisions eval/ consistency_evaluation/ \\
        --prices-dir ~/.tradingagents/cache [--benchmark-csv SPY-YFin-data.csv] [--horizons 1,5,10] [--out report.json]

See tradingagents/offline_backtest.py for the method (entry at the first close after the decision date,
no look-ahead) and its limits.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradingagents.offline_backtest import evaluate, load_closes, load_decisions, prices_from_dir  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--decisions", nargs="+", required=True, help="E3 summaries, or folders holding them")
    parser.add_argument("--prices-dir", required=True, help="folder with <TICKER>-YFin-data.csv files")
    parser.add_argument("--benchmark-csv", help="a benchmark's YFin-style CSV (e.g. SPY), for alpha")
    parser.add_argument("--horizons", default="1,5,10", help="trading days, comma-separated")
    parser.add_argument("--hold-band", type=float, default=0.02)
    parser.add_argument("--out", help="write the full report (every score) here as JSON")
    args = parser.parse_args(argv)
    decisions = load_decisions([Path(p).expanduser() for p in args.decisions])
    prices = prices_from_dir(Path(args.prices_dir).expanduser(), {d.ticker for d in decisions})
    benchmark = load_closes(Path(args.benchmark_csv).expanduser()) if args.benchmark_csv else None
    report = evaluate(decisions, prices, [int(h) for h in args.horizons.split(",")], benchmark=benchmark,
                      hold_band=args.hold_band)
    full = asdict(report)
    if args.out:
        Path(args.out).write_text(json.dumps(full, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in full.items() if k != "scores"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
