"""Scratch pytest plugin: after collection, move the wall clock seen by identity and order_policy forward by
CLOCK_JUMP_SECONDS, as the 2026-11-25 09:20:34 WSL clock step (+185.7 s) did during a full run."""
import os
from datetime import timedelta

def pytest_collection_finish(session):
    from tradingagents.execution import identity, order_policy
    jump = timedelta(seconds=float(os.environ["CLOCK_JUMP_SECONDS"]))
    for module in (identity, order_policy):
        real = module._now
        module._now = lambda real=real: real() + jump
