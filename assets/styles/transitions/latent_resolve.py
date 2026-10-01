"""Latent resolve: the picture collapses into a coarse grid of colour blocks (a "latent"),
the next picture resolves out of that grid, from coarse to fine.

  first third : `a` is pooled into ever larger square blocks (8, 16, 32, 64 px at 1080p), each
                level cross-faded into the next, while a faint cyan grid and a soft glow come in
  middle      : at the coarsest level a's block colours hand over to b's, block by block, behind a
                soft scan line of light that travels once across the frame
  last third  : `b` refines from the coarse blocks back to full detail

Everything is smooth and deterministic: the only randomness is a fixed per-block timing offset
drawn from numpy.random.default_rng(state["seed"]); nothing flickers from frame to frame.
"""
import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

NAME = "latent-resolve"
TITLE = "Latent resolve"
SECONDS = 1.1
DESCRIPTION = ("The picture collapses into a coarse grid of colour blocks, a scan line of light carries "
               "the grid from one picture to the next, and the new picture resolves out of it.")

BASE_BLOCKS = (8, 16, 32, 64)        # block sizes at 1080p; scaled by H / 1080
T1, T2 = 0.32, 0.68                  # end of collapse / start of refine
GRADE = (0.0, 0.30, 0.55, 0.80, 1.0)  # latent "grade" strength per level (0 = untouched picture)
GRID_ALPHA = (0.0, 0.0, 0.0, 0.04, 0.13)   # faint cyan grid per level
BLOOM_AMOUNT = 0.45
FLARE_AMOUNT = 0.22                  # cyan lift on a block as the scan line passes over it
NODE_ALPHA = 0.10                    # brightness of the little nodes where grid lines cross
FRONT_WIDTH = 0.24                   # how far behind the scan line the blocks finish handing over
FRONT_LEAD = 0.10                    # fraction of that zone that starts just ahead of the line
JITTER = 0.035                       # per-block timing offset, fraction of frame width (static)

WHITE = np.array([0.93, 0.97, 1.00], np.float32)
CYAN = np.array([0.353, 0.82, 1.00], np.float32)       # ~ #5ad1ff
BLUE = np.array([0.29, 0.49, 1.00], np.float32)        # ~ #4a7dff
NAVY = np.array([0.006, 0.016, 0.050], np.float32)     # what black lifts towards in the latent
LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)


def _sstep(x):
    x = min(max(float(x), 0.0), 1.0)
    return x * x * (3.0 - 2.0 * x)


def _sstep_arr(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _setup(state, H, W):
    key = (H, W)
    cached = state.get("_latent_resolve")
    if cached is not None and cached["key"] == key:
        return cached
    scale = H / 1080.0
    sizes = [max(2, int(round(s * scale))) for s in BASE_BLOCKS]
    for i in range(1, len(sizes)):
        sizes[i] = max(sizes[i], sizes[i - 1] + 1)
    lw = max(1, int(round(1.6 * scale)))
    levels = [None]  # level 0 is the untouched picture
    for bs in sizes:
        gh, gw = -(-H // bs), -(-W // bs)
        cx = (np.arange(gw) * bs + np.minimum((np.arange(gw) + 1) * bs, W)) / 2.0
        cy = (np.arange(gh) * bs + np.minimum((np.arange(gh) + 1) * bs, H)) / 2.0

        def lines(n_blocks, extent):
            edge = (np.arange(1, n_blocks) * bs)[:, None] + np.arange(lw)[None, :] - lw // 2
            edge = np.unique(edge.ravel())
            return edge[(edge >= 0) & (edge < extent)]

        levels.append({"bs": bs, "gh": gh, "gw": gw, "cx": cx.astype(np.float32),
                       "cy": cy.astype(np.float32), "cols": lines(gw, W), "rows": lines(gh, H)})
    top = levels[-1]
    rng = np.random.default_rng(state.get("seed", 0))
    jitter = rng.uniform(-1.0, 1.0, (top["gh"], top["gw"])).astype(np.float32) * JITTER
    top["nx"] = top["cx"][None, :] / W + jitter           # block position along the scan, with offset
    y = np.arange(H, dtype=np.float32)
    cached = {"key": key, "levels": levels, "vprof": (0.62 + 0.38 * np.sin(np.pi * (y + 0.5) / H)).astype(np.float32),
              "x": np.arange(W, dtype=np.float32)}
    state["_latent_resolve"] = cached
    return cached


def _pool(frame, bs):
    """Mean colour of each bs x bs block (edge rows/cols padded), as float32 0..1 of shape (gh, gw, 3)."""
    H, W = frame.shape[:2]
    ph, pw = (-H) % bs, (-W) % bs
    if ph or pw:
        frame = np.pad(frame, ((0, ph), (0, pw), (0, 0)), mode="edge")
    gh, gw = frame.shape[0] // bs, frame.shape[1] // bs
    rows = frame.reshape(gh * bs, gw, bs, 3).mean(axis=2, dtype=np.float32)
    return rows.reshape(gh, bs, gw, 3).mean(axis=1, dtype=np.float32) / 255.0


def _grade(c, g):
    """Make the latent read: a touch more colour, lifted darks, shadows leaning navy."""
    if g <= 0:
        return c
    luma = (c @ LUMA)[..., None]
    c = np.clip(luma + (c - luma) * (1.0 + 0.40 * g), 0.0, 1.0)
    c = c ** (1.0 - 0.14 * g)
    return c + g * NAVY * (1.0 - c) ** 2


def _up(small, bs, H, W):
    return np.repeat(np.repeat(small, bs, axis=0), bs, axis=1)[:H, :W]


def _bloom(small, H, W):
    """Soft light bleeding out of the brighter blocks, bilinear-upsampled to full size."""
    src = np.clip(small - 0.22, 0.0, None)
    blur = gaussian_filter(src, sigma=(1.3, 1.3, 0), mode="nearest")
    chans = [np.asarray(Image.fromarray(np.ascontiguousarray(blur[..., i], dtype=np.float32)).resize((W, H), Image.BILINEAR))
             for i in range(3)]
    glow = np.stack(chans, axis=-1)
    return glow * np.array([0.85, 1.0, 1.15], np.float32)


def _add_lines(out, level, alpha, boost_x, node=0.0):
    """Faint cyan lines on the block boundaries; `boost_x` (len W) brightens them near the scan line."""
    cols, rows = level["cols"], level["rows"]
    if cols.size:
        ax = alpha + boost_x[cols]
        out[:, cols, :] += ax[None, :, None] * CYAN
    if rows.size:
        ay = alpha + boost_x
        out[rows, :, :] += ay[None, :, None] * CYAN
    if cols.size and rows.size and node:
        # small bright nodes where the grid lines cross
        nc = np.unique(np.clip(np.concatenate([cols - 1, cols, cols + 1]), 0, out.shape[1] - 1))
        nr = np.unique(np.clip(np.concatenate([rows - 1, rows, rows + 1]), 0, out.shape[0] - 1))
        an = node * (alpha / max(GRID_ALPHA[4], 1e-6)) + boost_x[nc] * 0.9
        out[np.ix_(nr, nc)] += (an[None, :, None] * WHITE)


def _scan(out, sx, W, vprof):
    """The soft luminous line: white core, cyan halo, wide electric-blue haze."""
    s1, s2, s3 = 0.0014 * W, 0.0080 * W, 0.040 * W
    x0, x1 = int(max(0, sx - 4.2 * s3)), int(min(W, sx + 4.2 * s3))
    if x1 <= x0:
        return
    d = np.arange(x0, x1, dtype=np.float32) - sx
    prof = (np.exp(-0.5 * (d / s1) ** 2)[:, None] * WHITE * 0.95
            + np.exp(-0.5 * (d / s2) ** 2)[:, None] * CYAN * 0.46
            + np.exp(-0.5 * (d / s3) ** 2)[:, None] * BLUE * 0.18)
    out[:, x0:x1, :] += vprof[:, None, None] * prof[None, :, :]


def _screen(base, light):
    """Lay light over the picture: never exceeds white, and dims where the picture is already bright."""
    np.clip(light, 0.0, 0.985, out=light)
    light -= 1.0
    light *= -1.0                         # 1 - light
    light *= (1.0 - base)                 # (1 - base) * (1 - light)
    return 1.0 - light


def render(a, b, t, state):
    if t <= 0:
        return a
    if t >= 1:
        return b
    H, W = a.shape[:2]
    S = _setup(state, H, W)
    lv = S["levels"]
    light = np.zeros((H, W, 3), np.float32)       # everything luminous we add goes here

    if T1 <= t <= T2:
        # ---- hand-over at the coarsest level, under the scan line
        q = (t - T1) / (T2 - T1)
        q = 0.5 * q + 0.5 * _sstep(q)
        ls0 = -(JITTER + FRONT_LEAD * FRONT_WIDTH) - 0.03   # line starts left of frame, all blocks still a
        ls1 = 1.0 + JITTER + FRONT_WIDTH * (1.0 - FRONT_LEAD) + 0.01   # ends when every block is b
        ls = ls0 + (ls1 - ls0) * q                          # scan line position, in frame widths
        top = lv[4]
        A = _grade(_pool(a, top["bs"]), GRADE[4])
        B = _grade(_pool(b, top["bs"]), GRADE[4])
        m = _sstep_arr((ls - top["nx"]) / FRONT_WIDTH + FRONT_LEAD)[..., None]
        small = A * (1.0 - m) + B * m
        # blocks flare as the line passes over them, then the glow decays behind it
        behind = ls - top["nx"]                              # > 0 for blocks the line has passed
        flare = (_sstep_arr((behind + 0.015) / 0.03) * np.exp(-np.maximum(behind, 0.0) / 0.11))[..., None]
        flare = flare * (1.0 - _sstep((q - 0.80) / 0.20))   # trail has fully faded when the hand-over ends
        small = 1.0 - (1.0 - small) * (1.0 - flare * FLARE_AMOUNT * CYAN)
        base = np.array(_up(small, top["bs"], H, W), dtype=np.float32)
        light += _bloom(small, H, W) * BLOOM_AMOUNT
        sx = ls * W
        boost = (0.30 * np.exp(-0.5 * ((S["x"] - sx) / (0.09 * W)) ** 2)).astype(np.float32)
        _add_lines(light, lv[4], GRID_ALPHA[4], boost, node=NODE_ALPHA)
        _scan(light, sx, W, S["vprof"])
    else:
        # ---- collapse of `a` (first third) or refinement of `b` (last third)
        if t < T1:
            src, s = a, _sstep(t / T1)
            u = 4.0 * s
        else:
            src, s = b, _sstep((t - T2) / (1.0 - T2))
            u = 4.0 * (1.0 - s)
        k = min(int(u), 3)
        f = u - k

        def level(i):
            if i == 0:
                return src.astype(np.float32) / 255.0
            L = lv[i]
            return _up(_grade(_pool(src, L["bs"]), GRADE[i]), L["bs"], H, W)

        base = level(k) * (1.0 - f)
        base += level(k + 1) * f
        bloom_w = BLOOM_AMOUNT * _sstep((u - 2.5) / 1.5)
        if bloom_w > 0:
            top = lv[4]
            light += _bloom(_grade(_pool(src, top["bs"]), GRADE[4]), H, W) * bloom_w
        zero_x = np.zeros(W, np.float32)
        for i in (3, 4):
            w = max(0.0, 1.0 - abs(u - i))
            if w > 0 and GRID_ALPHA[i] > 0:
                _add_lines(light, lv[i], GRID_ALPHA[i] * w, zero_x, node=NODE_ALPHA if i == 4 else 0.0)

    out = _screen(base, light)
    return (out * 255.0 + 0.5).astype(np.uint8)
