"""Reference transition: a linear crossfade, the smallest file that meets the contract."""
import numpy as np

NAME = "plain-dissolve"
TITLE = "Plain dissolve"
SECONDS = 0.6
DESCRIPTION = "Linear crossfade; the reference the code-drawn transitions are compared against."


def render(a, b, t, state):
    if t <= 0:
        return a
    if t >= 1:
        return b
    return (a.astype(np.float32) * (1 - t) + b.astype(np.float32) * t + 0.5).astype(np.uint8)
