#!/usr/bin/env bash
# Remaining Section 4 runs, for after the Gemini free-tier daily quota resets
# (midnight Pacific). About 80 requests of the 500/day. Each plan runs a
# 3-request preflight and refuses to start if gemini-3.5-flash-lite is still
# out of quota. Writes only to new eval/ folders; the final table goes to
# eval/section4_summary.txt.
set -euo pipefail
cd "$(dirname "$0")/.."
P=.venv/bin/python
S=scripts/phase2_eval.py

$P $S run phase2-rerun --out eval/phase2-rerun --pause 30
$P $S run tracing --out eval/tracing_fresh --cache-from eval/phase2 --pause 30
$P $S summarize eval/phase2 eval/phase2-rerun eval/tracing_fresh eval/forced_failure \
  | tee eval/section4_summary.txt
