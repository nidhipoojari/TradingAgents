"""Scratch plugin: log each test's start, as seconds since the session started and its run order."""
import json, os, time
T0 = time.monotonic()
_log = open(os.environ["TIMELINE_LOG"], "w")
_n = [0]

def pytest_runtest_logstart(nodeid, location):
    _n[0] += 1
    _log.write(json.dumps({"i": _n[0], "t": round(time.monotonic() - T0, 2), "test": nodeid}) + "\n"); _log.flush()

def pytest_runtest_logreport(report):
    if report.when == "call" or report.outcome != "passed":
        _log.write(json.dumps({"i": _n[0], "t": round(time.monotonic() - T0, 2), "test": report.nodeid,
                               "when": report.when, "outcome": report.outcome}) + "\n"); _log.flush()
