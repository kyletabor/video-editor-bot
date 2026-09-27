"""Share the integration suite's generated media with the reel tests.

The fixtures stay defined in test_integration.py, next to the timing checks they were written
for; importing them here registers them for every module without a second copy. The 10 s
"talk" of the music tests is shared the same way with the bounded-assembly tests.
"""

from test_integration import media, timing_flags  # noqa: F401
from test_reel_music import talk  # noqa: F401
