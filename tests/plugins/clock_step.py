"""A wall-clock step of CLOCK_STEP_SECONDS, CLOCK_STEP_AFTER seconds into every test's call phase (default 0:
at its start). time.time() and the identity/order clocks move; the monotonic clock does not. Restored after
each test. Parent process only (analysis workers see the real clock). From
results/phase3_verification_20261125/fifth_failure/clock_step.py, with settings read at session start."""

import os
import threading
import time
from datetime import timedelta

import pytest

from tests.plugins._settings import seconds

_real_time = time.time
_settings = {}


def pytest_configure(config):
    _settings["step"] = seconds("CLOCK_STEP_SECONDS")
    _settings["after"] = seconds("CLOCK_STEP_AFTER", "0")


def pytest_report_header(config):
    import tradingagents

    return (f"clock_step: {_settings['step']:+g} s, {_settings['after']:g} s into each test; "
            f"tradingagents imported from {os.path.dirname(tradingagents.__file__)}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    from tradingagents.execution import identity, order_policy

    saved = {m: m._now for m in (identity, order_policy)}
    state = {"offset": 0.0}
    time.time = lambda: _real_time() + state["offset"]
    for module, real in saved.items():
        module._now = (lambda real=real: real() + timedelta(seconds=state["offset"]))

    def step():
        state["offset"] = _settings["step"]

    timer = threading.Timer(_settings["after"], step) if _settings["after"] > 0 else None
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
        for module, real in saved.items():
            module._now = real
