"""Reference transition module for `output.reel.transition {kind: module}`: a soft wipe.

The whole contract is the `render` function below. The renderer calls it once per frame of
every join, in order, and encodes what it returns. Only numpy (and scipy, Pillow, the
standard library) may be imported; random choices must use `state["seed"]`.
"""

import numpy as np

NAME = "wipe"
TITLE = "Soft wipe"
SECONDS = 0.6  # advisory: the plan's `transition.seconds` decides the length
DESCRIPTION = "The incoming picture sweeps in from the left behind a soft edge."

SOFTNESS = 0.08  # width of the blended edge, as a fraction of the frame width


def render(a, b, t, state):
    """a, b: HxWx3 uint8 RGB arrays, the outgoing and the incoming frame at this instant.
    t: 0.0 on the first frame of the join, 1.0 on the last (0.5 if the join is one frame).
    state: a dict that lives for one join, pre-filled with `size` (W, H), `fps`, `n_frames`,
    `seed` and `frame_index`; precompute into it on the first call.
    Returns an HxWx3 uint8 RGB array."""
    if "ramp" not in state:
        width = state["size"][0]
        state["ramp"] = np.linspace(0.0, 1.0, width, dtype=np.float32)
    # The edge travels from just left of the frame to just right of it, so t=0 is all `a`
    # and t=1 is all `b`.
    edge = t * (1 + 2 * SOFTNESS) - SOFTNESS
    alpha = np.clip((edge - state["ramp"]) / SOFTNESS * 0.5 + 0.5, 0.0, 1.0)[None, :, None]
    return (a * (1 - alpha) + b * alpha + 0.5).astype(np.uint8)
