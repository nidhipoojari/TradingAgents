#!/usr/bin/env python3
"""Audit TradingAgents E1/E2/E3 JSON runs without altering experiment data.

Usage:
    python tradingagents_benchmark_audit.py --root consistency_evaluation

Outputs CSV files in ./benchmark_audit_output, and a concise terminal summary.
Never assumes a directory named 'live' proves an exact cache-off baseline.
"""
import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path


def number(value):
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def model_names(calls):
    return ', '.join(sorted({str(c.get('model')) for c in calls if isinstance(c, dict) and c.get('model')}))


def get_token(data, calls, field):
    # The known E3 format reports usage in per_call.
    values = [number(c.get(field)) for c in calls if isinstance(c, dict)]
    if values and any(x is not None for x in values):
        return int(sum(x for x in values if x is not None))
    for key in (field, f'total_{field}'):
        n = number(data.get(key))
        if n is not None:
            return int(n)
    return None


def classify(path, root):
    parts = path.relative_to(root).parts
    low = [s.lower() for s in parts]
    phase = next((p.upper() for p in low if p in {'e1', 'e2', 'e3'}), 'unknown')
    # Use path labels as labels only. Never treat them as validated experimental configurations.
    dataset = low[0] if low else 'unknown'
    run = next((p for p in low if p.startswith('run_')), 'unknown')
    return dataset, run, phase


def write_csv(path, rows, fields):
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def fmt(v, precision=3):
    return 'N/A' if v is None else f'{v:,.{precision}f}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='consistency_evaluation')
    parser.add_argument('--out', default='benchmark_audit_output')
    args = parser.parse_args()
    root = Path(args.root)
    if not root.is_dir():
        parser.error(f'Cannot find directory: {root}')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    rows, skipped = [], []
    for path in sorted(root.rglob('*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            skipped.append((str(path), f'unreadable: {exc}'))
            continue
        if not isinstance(data, dict):
            skipped.append((str(path), 'not a JSON object'))
            continue
        wall = number(data.get('wall_time_seconds'))
        calls = data.get('per_call')
        if wall is None or not isinstance(calls, list):
            # Ignore unrelated JSON rather than misclassify it as a benchmark.
            continue
        dataset, run, phase = classify(path, root)
        latencies = [number(c.get('latency_ms')) for c in calls if isinstance(c, dict)]
        latency_sum = sum(x for x in latencies if x is not None) / 1000 if any(x is not None for x in latencies) else None
        it = get_token(data, calls, 'input_tokens')
        ot = get_token(data, calls, 'output_tokens')
        cost = number(data.get('estimated_cost_usd'))
        rows.append({
            'path': str(path), 'dataset_label': dataset, 'run_label': run, 'phase': phase,
            'ticker': data.get('ticker', ''), 'analysis_date': data.get('analysis_date', ''),
            'status': data.get('status', ''), 'wall_seconds': wall,
            'summed_llm_seconds': latency_sum,
            'wall_minus_summed_llm_seconds': wall - latency_sum if latency_sum is not None else None,
            'input_tokens': it, 'output_tokens': ot,
            'total_tokens': it + ot if it is not None and ot is not None else None,
            'estimated_cost_usd': cost, 'llm_calls': len(calls),
            'models': model_names(calls),
            'observed_llm_errors': data.get('observed_llm_errors', ''),
            'cost_complete': data.get('cost_complete', ''),
            'decision_rating': str(data.get('decision', '')).splitlines()[0][:100],
        })

    fields = ['path', 'dataset_label', 'run_label', 'phase', 'ticker', 'analysis_date',
              'status', 'wall_seconds', 'summed_llm_seconds', 'wall_minus_summed_llm_seconds',
              'input_tokens', 'output_tokens', 'total_tokens', 'estimated_cost_usd',
              'llm_calls', 'models', 'observed_llm_errors', 'cost_complete', 'decision_rating']
    write_csv(out / 'all_runs.csv', rows, fields)
    groups = defaultdict(list)
    for r in rows:
        groups[(r['dataset_label'], r['phase'], r['ticker'], r['analysis_date'])].append(r)
    aggregated = []
    for (dataset, phase, ticker, date), items in sorted(groups.items()):
        a = {'dataset_label': dataset, 'phase': phase, 'ticker': ticker,
             'analysis_date': date, 'run_count': len(items)}
        for column in ['wall_seconds', 'summed_llm_seconds', 'wall_minus_summed_llm_seconds',
                       'input_tokens', 'output_tokens', 'total_tokens', 'estimated_cost_usd', 'llm_calls']:
            vals = [r[column] for r in items if r[column] is not None]
            a['mean_' + column] = statistics.mean(vals) if vals else None
            a['median_' + column] = statistics.median(vals) if vals else None
        a['unique_models'] = ' | '.join(sorted({r['models'] for r in items if r['models']}))
        a['ratings'] = ' | '.join(sorted({r['decision_rating'] for r in items if r['decision_rating']}))
        aggregated.append(a)
    agg_fields = ['dataset_label', 'phase', 'ticker', 'analysis_date', 'run_count'] + [
        p + c for c in ['wall_seconds', 'summed_llm_seconds', 'wall_minus_summed_llm_seconds',
                        'input_tokens', 'output_tokens', 'total_tokens', 'estimated_cost_usd', 'llm_calls']
        for p in ('mean_', 'median_')
    ] + ['unique_models', 'ratings']
    write_csv(out / 'group_summary.csv', aggregated, agg_fields)

    print(f'Parsed {len(rows)} run summaries from {root}; skipped {len(skipped)} unreadable files.')
    print(f'Wrote {out / "all_runs.csv"} and {out / "group_summary.csv"}')
    print('\nGROUP AVERAGES (directory labels are NOT proof of controlled configuration):')
    for a in aggregated:
        print(f"{a['dataset_label']:18} {a['phase']:8} {a['ticker']:7} {a['analysis_date']:12} "
              f"n={a['run_count']:2} wall={fmt(a['mean_wall_seconds'])}s "
              f"llm={fmt(a['mean_summed_llm_seconds'])}s "
              f"tokens={fmt(a['mean_total_tokens'], 0)} "
              f"cost=${fmt(a['mean_estimated_cost_usd'], 5)}")
    print('\nINTERPRETATION WARNINGS:')
    print('- A dataset directory named live is not evidence that only caching changed.')
    print('- wall minus summed LLM latency is not reliably non-LLM time if calls overlap.')
    print('- E1/E2/E3 labels might represent stages rather than isolated A/B treatments.')
    print('- Token or time savings require a matched original-vs-optimized configuration.')
    print('- Small sample sizes and differing decisions can affect token counts and timings.')
    if not rows:
        print('No files matched the known E3 format; inspect the JSON schema for other experiments.')


if __name__ == '__main__':
    main()
