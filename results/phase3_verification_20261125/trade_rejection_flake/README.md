# The 2026-11-25 full-suite run with 5 failures: investigation (offline)

## What was seen
A full `pytest` run, 09:17:10-09:20:57 EST, reported `5 failed, 1952 passed`. Only the last three lines of
its output were captured, so only two of the five failing tests are known:

- `tests/test_trade_rejection.py::test_each_kind_of_failure_is_recorded_with_its_category`
- `tests/test_trade_rejection.py::test_an_alpaca_refusal_is_recorded_in_both_logs_without_secrets`

The other three names were never printed. That run's temporary folder has been rotated away, so they cannot
be recovered. (An earlier message said all five were in `test_trade_rejection.py`. That was an assumption, not
an observation.)

## Root cause (verified)
1. **A system clock step.** The systemd journal shows `systemd-resolved: Clock change detected` at 09:20:34,
   inside that run. Comparing each journal entry's realtime and monotonic timestamps, the wall clock jumped
   **185.7 s forward**. That was the only step between 09:00 and 09:35. This machine is WSL2, whose clock is
   re-synchronised in steps. The run began right after a 4-minute, 42-process CPU-load test.
2. **A test defect that made the tests depend on it.** Five test files used one signed-in identity, `TESTER`,
   created once at import (collection) time. A password confirmation is valid for 5 minutes + 5 s of
   **wall-clock** time (`identity.STEP_UP_SECONDS`, `confirmed_recently`).
   - `test_trade_rejection.py` runs about 210 s after collection; with the 185.7 s step that is about 396 s, so
     every submit was refused with "Re-enter your password".
   - The same failure is deterministic in any run that reaches these tests more than 305 s after collection
     (a slower machine, or a longer suite).
   - `test_limit_orders_and_identity.py` also bound `TESTER` as a default argument value (evaluated once, when
     the function is defined), which a per-test refresh alone would not have fixed.

The product behaviour is correct and fails safe: a forward clock step makes a confirmation look older, so
orders are refused, never accepted.

## Reproduction (before the fix)
| Log | Condition | Result |
| --- | --- | --- |
| `A_clock_jump_396s_before_fix.log` | `clock_jump.py`: identity/order clocks +396 s after collection | **4 failed, 4 passed**: tests 3-6 fail, all "Re-enter your password"; 1, 2, 7, 8 pass. The last two FAILED lines are exactly the two names seen. |
| `A_control_no_jump_before_fix.log` | same plugin, +0 s | 8 passed |
| `B_real_310s_wait_before_fix.log` | `late_start.py`: 310 s of real time between collection and the tests, no patching | see "Real-time runs" below |
| `C_cpu_load_42_procs_no_jump.log` | 42 busy processes on 14 cores, no clock change | 8 passed x 3: CPU load alone does not cause it |

The four failures account for four of the five. **The fifth is unidentified.** It ran before these tests
(FAILED lines print in run order). Candidates are tests that read the wall clock around 09:20:34, such as
`test_sessions.py` (session timeouts) or `test_social_lookahead.py` (date windows), but the evidence does not
show which.

## Fix (test code only)
- An autouse fixture `_fresh_sign_in` in `test_trade_rejection.py`, `test_dashboard_logic.py`,
  `test_alpaca_paper.py`, `test_order_policy.py` and `test_limit_orders_and_identity.py` issues `TESTER` fresh
  for every test. It is set directly, not with `monkeypatch`, so a test's `monkeypatch.undo()` cannot restore a
  stale identity.
- In `test_limit_orders_and_identity.py`, `_approve(identity=TESTER)` became a sentinel resolved at call time;
  `identity=None` still means no sign-in.
- An AST scan of `tests/` found no other default argument capturing `TESTER`.

## Verification (after the fix)
| Log | Condition | Result |
| --- | --- | --- |
| `A_clock_jump_396s_after_fix.log` | +396 s, `test_trade_rejection.py` | 8 passed |
| `A_clock_jump_396s_after_fix_all_5_files.log` | +396 s, all five files, before the `_approve` default fix | 8 failed (all in `_approve` callers): found the second defect |
| `A_clock_jump_396s_after_fix_all_5_files_v2.log` | +396 s, all five files | 153 passed |
| `B_real_310s_wait_after_fix_all_5_files.log` | 310 s real wait, all five files | see "Real-time runs" below |

Other hypotheses checked:
- **CPU load:** C above.
- **File locks:** every decision log and ledger is under the test's own `tmp_path`.
- **Leftover processes:** none running.
- **Streamlit session state:** the page test signs in afresh and passed in the reproduction.
- **Test order:** order matters only through elapsed wall-clock time; the full suite passed in three other
  runs.

The plugins used are `clock_jump.py` and `late_start.py` (scratch, kept here as evidence).
