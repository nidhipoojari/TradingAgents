#!/usr/bin/env python3
"""Compare E3's cost estimates per day with a provider billing export (offline; no API calls).

    python scripts/reconcile_billing.py --e3-dir results/e3 \\
        [--billing-csv billing.csv --filter "Service description=Generative Language"] \\
        [--from 2026-11-01] [--to 2026-11-30]

Without --billing-csv it reports E3's estimates only, marked "not verified". See
tradingagents/billing_reconcile.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradingagents.billing_reconcile import reconcile  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--e3-dir", action="append", required=True, help="folder of *_e3.json summaries (repeatable)")
    parser.add_argument("--billing-csv", help="the provider's billing export (CSV)")
    parser.add_argument("--date-column")
    parser.add_argument("--cost-column")
    parser.add_argument("--filter", action="append", default=[], metavar="COLUMN=TEXT",
                        help="keep billing rows whose COLUMN contains TEXT (repeatable)")
    parser.add_argument("--from", dest="start", type=date.fromisoformat)
    parser.add_argument("--to", dest="end", type=date.fromisoformat)
    args = parser.parse_args(argv)
    filters = dict(item.split("=", 1) for item in args.filter)
    extra = {"date_column": args.date_column, "cost_column": args.cost_column, "filters": filters} \
        if args.billing_csv else {}
    report = reconcile([Path(d) for d in args.e3_dir], Path(args.billing_csv) if args.billing_csv else None,
                       start=args.start, end=args.end, **extra)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
