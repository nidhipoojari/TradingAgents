"""Opt-in pytest plugins for clock and timing investigations (never loaded by default).

Load one with ``-p tests.plugins.<name>`` from the repository root, e.g.:

    CLOCK_STEP_SECONDS=185.7 CLOCK_STEP_AFTER=0.3 python -m pytest -p tests.plugins.clock_step tests/test_deadlines_and_limits.py
    CLOCK_JUMP_SECONDS=396 python -m pytest -p tests.plugins.clock_jump tests/test_trade_rejection.py
    LATE_START_SECONDS=310 python -m pytest -p tests.plugins.late_start tests/test_trade_rejection.py
    TIMELINE_LOG=timeline.jsonl python -m pytest -p tests.plugins.timeline

- clock_step: a wall-clock step during each test (time.time and the identity/order clocks move; the monotonic
  clock does not), as WSL2's +185.7 s step on 2026-11-25. Parent process only: workers see the real clock.
- clock_jump: the identity/order clocks move forward once, after collection.
- late_start: real time passes between collection and the first test (no patching).
- timeline: each test's start time and outcome, as JSON lines.

The copies used for the 2026-11-25 evidence are kept unchanged under results/phase3_verification_20261125/.
None of these files is named test_*.py, so pytest never collects them.
"""
