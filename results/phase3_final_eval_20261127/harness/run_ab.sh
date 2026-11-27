#!/bin/bash
# Matched Phase 2 (aa763f7) vs current working tree, identical replayed inputs. Sequential (no CPU contention).
# Offline replay: scripted models replay the per-call latencies of the Phase 2 live runs 01_NVDA and 03_NVDA.
P=/home/sbhat/Projects/TradingAgents-cli-startup; H=$P/results/phase3_final_eval_20261127/harness
P2=$1; OUT=$P/results/phase3_final_eval_20261127/04_benchmark_ab/ab.jsonl; PY=$P/.venv/bin/python
run() { echo "== $*"; timeout 1500 $PY $H/phase_ab.py "$@" $OUT 2>&1 | grep -E "^ROW|Traceback|Error" ; }
run $P2 phase2 graph $H/zero.json 30 cold on
run $P current graph $H/zero.json 30 cold on
run $P current service $H/zero.json 30 cold on
run $P current worker $H/zero.json 30 cold on
for prof in "p2_01_NVDA cold" "p2_01_NVDA warm" "p2_03_NVDA cold" "p2_03_NVDA warm"; do set -- $prof
  run $P2 phase2 graph $H/$1.json 5 $2 on
  run $P current graph $H/$1.json 5 $2 on
  run $P current service $H/$1.json 5 $2 on
  run $P current worker $H/$1.json 5 $2 on
done
echo "AB DONE"
