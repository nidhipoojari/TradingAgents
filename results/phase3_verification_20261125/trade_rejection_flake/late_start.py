"""Scratch pytest plugin: wait LATE_START_SECONDS of real time between collection and the first test, as a
long full-suite run does before reaching test_trade_rejection.py. No patching."""
import os, time

def pytest_collection_finish(session):
    time.sleep(float(os.environ["LATE_START_SECONDS"]))
