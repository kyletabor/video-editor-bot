"""Ink brush: three broad dry-brush strokes sweep upper-left to lower-right and reveal b.

Every pixel gets, per stroke, an "arrival coordinate" A (how far along the stroke the brush
head must travel before that pixel is wet).  A is the stroke axis coordinate plus a chisel
slant, a rounded tip, ragged tip noise and a dry-brush lag: a 1-D bristle profile across the
stroke width, stretched along its length, so some bristles trail the head as long streaks.
The stroke's sides fray the same way.  A frame is then: eased head position (a scalar)
minus A, through a soft smoothstep, so every edge is a float mask, never a hard pixel.
Light is laid down where the brush is fresh and fades with the time since each pixel was
reached: a thin pale-gold rim (#f2e6c8) along every reveal edge, a short halo ahead of the
head, and a faint warm glaze behind it that carries the bristle streaks.  That light is what
makes the edge readable on dark footage; it is a few levels of mean luma, never a flash.
Everything is measured in "design pixels" (1080p) and divided by H / 1080, so the picture is
the same at any size; all randomness comes from default_rng(state["seed"]).
"""
import math

import numpy as np
from scipy import ndimage

NAME = "ink-brush"
TITLE = "Ink brush"
SECONDS = 1.3
DESCRIPTION = ("Three broad dry-brush strokes sweep diagonally across the frame one after another, "
               "their edges frayed into bristle streaks and lit by a thin pale-gold rim, revealing the next clip.")

LIGHT = np.array([242.0, 230.0, 200.0], np.float32)   # #f2e6c8, the rim
GOLD = np.array([226.0, 198.0, 140.0], np.float32)    # the faint wet glaze behind it
ANGLE = 27.0                                          # degrees below horizontal, left to right
FRAY = 70.0                                           # lateral dry-brush fringe (design px)
SOFT_L = 4.0                                          # lateral edge softness (design px)
GRAIN = 3.5                                           # pixel-level edge grain (design px)
FADE_S = 0.10                                         # light decays with this time constant (s)

# start / duration are fractions of the whole transition; ease is a Beta(a, b) velocity curve
# (a quick lift-off, a long settle), slope = chisel slant, bulge = rounded tip, lag = dry-brush
STROKES = [
    dict(start=0.00, dur=0.62, a=2.0, b=2.3, frac=0.35, slope=0.40, bulge=0.30, lag=170.0),
    dict(start=0.20, dur=0.62, a=2.0, b=2.1, frac=0.30, slope=-0.30, bulge=0.28, lag=190.0),
    dict(start=0.42, dur=0.55, a=1.9, b=1.8, frac=0.35, slope=0.35, bulge=0.32, lag=175.0),
]


def _smooth_noise(rng, n, sigmas, weights):
    """1-D noise table, rank-transformed so values are uniform on [0, 1]."""
    acc = np.zeros(n)
    for s, w in zip(sigmas, weights):
        x = ndimage.gaussian_filter1d(rng.standard_normal(n), s, mode="wrap")
        acc += w * x / x.std()
    out = np.empty(n)
    out[np.argsort(acc)] = (np.arange(n) + 0.5) / n
    return out.astype(np.float32)


def _ease_table(a, b, n=2048):
    x = np.linspace(0.0, 1.0, n)
    pdf = x ** (a - 1.0) * (1.0 - x) ** (b - 1.0)
    cdf = np.cumsum(pdf)
    cdf -= cdf[0]
    cdf /= cdf[-1]
    return x, cdf


def _build(state):
    W, H = state["size"]
    S = H / 1080.0
    rng = np.random.default_rng(state["seed"])
    th = math.radians(ANGLE)
    ct, st = math.cos(th), math.sin(th)
    xs = (np.arange(W, dtype=np.float32) + 0.5) / S
    ys = (np.arange(H, dtype=np.float32) + 0.5) / S
    U = (xs[None, :] * ct + ys[:, None] * st).astype(np.float32)      # along the stroke
    V = (-xs[None, :] * st + ys[:, None] * ct).astype(np.float32)     # across the stroke
    wd, hd = W / S, H / S                                               # frame in design px
    cu = [x * ct + y * st for x in (0.0, wd) for y in (0.0, hd)]
    cv = [-x * st + y * ct for x in (0.0, wd) for y in (0.0, hd)]
    u_lo, u_hi = math.floor(min(cu)), math.ceil(max(cu))
    v_lo, v_hi = math.floor(min(cv)), math.ceil(max(cv))

    # 1-D tables shared by all strokes' lookups, defined over design-pixel coordinates
    pad = 700.0
    vt = np.arange(v_lo - pad, v_hi + pad, 1.0, dtype=np.float64)
    ut = np.arange(u_lo - pad, u_hi + pad, 4.0, dtype=np.float64)

    fr = np.array([s["frac"] for s in STROKES])
    edges = v_lo + (v_hi - v_lo) * np.concatenate([[0.0], np.cumsum(fr / fr.sum())])
    overlap = 0.5 * FRAY + SOFT_L + 12.0

    strokes = []
    for k, sp in enumerate(STROKES):
        c0, c1 = edges[k], edges[k + 1]
        centre, hw = 0.5 * (c0 + c1), 0.5 * (c1 - c0)
        core_lo = -1e6 if k == 0 else c0 - overlap
        core_hi = 1e6 if k == len(STROKES) - 1 else c1 + overlap

        # bristle load per bristle (position across the width), three scales of clumping
        r1 = _smooth_noise(rng, len(vt), (2.0, 6.0, 22.0), (0.55, 0.40, 0.45))
        r2 = _smooth_noise(rng, len(vt), (2.6, 9.0, 36.0), (0.50, 0.40, 0.40))
        rq = _smooth_noise(rng, len(vt), (1.0, 4.0, 14.0), (0.60, 0.40, 0.30))
        rg = _smooth_noise(rng, len(vt), (6.0, 18.0), (0.5, 0.5))
        # slow variation along the stroke: pressure and wander of the bristle lines
        mod1 = _smooth_noise(rng, len(ut), (40.0, 90.0), (0.5, 0.5))
        mod2 = _smooth_noise(rng, len(ut), (40.0, 90.0), (0.5, 0.5))
        wob1 = (_smooth_noise(rng, len(ut), (60.0, 140.0), (0.5, 0.5)) - 0.5) * 60.0
        wob2 = (_smooth_noise(rng, len(ut), (60.0, 140.0), (0.5, 0.5)) - 0.5) * 60.0
        wob3 = (_smooth_noise(rng, len(ut), (60.0, 140.0), (0.5, 0.5)) - 0.5) * 40.0

        def lut_v(table, shift):
            return np.interp(V + shift, vt, table).astype(np.float32)

        def lut_u(table):
            return np.interp(U, ut, table).astype(np.float32)

        m1, m2 = lut_u(mod1), lut_u(mod2)
        b1 = lut_v(r1, lut_u(wob1))
        b2 = lut_v(r2, lut_u(wob2) + 211.0)
        lag = sp["lag"] * (b1 ** 3.2 * (0.35 + 0.65 * m1) + 0.4 * b2 ** 4.0 * m2)
        rag = (lut_v(rg, 0.0) - 0.5) * 46.0

        z = np.clip((V - centre) / hw, -1.25, 1.25)
        tip = sp["slope"] * hw * 0.5 * z + sp["bulge"] * hw * 0.5 * z * z
        grain = ndimage.gaussian_filter(
            np.random.default_rng([state["seed"], 100 + k]).standard_normal((H, W)).astype(np.float32),
            max(0.7, S))
        grain *= GRAIN / float(grain.std())
        A = (U + tip + lag + rag + grain).astype(np.float32)

        # lateral band: weight 1 in the core, frayed into bristle streaks outside it
        din = np.minimum(V - core_lo, core_hi - V)
        q = lut_v(rq, lut_u(wob3) - 97.0)
        band = np.clip((din + FRAY * (q - 0.4)) / SOFT_L + 0.5, 0.0, 1.0)
        band = (band * band * (3.0 - 2.0 * band)).astype(np.float32)

        xe, ce = _ease_table(sp["a"], sp["b"])
        inside = band > 0.02
        a_min = float(A[inside].min())
        a_max = float(A[inside].max())
        u0 = a_min - 70.0
        u1 = a_max + 10.0
        x_pix = np.clip((A - u0) / (u1 - u0), 0.0, 1.0)
        tau = sp["start"] + sp["dur"] * np.interp(x_pix, ce, xe)          # when the head reaches each pixel
        etau = np.exp(tau * (SECONDS / FADE_S)).astype(np.float32)
        strokes.append(dict(sp=sp, A=A, band=band, etau=etau, u0=u0, u1=u1, a_min=a_min,
                            a_max=a_max, xe=xe, ce=ce, rim_tex=(0.72 + 0.28 * b1).astype(np.float32),
                            wake_tex=(0.35 + 0.65 * b1).astype(np.float32)))
    return dict(S=S, strokes=strokes)


def _smoothstep(lo, hi, x):
    x = min(max((x - lo) / (hi - lo), 0.0), 1.0)
    return x * x * (3.0 - 2.0 * x)


def _head(sk, t):
    sp = sk["sp"]
    x = (t - sp["start"]) / sp["dur"]
    x = min(max(x, 0.0), 1.0)
    return sk["u0"] + (sk["u1"] - sk["u0"]) * float(np.interp(x, sk["xe"], sk["ce"]))


def render(a, b, t, state):
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    if "ink" not in state:
        state["ink"] = _build(state)
    ink = state["ink"]
    S = ink["S"]
    H, W = a.shape[:2]
    n = max(2, int(state.get("n_frames", 29)))
    dt = 1.0 / (n - 1)
    if "buf" not in ink:
        ink["buf"] = [np.empty((H, W), np.float32) for _ in range(5)]
    sd, m, tmp, fade, wk = ink["buf"]

    keep = None            # product of (1 - M_k): fraction of a that survives
    lit = None             # product of (1 - rim_k): light laid down at the edges (rim + halo)
    glaze = None           # product of (1 - wake_k): faint glaze behind the head
    env = 1.0 - _smoothstep(0.86, 0.975, t)      # all light is gone before the last frame
    k_fade = SECONDS / FADE_S
    e_t = math.exp(-k_fade * t) * env
    for sk in ink["strokes"]:
        sp = sk["sp"]
        if t <= sp["start"]:
            continue
        h = _head(sk, t)
        speed = (h - _head(sk, t - dt)) if t - dt > sp["start"] else h - sk["u0"]
        soft = max(8.0, 1.6 / S) + 0.16 * max(speed, 0.0)
        if h <= sk["a_min"] - 90.0:
            continue
        band = sk["band"]
        if h >= sk["a_max"] + soft and t - (sp["start"] + sp["dur"]) > 3.0 / k_fade:
            np.subtract(1.0, band, out=tmp)
            keep = tmp.copy() if keep is None else np.multiply(keep, tmp, out=keep)
            continue
        # reveal mask: eased head position minus the pixel's arrival coordinate, softened
        np.subtract(h, sk["A"], out=sd)
        np.multiply(sd, 1.0 / soft, out=m)
        m += 0.5
        np.clip(m, 0.0, 1.0, out=m)
        np.multiply(m, m, out=tmp)
        m *= -2.0
        m += 3.0
        m *= tmp
        m *= band                                                   # m is now M_k
        np.subtract(1.0, m, out=tmp)
        keep = tmp.copy() if keep is None else np.multiply(keep, tmp, out=keep)
        if env <= 0.0:
            continue
        # light fades with the time since each pixel was reached: exp(-(t - tau)/FADE), <= 1
        np.multiply(sk["etau"], e_t, out=fade)
        np.minimum(fade, env, out=fade)
        # thin rim hugging every reveal edge (4 M (1 - M)) + a halo around the head
        np.multiply(tmp, m, out=tmp)                                # M (1 - M)
        tmp *= 0.95 * 4.0
        tmp *= sk["rim_tex"]
        np.abs(sd, out=sd)
        sd *= -1.0 / 22.0
        np.exp(sd, out=sd)
        sd *= band
        sd *= 0.22
        tmp += sd
        tmp *= fade
        np.minimum(tmp, 0.92, out=tmp)                              # rim_k
        np.subtract(1.0, tmp, out=tmp)
        lit = tmp.copy() if lit is None else np.multiply(lit, tmp, out=lit)
        # faint warm glaze behind the head, textured like the bristles
        np.multiply(m, sk["wake_tex"], out=wk)
        wk *= fade
        wk *= -0.24
        wk += 1.0                                                   # 1 - wake_k
        glaze = wk.copy() if glaze is None else np.multiply(glaze, wk, out=glaze)

    if keep is None:
        return a
    af = a.astype(np.float32)
    out = b.astype(np.float32)
    out -= af
    out *= (1.0 - keep)[..., None]
    out += af
    if lit is not None:
        rim = 1.0 - lit
        wake = 1.0 - glaze
        total = 1.0 - lit * glaze
        share = rim / np.maximum(rim + wake, 1e-4)                  # how much of the light is rim
        out *= (1.0 - total)[..., None]
        out += total[..., None] * GOLD
        out += (total * share)[..., None] * (LIGHT - GOLD)
    np.clip(out, 0.0, 255.0, out=out)
    return (out + 0.5).astype(np.uint8)
