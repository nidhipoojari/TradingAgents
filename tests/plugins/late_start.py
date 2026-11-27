"""Wait LATE_START_SECONDS of real time between collection and the first test, as a long run does before it
reaches a late test file. No patching. From results/phase3_verification_20261125/trade_rejection_flake/."""

import time

from tests.plugins._settings import seconds


def pytest_collection_finish(session):
    time.sleep(seconds("LATE_START_SECONDS"))
