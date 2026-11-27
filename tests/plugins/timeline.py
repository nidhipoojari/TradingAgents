"""Log each test's start (seconds since the session started, run order) and outcome to TIMELINE_LOG as JSON
lines. From results/phase3_verification_20261125/fifth_failure/test_timeline.py (renamed: not a test file)."""

import json
import os
import time

_state = {"t0": time.monotonic(), "n": 0, "log": None}


def pytest_configure(config):
    path = os.environ.get("TIMELINE_LOG")
    if not path:
        raise RuntimeError("this plugin needs TIMELINE_LOG=<file> in the environment")
    _state["log"] = open(path, "w", encoding="utf-8")  # noqa: SIM115 - open for the session, closed in unconfigure


def _write(**row):
    _state["log"].write(json.dumps({"t": round(time.monotonic() - _state["t0"], 2), **row}) + "\n")
    _state["log"].flush()


def pytest_runtest_logstart(nodeid, location):
    _state["n"] += 1
    _write(i=_state["n"], test=nodeid)


def pytest_runtest_logreport(report):
    if report.when == "call" or report.outcome != "passed":
        _write(i=_state["n"], test=report.nodeid, when=report.when, outcome=report.outcome)


def pytest_unconfigure(config):
    if _state["log"] is not None:
        _state["log"].close()
