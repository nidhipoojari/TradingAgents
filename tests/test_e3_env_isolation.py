"""The suite never inherits a contributor's E3 settings.

With E3_TRACE_ENABLED=1 in the shell, a CLI test would otherwise trace to
Langfuse and save a summary into that contributor's E3_RESULTS_DIR, which can
be an experiment folder.
"""

import os


def test_e3_settings_blanked_for_the_suite():
    for name, value in os.environ.items():
        if name.startswith("E3_"):
            assert value == "", name
