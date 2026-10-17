"""Horizon wipe: the Art of Intelligence join between two shots (contract v1.3 module).

From the "Transition system" board of the Art of Intelligence canvas: "The workhorse. The 4px
band rises up the frame; the new shot is already under it. Use it for every cut between two
sources." 18 frames at 30 fps (0.6 s), on the series' one easing curve,
cubic-bezier(0.6, 0, 0.4, 1): the band sweeps up for the first 8/9 of the join and clears the
top in the last ninth. Below the band is the incoming shot, above it the outgoing one, so the
new picture rises into the frame along the horizon.

Name it in a plan or a style as
`{"kind": "module", "module": "render/cliprender/themes/horizon_wipe.py", "seconds": 0.6}`.
Rendering needs numpy (`uv run --project render --extra styles cliprender ...`).
"""

import numpy as np

NAME = "horizon-wipe"
TITLE = "Horizon wipe"
DESCRIPTION = (
    "The Art of Intelligence cut: a glowing 4 px band rises up the frame over the new shot."
)
SECONDS = 0.6
# The band: orange, amber at 45 %, sun, left to right; its glow is amber.
STOPS = ((0.0, (232, 89, 12)), (0.45, (245, 166, 35)), (1.0, (247, 183, 51)))
GLOW = (245, 166, 35)
# Share of the join the band takes to cross the frame; the rest it fades at the top.
SWEEP = 0.889
# Share of the join over which the band fades in at the bottom edge, so frame 0 is the
# outgoing shot untouched.
ARRIVE = 0.06
GLOW_ALPHA = 0.5


def ease(t):
    """cubic-bezier(0.6, 0, 0.4, 1) at time `t` (0..1), solved for x by bisection."""
    low, high = 0.0, 1.0
    for _ in range(40):
        u = (low + high) / 2
        x = 3 * (1 - u) ** 2 * u * 0.6 + 3 * (1 - u) * u**2 * 0.4 + u**3
        if x < t:
            low = u
        else:
            high = u
    u = (low + high) / 2
    return 3 * (1 - u) * u**2 + u**3


def prepare(state, width, height):
    scale = height / 1080
    core = max(2, round(3 * scale))
    sigma = max(2.0, 9 * scale)
    position = np.linspace(0.0, 1.0, width)
    band = np.stack(
        [np.interp(position, [s for s, _ in STOPS], [c[i] for _, c in STOPS]) for i in range(3)],
        axis=-1,
    ).astype(np.float32)
    reach = int(np.ceil(3 * sigma))
    offsets = np.arange(-reach, reach + core)
    # Distance from the band's core rows: 0 inside it, growing outside.
    distance = np.where(offsets < 0, -offsets, np.maximum(0, offsets - core + 1))
    glow = np.exp(-0.5 * (distance / sigma) ** 2) * GLOW_ALPHA
    glow[(offsets >= 0) & (offsets < core)] = 1.0
    is_core = (offsets >= 0) & (offsets < core)
    state.update(core=core, band=band, offsets=offsets, weights=glow, is_core=is_core)


def render(a, b, t, state):
    if t <= 0:
        return a
    if t >= 1:
        return b
    height, width = a.shape[:2]
    if "band" not in state:
        prepare(state, width, height)
    progress = ease(min(1.0, t / SWEEP))
    # Rows above `boundary` still show the outgoing shot; from it down, the incoming one.
    boundary = round(height * (1 - progress))
    out = b.copy()
    out[:boundary] = a[:boundary]
    alpha = min(1.0, t / ARRIVE)
    if t > SWEEP:
        alpha *= max(0.0, (1 - t) / (1 - SWEEP))
    if alpha <= 0:
        return out
    core = state["core"]
    rows = boundary - core + state["offsets"]
    keep = (rows >= 0) & (rows < height)
    if not keep.any():
        return out
    rows = rows[keep]
    weights = (state["weights"][keep] * alpha)[:, None, None].astype(np.float32)
    colour = np.where(
        state["is_core"][keep][:, None, None], state["band"][None, :, :], np.float32(GLOW)
    )
    strip = out[rows].astype(np.float32)
    out[rows] = np.clip(strip * (1 - weights) + colour * weights, 0, 255).astype(np.uint8)
    return out
