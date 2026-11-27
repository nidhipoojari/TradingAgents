"""After collection, move the identity and order_policy clocks forward by CLOCK_JUMP_SECONDS (once, for the
whole session). From results/phase3_verification_20261125/trade_rejection_flake/clock_jump.py."""

from datetime import timedelta

from tests.plugins._settings import seconds


def pytest_collection_finish(session):
    from tradingagents.execution import identity, order_policy

    jump = timedelta(seconds=seconds("CLOCK_JUMP_SECONDS"))
    for module in (identity, order_policy):
        real = module._now
        module._now = lambda real=real: real() + jump
