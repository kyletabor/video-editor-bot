"""Token stream: the incoming video is written onto the screen the way a language model streams text.

The frame is a grid of small token cells (48 columns, wider than tall). Many rows stream at once,
each row filling left to right with irregular, bursty token timing, and rows start in staggered
reading order so the whole frame resolves in about a second (overall progress is eased with a
smoothstep). Every cell that has just been written lights up cool white, then cyan, then electric
blue as it decays, with a bright cursor block at the head of each row, a short comet trail and a
soft bloom, so the writing front carries its own light. Behind the front is b, ahead is a, with a
faint lattice of cell outlines where the stream is about to arrive.

Speed: everything is computed per cell (tiny arrays); pixels are only touched in the cells that are
actually changing this frame. Settled cells are copied straight from a or b.
"""
import numpy as np
from scipy.ndimage import gaussian_filter, maximum_filter

NAME = "token-stream"
TITLE = "Token stream"
SECONDS = 1.0
DESCRIPTION = ("The incoming video is written onto the screen token by token in reading order, "
               "many rows streaming at once behind a luminous cyan-white cursor front.")

COLS = 48            # token columns across the frame
ASPECT = 0.56        # cell height / width
FLIP_FRAMES = 2.5    # a -> b swap per cell, in frames
DECAY_FRAMES = 0.8   # glow decay constant, in frames
FADE_START, FADE_END = 1.4, 2.8   # glow is gone FADE_END frames after a cell is written
TAIL_CELLS = 3.2     # comet trail behind each row's cursor, in cells
T0 = 0.09            # time of the first written cell (frame 0 must stay pure a)
EPS = 0.002          # glow below this is invisible after 8-bit rounding

# glow colour by "heat" (brightness is baked in): deep blue, electric blue, cyan, cool white
HEAT_X = np.array([0.0, 0.12, 0.34, 0.62, 0.86, 1.0])
HEAT_RGB = np.array([[0.00, 0.00, 0.00],
                     [0.05, 0.11, 0.40],
                     [0.26, 0.45, 0.95],     # electric blue  #4a7dff
                     [0.35, 0.82, 1.00],     # cyan           #5ad1ff
                     [0.64, 0.92, 1.00],
                     [0.84, 0.96, 1.00]])    # cool white
CURSOR_RGB = np.array([0.88, 0.97, 1.0], np.float32)
LINE_RGB = np.array([0.35, 0.82, 1.0], np.float32)


def _smooth(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _interp_idx(n_pix, cell, n_cells):
    """Linear interpolation from cell centres to pixel centres: left cell index and right weight."""
    pos = np.clip((np.arange(n_pix) + 0.5) / cell - 0.5, 0, n_cells - 1)
    i0 = np.minimum(np.floor(pos).astype(np.int64), n_cells - 2)
    return i0, (pos - i0).astype(np.float32)


def _setup(state, W, H):
    rng = np.random.default_rng(state["seed"])
    s = H / 1080.0                                   # every pixel size scales with this
    cw = W / COLS
    R = max(8, int(round(H / (cw * ASPECT))))
    ch = H / R
    n = max(2, int(state.get("n_frames", 24)))
    fr = 1.0 / (n - 1)

    # ---- per-cell write times: staggered rows, bursty tokens, eased overall progress ----
    dur = rng.uniform(0.2, 0.45, R)                  # how long each row takes to stream
    base = np.linspace(0.0, 1.0, R)
    start = np.clip(base * (1.0 - dur) + rng.normal(0.0, 0.035, R), 0.0, 1.0 - dur)
    gaps = rng.gamma(1.4, 1.0, (R, COLS))            # irregular inter-token gaps
    cum = np.cumsum(gaps, axis=1)
    cum = (cum - cum[:, :1]) / (cum[:, -1:] - cum[:, :1])
    f_lin = start[:, None] + dur[:, None] * cum
    f_lin = (f_lin - f_lin.min()) / (f_lin.max() - f_lin.min())
    xs = np.linspace(0.0, 1.0, 4097)                 # progress p(t) = smoothstep(t) -> time = p^-1
    ys = xs * xs * (3.0 - 2.0 * xs)
    t_end = 1.0 - (1.0 + max(FLIP_FRAMES, FADE_END)) * fr - 0.003   # last drawn frame (n-2) is already pure b
    F = (T0 + (t_end - T0) * np.interp(f_lin, ys, xs)).astype(np.float32)

    # ---- per-pixel geometry: owning cell, soft block / outline coverage ------------------
    px = np.arange(W) + 0.5
    py = np.arange(H) + 0.5
    cx = np.minimum((px / cw).astype(np.int32), COLS - 1)
    cy = np.minimum((py / ch).astype(np.int32), R - 1)
    dx = np.minimum(px - cx * cw, (cx + 1) * cw - px).astype(np.float32)
    dy = np.minimum(py - cy * ch, (cy + 1) * ch - py).astype(np.float32)
    d = np.minimum(dx[None, :], dy[:, None])         # distance to nearest cell edge, px
    gap = max(0.5, 1.5 * s)
    soft = max(0.6, 1.5 * s)
    cf = _smooth((d - gap) / soft)                                  # lit block: gutter + soft edge
    cl = 1.0 - _smooth(d / max(0.6, 1.1 * s))                       # hairline outline on the edges

    return {
        "size": (W, H), "R": R, "F": F, "fr": fr,
        "cf": cf.astype(np.float32)[..., None], "cl": cl.astype(np.float32)[..., None],
        "cidx": (cy[:, None] * COLS + cx[None, :]).astype(np.int32),
        "noise": rng.uniform(-0.45, 0.45, (H, W, 1)).astype(np.float32),
        "row_y": np.append(np.searchsorted(cy, np.arange(R)), H),
        "col_x": np.append(np.searchsorted(cx, np.arange(COLS)), W),
        "ix": _interp_idx(W, cw, COLS), "iy": _interp_idx(H, ch, R),
        "bloom_sigma": (1.5, 0.9),
    }


def render(a, b, t, state):
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    H, W = a.shape[:2]
    st = state.get("_ts")
    if st is None or st["size"] != (W, H):
        st = state["_ts"] = _setup(state, W, H)
    R, F, fr = st["R"], st["F"], st["fr"]

    # ---- everything per cell (R x COLS) -------------------------------------------------
    tau = (t - F) / fr                                   # frames since the cell was written
    m = _smooth(tau / FLIP_FRAMES)                       # a -> b swap over ~2.5 frames
    pos = np.clip(tau, 0.0, None)
    window = 1.0 - _smooth((pos - FADE_START) / (FADE_END - FADE_START))
    decay = np.exp(-pos / DECAY_FRAMES) * window
    count = (F <= t).sum(axis=1)                         # tokens written so far in each row
    behind = np.clip(count[:, None] - 1 - np.arange(COLS)[None, :], 0, None)  # cells behind cursor
    heat = _smooth((tau + 0.12) / 0.12) * decay * np.exp(-behind / TAIL_CELLS)
    fill = np.stack([np.interp(heat, HEAT_X, HEAT_RGB[:, c]) for c in range(3)],
                    axis=-1).astype(np.float32)
    rows = np.nonzero(count > 0)[0]
    if rows.size:                                        # cursor block at the head of each row
        head = count[rows] - 1
        ht = (t - F[rows, head]) / fr
        ac = np.exp(-ht / 0.8) * (1.0 - _smooth((ht - 1.0) / 1.5))
        fill[rows, head] = np.maximum(fill[rows, head], 0.92 * ac[:, None] * CURSOR_RGB)
    ao = 0.34 * _smooth((tau + 2.0) / 2.0) * np.exp(-pos / 1.6) * window   # outline lattice
    line = ao[..., None] * LINE_RGB
    glow = fill + 0.5 * line
    sy, sx = st["bloom_sigma"]
    blur = np.stack([gaussian_filter(glow[..., c], (sy, sx), mode="constant") for c in range(3)],
                    axis=-1)

    # cells whose pixels differ from plain a / b this frame (bloom reaches one cell further)
    lit = (fill.max(-1) > EPS) | (line.max(-1) > EPS) | (blur.max(-1) > EPS * 0.5)
    dirty = ((m > 0.0) & (m < 1.0)) | maximum_filter(lit, size=3, mode="constant")

    # ---- pixels: settled cells straight from a or b, dirty cells composited ----------------
    cidx, cf, cl, noise = st["cidx"], st["cf"], st["cl"], st["noise"]
    done = np.take((m >= 1.0).ravel(), cidx)
    out = np.where(done[..., None], b, a)
    fill_t, line_t = fill.reshape(-1, 3), line.reshape(-1, 3)
    m_t = m.astype(np.float32).ravel()
    row_y, col_x = st["row_y"], st["col_x"]
    (cx0, wx), (ry0, wy) = st["ix"], st["iy"]
    # bloom, step 1: interpolate the blurred cell glow horizontally to pixel columns, all rows
    bh = blur[:, cx0, :] * (1.0 - wx)[None, :, None] + blur[:, cx0 + 1, :] * wx[None, :, None]
    for r in np.nonzero(dirty.any(axis=1))[0]:
        cols = np.nonzero(dirty[r])[0]
        y0, y1 = row_y[r], row_y[r + 1]
        x0, x1 = col_x[cols[0]], col_x[cols[-1] + 1]
        cid = cidx[y0:y1, x0:x1]
        af = a[y0:y1, x0:x1].astype(np.float32)
        base = af + (b[y0:y1, x0:x1].astype(np.float32) - af) * np.take(m_t, cid)[..., None]
        g = np.take(fill_t, cid, axis=0) * cf[y0:y1, x0:x1]
        g += np.take(line_t, cid, axis=0) * cl[y0:y1, x0:x1]
        w1 = wy[y0:y1, None, None]                       # step 2: interpolate vertically
        g += 0.55 * (bh[ry0[y0:y1], x0:x1] * (1.0 - w1) + bh[ry0[y0:y1] + 1, x0:x1] * w1)
        np.clip(g, 0.0, 1.0, out=g)
        base += g * (255.0 - base)                       # screen blend: light on top of the footage
        base += 0.5
        base += noise[y0:y1, x0:x1]                      # +-0.45 level dither, no banding
        out[y0:y1, x0:x1] = np.clip(base, 0.0, 255.0).astype(np.uint8)
    return out
