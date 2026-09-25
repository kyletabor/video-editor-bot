"""Share the integration suite's generated media with the reel tests.

The fixtures stay defined in test_integration.py, next to the timing checks they were written
for; importing them here registers them for every module without a second copy.
"""

from test_integration import media, timing_flags  # noqa: F401
