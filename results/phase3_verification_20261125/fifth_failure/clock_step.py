"""Scratch pytest plugin: a wall-clock step of CLOCK_STEP_SECONDS at the start of every test's call phase
(after its fixtures), as WSL2's +185.7 s step on 2026-11-25. time.time() and the identity/order clocks move;
the monotonic clock does not. Restored after each test. Parent process only (workers see the real clock)."""
import os, time, pytest
from datetime import timedelta

STEP = float(os.environ["CLOCK_STEP_SECONDS"])
AFTER = float(os.environ.get("CLOCK_STEP_AFTER", "0"))   # seconds into the test before the step lands
_real_time = time.time

def pytest_report_header(config):
    import tradingagents
    return f"clock_step: {STEP:+g} s, {AFTER:g} s into each test; tradingagents imported from {os.path.dirname(tradingagents.__file__)}"

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    from tradingagents.execution import identity, order_policy
    import threading
    saved = {m: m._now for m in (identity, order_policy)}
    state = {"offset": 0.0}
    time.time = lambda: _real_time() + state["offset"]
    for m, real in saved.items():
        m._now = (lambda real=real: real() + timedelta(seconds=state["offset"]))
    def step():
        state["offset"] = STEP
    timer = threading.Timer(AFTER, step) if AFTER > 0 else None
    if timer is None:
        step()
    else:
        timer.start()
    try:
        yield
    finally:
        if timer is not None:
            timer.cancel()
        time.time = _real_time
        for m, real in saved.items():
            m._now = real
