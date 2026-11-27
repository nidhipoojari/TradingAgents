"""Link the live CLI run's E3 summary into its run record, once the CLI has actually written that summary.

The CLI process for run 20261127-113820-f97deed2 started at 11:35:48 with the code from before the fix. It
completed the analysis at 11:38:45 and then waited at its report prompts, where it saves E3. So its record was
written without `e3_summary_path`, and that process will not add it.

This script changes nothing until the real summary exists. It then:
- copies the original record, byte for byte, to <record>.orig-before-e3-link (never overwritten);
- adds only `e3_summary_path` (absolute) and `usage.e3_cost`, derived by service.e3_cost from that summary, through
  the same service.attach_e3_summary the fixed CLI now calls. No token, call or timing figure is changed or invented;
- prints which keys changed.

    .venv/bin/python results/phase3_final_eval_20261127/harness/repair_cli_record.py [--apply]
Without --apply it only reports what it would do.
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
RUN = "20261127-113820-f97deed2"
RESULTS = Path.home() / ".tradingagents" / "logs"
RECORD = RESULTS / "runs" / f"{RUN}.json"
# The CLI ran from the project root, so E3 saves under its relative default folder there.
SUMMARY = next(iter(sorted((ROOT / "e3_results").glob(f"*_{RUN}_e3.json"))),
               ROOT / "e3_results" / f"NVDA_2026-10-23_{RUN}_e3.json")


def main() -> int:
    from tradingagents import service

    apply = "--apply" in sys.argv
    record = json.loads(RECORD.read_text())
    if record.get("e3_summary_path"):
        print(f"already linked: {record['e3_summary_path']}")
        return 0
    if not SUMMARY.exists():
        print(f"not yet: {SUMMARY} does not exist. Finish (or Ctrl-C) the CLI's report prompts first; "
              "nothing was changed.")
        return 2
    summary = json.loads(SUMMARY.read_text())
    if summary.get("run_id") != RUN or summary.get("source") != "cli":
        print(f"refused: {SUMMARY.name} names run {summary.get('run_id')!r} / source {summary.get('source')!r}")
        return 1
    print(f"summary found: {SUMMARY} (calls {summary.get('observed_llm_calls')}, errors "
          f"{summary.get('observed_llm_errors')}); record calls {record['usage'].get('llm_calls')}")
    if not apply:
        print("dry run: would add e3_summary_path and usage.e3_cost; rerun with --apply")
        return 0
    backup = RECORD.with_name(RECORD.name + ".orig-before-e3-link")
    if not backup.exists():
        shutil.copy2(RECORD, backup)
    request = service.AnalysisRequest(ticker=record["ticker"], trade_date=record["trade_date"], source="cli",
                                      analysts=tuple(record["analysts"]), config={"results_dir": str(RESULTS)})
    if not service.attach_e3_summary(request, RUN, SUMMARY):
        print("attach failed; the original record is unchanged")
        return 1
    after = json.loads(RECORD.read_text())
    original = json.loads(backup.read_text())
    changed = sorted(k for k in set(original) | set(after) if original.get(k) != after.get(k))
    usage_changed = sorted(k for k in set(original["usage"]) | set(after["usage"])
                           if original["usage"].get(k) != after["usage"].get(k))
    print(f"linked. backup: {backup.name}; top-level keys changed: {changed}; usage keys changed: {usage_changed}")
    print(f"e3_cost: {after['usage'].get('e3_cost')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
