"""Compare E3's cost estimates with what the provider actually billed (Phase 3).

Providers return token counts with each response but not a charge, so E3 can only *estimate* cost (reported
tokens x the prices in E3_PRICING_JSON). The actual charge exists only in the provider's billing export (for
Gemini: Google Cloud Billing, "Export to CSV", filtered to the Generative Language API / Vertex AI service).

This module sums E3's estimates per day (UTC) and, when you supply that export, the billed cost per day, and
reports the difference. Nothing is labelled actual unless it came from the export; without one the billed
column is None and the status says "not verified". Billing exports lag by up to a day or two and round small
amounts, so expect differences on recent or tiny days.

    python scripts/reconcile_billing.py --e3-dir results/e3 [--billing-csv billing.csv
        --filter "Service description=Generative Language"] [--from 2026-11-01 --to 2026-11-30]
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path

DATE_COLUMNS = ("Usage start date", "usage_start_time", "Date", "date", "Day")
COST_COLUMNS = ("Cost ($)", "Cost (USD)", "cost", "Cost", "Unrounded Cost ($)")


def e3_by_day(e3_dirs: list[Path], start: date | None = None, end: date | None = None) -> dict[str, dict]:
    days: dict[str, dict] = {}
    for directory in e3_dirs:
        for path in sorted(Path(directory).rglob("*_e3.json")):
            try:
                summary = json.loads(path.read_text(encoding="utf-8"))
                day = datetime.fromisoformat(summary["created_at_utc"]).date()
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if (start and day < start) or (end and day > end):
                continue
            row = days.setdefault(day.isoformat(), {"runs": 0, "estimate_usd": 0.0, "runs_without_full_estimate": 0})
            row["runs"] += 1
            full = summary.get("estimated_cost_usd")
            partial = summary.get("estimated_cost_usd_successful_calls")
            if full is None:
                row["runs_without_full_estimate"] += 1
            row["estimate_usd"] += float(full if full is not None else (partial or 0.0))
    return days


def _column(header: list[str], wanted: str | None, candidates: tuple[str, ...], what: str) -> str:
    if wanted:
        if wanted not in header:
            raise ValueError(f"the billing CSV has no {what} column {wanted!r}; columns: {', '.join(header)}")
        return wanted
    for name in candidates:
        if name in header:
            return name
    raise ValueError(f"could not find the {what} column in the billing CSV (tried {', '.join(candidates)}); "
                     f"name it with --{what}-column. Columns: {', '.join(header)}")


def _day(text: str) -> date:
    text = text.strip()
    for parse in (lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")).date(),
                  lambda t: datetime.strptime(t[:10], "%Y-%m-%d").date(),
                  lambda t: datetime.strptime(t.split()[0], "%m/%d/%Y").date(),
                  lambda t: datetime.strptime(t.split(",")[0], "%b %d").replace(year=date.today().year).date()):
        try:
            return parse(text)
        except (ValueError, IndexError):
            continue
    raise ValueError(f"unrecognised date in the billing CSV: {text!r}")


def billed_by_day(billing_csv: Path, *, date_column: str | None = None, cost_column: str | None = None,
                  filters: dict[str, str] | None = None, start: date | None = None,
                  end: date | None = None) -> dict[str, float]:
    days: dict[str, float] = {}
    with Path(billing_csv).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        when = _column(header, date_column, DATE_COLUMNS, "date")
        cost = _column(header, cost_column, COST_COLUMNS, "cost")
        for name in (filters or {}):
            _column(header, name, (), "filter")
        for row in reader:
            if any(value.lower() not in (row.get(name) or "").lower() for name, value in (filters or {}).items()):
                continue
            day = _day(row[when])
            if (start and day < start) or (end and day > end):
                continue
            amount = float((row[cost] or "0").replace("$", "").replace(",", "").strip() or 0)
            days[day.isoformat()] = days.get(day.isoformat(), 0.0) + amount
    return days


def reconcile(e3_dirs: list[Path], billing_csv: Path | None = None, **kwargs) -> dict:
    start, end = kwargs.pop("start", None), kwargs.pop("end", None)
    estimates = e3_by_day(e3_dirs, start, end)
    billed = billed_by_day(billing_csv, start=start, end=end, **kwargs) if billing_csv else None
    rows = []
    for day in sorted(set(estimates) | set(billed or {})):
        e = estimates.get(day, {"runs": 0, "estimate_usd": 0.0, "runs_without_full_estimate": 0})
        row = {"day": day, "runs": e["runs"], "e3_estimate_usd": round(e["estimate_usd"], 6),
               "runs_without_full_estimate": e["runs_without_full_estimate"]}
        if billed is not None:
            row["billed_usd"] = round(billed.get(day, 0.0), 6)
            row["difference_usd"] = round(row["billed_usd"] - row["e3_estimate_usd"], 6)
        rows.append(row)
    total_estimate = round(sum(r["e3_estimate_usd"] for r in rows), 6)
    report = {
        "days": rows,
        "e3_estimate_usd": total_estimate,
        "e3_estimate_basis": "provider-reported tokens x E3_PRICING_JSON prices (an estimate, not a charge)",
        "billed_usd": None,
        "billing_status": "not verified: no billing export supplied",
    }
    if billed is not None:
        report["billed_usd"] = round(sum(r["billed_usd"] for r in rows), 6)
        report["billing_status"] = (f"actual, from the billing export {Path(billing_csv).name} (as supplied; "
                                    "exports lag by up to a day or two and round small amounts)")
        report["difference_usd"] = round(report["billed_usd"] - total_estimate, 6)
        other = [r["day"] for r in rows if r["runs"] == 0 and r["billed_usd"]]
        if other:
            report["note"] = (f"{len(other)} billed day(s) with no E3 run here (other usage of the same key/project, "
                              f"or E3 records kept elsewhere), e.g. {other[0]}")
    return report
