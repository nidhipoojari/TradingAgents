#!/usr/bin/env python3
"""E3 -> E1: per-agent cost and latency, checked against E1's routing table.

Reads E3 run summaries (*_e3.json), averages each agent's latency, tokens and
estimated cost over the runs, and labels each agent with its tier from
tradingagents.e1_routing.ROUTING_TABLE. This answers which agents a model
change affects most:
  - a quick-tier agent with a large share is where a fast, cheap model matters;
  - the deep tier's share is what a stronger (pricier) deep model multiplies.
Failed calls (no usage reported) are excluded; calls answered by a backup
model are counted and flagged. No API calls.

    .venv/bin/python scripts/e3_agent_breakdown.py eval/phase2/0*_NVDA
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tradingagents.e1_routing import ROUTING_TABLE  # noqa: E402


def _tier(agent: str) -> str:
    return ROUTING_TABLE.get(agent.lower().replace(" ", "_"), "unrouted")


def _summaries(paths: list[Path]) -> list[dict]:
    files = []
    for path in paths:
        files += sorted(path.rglob("*_e3.json")) if path.is_dir() else [path]
    if not files:
        raise SystemExit("No *_e3.json files found.")
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def breakdown(summaries: list[dict], pricing: dict) -> dict:
    primary = statistics.mode(c["model"] for s in summaries for c in s["per_call"] if not c["had_error"])
    per_run = []
    backup_calls = 0
    for summary in summaries:
        agents: dict[str, dict] = defaultdict(lambda: {"latency_s": 0.0, "input_tokens": 0,
                                                       "output_tokens": 0, "cost_usd": 0.0})
        for call in summary["per_call"]:
            if call["had_error"] or call["input_tokens"] is None or call["output_tokens"] is None:
                continue
            price = pricing[call["model"]]
            backup_calls += call["model"] != primary
            row = agents[call["agent"]]
            row["latency_s"] += call["latency_ms"] / 1000
            row["input_tokens"] += call["input_tokens"]
            row["output_tokens"] += call["output_tokens"]
            row["cost_usd"] += (call["input_tokens"] * price["input_per_million_usd"]
                                + call["output_tokens"] * price["output_per_million_usd"]) / 1e6
        per_run.append(agents)

    names = sorted({name for run in per_run for name in run})
    mean = {name: {k: statistics.mean(run[name][k] for run in per_run if name in run)
                   for k in ("latency_s", "input_tokens", "output_tokens", "cost_usd")} for name in names}
    total = {k: sum(row[k] for row in mean.values()) for k in ("latency_s", "cost_usd")}
    rows = [{"agent": name, "tier": _tier(name), **{k: round(v, 6) for k, v in mean[name].items()},
             "latency_share_pct": round(100 * mean[name]["latency_s"] / total["latency_s"], 1),
             "cost_share_pct": round(100 * mean[name]["cost_usd"] / total["cost_usd"], 1)}
            for name in names]
    rows.sort(key=lambda r: -r["latency_s"])
    tiers = {tier: {"agents": [r["agent"] for r in rows if r["tier"] == tier],
                    "latency_share_pct": round(sum(r["latency_share_pct"] for r in rows if r["tier"] == tier), 1),
                    "cost_share_pct": round(sum(r["cost_share_pct"] for r in rows if r["tier"] == tier), 1)}
             for tier in sorted({r["tier"] for r in rows})}
    top = rows[0]
    deep_share = tiers.get("deep", {}).get("cost_share_pct", 0.0)
    return {
        "runs": len(summaries), "primary_model": primary, "backup_model_calls": backup_calls,
        "agents": rows, "tiers": tiers,
        "verdict": {
            "largest_agent": top["agent"], "largest_agent_tier": top["tier"],
            "largest_agent_on_quick_tier": top["tier"] == "quick",
            "deep_tier_cost_share_pct": deep_share,
            "deep_model_k_times_price_raises_run_cost_by_pct": f"{deep_share:.1f} x (k - 1)",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", type=Path, nargs="+", help="run directories or *_e3.json files")
    parser.add_argument("--pricing", type=Path, default=ROOT / "e3_pricing.json")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args()
    result = breakdown(_summaries(args.paths), json.loads(args.pricing.read_text(encoding="utf-8")))
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"{result['runs']} runs, primary model {result['primary_model']}, "
          f"{result['backup_model_calls']} calls answered by a backup model\n")
    print("| Agent | E1 tier | Latency (s) | Latency share | Output tokens | Cost (USD) | Cost share |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for r in result["agents"]:
        print(f"| {r['agent']} | {r['tier']} | {r['latency_s']:.2f} | {r['latency_share_pct']}% | "
              f"{r['output_tokens']:.0f} | {r['cost_usd']:.5f} | {r['cost_share_pct']}% |")
    for tier, t in result["tiers"].items():
        print(f"| **{tier} tier ({len(t['agents'])} agents)** | | | **{t['latency_share_pct']}%** | | | "
              f"**{t['cost_share_pct']}%** |")
    v = result["verdict"]
    print(f"\nLargest agent: {v['largest_agent']} ({v['largest_agent_tier']} tier)."
          f" Deep tier = {v['deep_tier_cost_share_pct']}% of cost: a deep model k times the price"
          f" raises run cost by {v['deep_model_k_times_price_raises_run_cost_by_pct']}%.")


if __name__ == "__main__":
    main()
