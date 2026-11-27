#!/bin/bash
# Phase 2 vs committed Phase 3 vs current Phase 3, identical replayed inputs. Sequential (no CPU contention).
S=/tmp/claude-1000/-home-sbhat-Projects-TradingAgents-cli-startup/45e62ebc-d5b3-46f2-84f1-842656ef60ba/scratchpad; P=/home/sbhat/Projects/TradingAgents-cli-startup; PY=$P/.venv/bin/python; OUT=$S/phase_matrix.jsonl
rm -f $OUT
run() { timeout 1500 $PY $S/phase_ab.py "$@" $OUT 2>&1 | grep -E "^ROW|Traceback|Error:" ; }
# framework overhead (zero model and data time), 30 runs each
run $S/p2tree phase2 graph $S/zero.json 30 cold on
run $S/p2tree phase2 graph $S/zero.json 30 cold off
run $P current graph $S/zero.json 30 cold on
run $P current graph $S/zero.json 30 cold off
run $S/ab_base committed service $S/zero.json 30 cold on
run $P current service $S/zero.json 30 cold on
run $S/ab_base committed worker $S/zero.json 30 cold on
run $P current worker $S/zero.json 30 cold on
# realistic: the Phase 2 baseline runs' own latencies
for prof in "p2_01_NVDA cold" "p2_03_NVDA warm"; do set -- $prof
  run $S/p2tree phase2 graph $S/$1.json 5 $2 on
  run $P current graph $S/$1.json 5 $2 on
  run $P current service $S/$1.json 5 $2 on
  run $S/ab_base committed worker $S/$1.json 5 $2 on
  run $P current worker $S/$1.json 5 $2 on
done
echo "MATRIX DONE"
