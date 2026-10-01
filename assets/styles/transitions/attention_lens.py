"""Attention lens: a glass lens of attention opens from the centre and swallows the frame.

Inside the lens is the incoming video, outside is the outgoing one (dimmed and softened as the
lens nears). The rim is a thin luminous ring (white core, cyan then electric-blue halo, brighter
on the upper-left like a glass edge catching light) with a narrow band of real radial
refraction on both sides and a one-to-two pixel red/blue fringe. Everything is drawn from
radial lookup tables and a once-sorted pixel list, so only the pixels near the rim are touched.
"""
import numpy as np
from scipy.ndimage import gaussian_filter

NAME = "attention-lens"
TITLE = "Attention lens"
SECONDS = 1.0
DESCRIPTION = ("A luminous glass lens opens from the centre of the frame, bending the image at its "
               "rim, and grows until the incoming video fills the screen.")

_CYAN = np.array([90, 209, 255], np.float32) / 255.0
_BLUE = np.array([74, 125, 255], np.float32) / 255.0
_LIGHT = np.array([-0.866, -0.5], np.float32)  # direction the rim highlight sits (up-left)


def _smooth(x):
    x = min(max(float(x), 0.0), 1.0)
    return x * x * (3.0 - 2.0 * x)


def _ease(t):
    """Cubic Hermite ease: gentle start, strong middle, and a soft landing that is still moving
    when the rim leaves the frame (so no dead frames at the end, like a focus pull settling)."""
    t2, t3 = t * t, t * t * t
    return (t3 - 2 * t2 + t) * 0.10 + (-2 * t3 + 3 * t2) + (t3 - t2) * 0.60


def _prepare(state):
    W, H = state["size"]
    s = H / 1080.0
    P = {"W": W, "H": H, "s": s}
    cx, cy = (W - 1) / 2.0, (H - 1) / 2.0
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    dx, dy = xx - np.float32(cx), yy - np.float32(cy)
    rho = np.hypot(dx, dy).astype(np.float32)
    inv = 1.0 / np.maximum(rho, 1e-3)
    ux, uy = dx * inv, dy * inv
    P["rc"] = float(np.hypot(W / 2.0, H / 2.0))                 # centre -> outer corner
    P["rmax"] = P["rc"] + 40.0 * s                               # rim fully off-frame
    P["rn"] = int(P["rc"]) + 3
    P["rr"] = np.arange(P["rn"], dtype=np.float32)
    P["rho_i"] = np.minimum(rho.astype(np.intp), P["rn"] - 1)

    order = np.argsort(rho.ravel(), kind="stable")
    P["order"] = order
    P["rho_s"] = rho.ravel()[order]
    P["ux_s"], P["uy_s"] = ux.ravel()[order], uy.ravel()[order]
    P["x_s"] = (order % W).astype(np.float32)
    P["y_s"] = (order // W).astype(np.float32)
    cphi = ux.ravel()[order] * _LIGHT[0] + uy.ravel()[order] * _LIGHT[1]
    P["hl_s"] = (0.80 + 0.20 * cphi + 0.60 * np.maximum(cphi, 0.0) ** 18
                 + 0.10 * np.maximum(-cphi, 0.0) ** 18).astype(np.float32)
    rng = np.random.default_rng(state["seed"])
    P["dither_s"] = rng.random(order.size, dtype=np.float32)    # stochastic rounding

    # separable bilinear upsample tables for the softened copy of the outgoing frame
    f = max(1, int(round(H / 270.0)))
    h, w = H // f, W // f
    P["f"], P["h"], P["w"] = f, h, w
    if f > 1:
        def tables(n, m):
            c = np.clip((np.arange(n, dtype=np.float32) - (f - 1) / 2.0) / f, 0, m - 1)
            i0 = np.floor(c).astype(np.intp)
            return i0, np.minimum(i0 + 1, m - 1), (c - i0).astype(np.float32)
        P["y0"], P["y1"], P["wy"] = tables(H, h)
        P["x0"], P["x1"], P["wx"] = tables(W, w)
    return P


def _soften(a, P, sigma_full):
    """Gaussian-softened copy of `a` (float32 HxWx3), computed on a 1/f grid."""
    H, W, f, h, w = P["H"], P["W"], P["f"], P["h"], P["w"]
    S = np.zeros((h, w, 3), np.float32)
    for i in range(f):
        for j in range(f):
            S += a[i:h * f:f, j:w * f:f]
    S *= 1.0 / (f * f)
    sg = max(0.6, sigma_full / f)
    S = gaussian_filter(S, sigma=(sg, sg, 0), mode="nearest")
    if f == 1:
        return S
    X = S[:, P["x0"], :] * (1 - P["wx"])[None, :, None] + S[:, P["x1"], :] * P["wx"][None, :, None]
    return X[P["y0"]] * (1 - P["wy"])[:, None, None] + X[P["y1"]] * P["wy"][:, None, None]


def _bilinear(src, c, sx, sy, W, H):
    """Bilinear sample of channel c from a flat interleaved image; sx, sy are float32 pixel coords."""
    np.clip(sx, 0.0, W - 1.001, out=sx)
    np.clip(sy, 0.0, H - 1.001, out=sy)
    x0 = sx.astype(np.intp)
    y0 = sy.astype(np.intp)
    fx = sx - x0
    fy = sy - y0
    base = (y0 * W + x0) * 3 + c
    top = src.take(base) * (1 - fx) + src.take(base + 3) * fx
    bot = src.take(base + 3 * W) * (1 - fx) + src.take(base + 3 * W + 3) * fx
    return top * (1 - fy) + bot * fy


def _warp(src, P, lo, hi, r, sign, bw, amp, fringe, env):
    """Radially displaced RGB samples for sorted-pixel range [lo, hi). sign=+1 samples outward."""
    W, H = P["W"], P["H"]
    d = P["rho_s"][lo:hi] - r
    w = np.clip(1.0 + sign * d / bw, 0.0, 1.0)      # 1 at the rim, 0 at the band edge
    w2 = w * w * env
    ux, uy = P["ux_s"][lo:hi], P["uy_s"][lo:hi]
    xs, ys = P["x_s"][lo:hi], P["y_s"][lo:hi]
    out = np.empty((hi - lo, 3), np.float32)
    for c, eps in enumerate((fringe, 0.0, -fringe)):          # R, G, B radial offsets
        disp = sign * amp * w2 + eps * w * env
        out[:, c] = _bilinear(src, c, xs + ux * disp, ys + uy * disp, W, H)
    return out


def render(a, b, t, state):
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    P = state.get("_attention_lens") or state.setdefault("_attention_lens", _prepare(state))
    W, H, s = P["W"], P["H"], P["s"]
    r = P["rmax"] * _ease(t)
    rc = P["rc"]

    # ---- envelopes -------------------------------------------------------------------
    ring_env = _smooth(r / (16.0 * s)) * (1.0 - _smooth((r - (rc - 70.0 * s)) / (100.0 * s)))
    T = _smooth(t / 0.30)                                       # outside dim/soften ramp

    # ---- outgoing frame: dim + soften, via radial lookup tables ---------------------------
    d_l = P["rr"] - np.float32(r)
    near = np.exp(-np.maximum(d_l, 0.0) / (240.0 * s))
    dim = 1.0 - 0.30 * T * (0.55 + 0.45 * near)
    mix = 0.90 * T * (0.45 + 0.55 * near)
    bw_in, bw_out = 46.0 * s, 34.0 * s
    inside_lut = P["rr"] <= (r - bw_in)
    a_f = a.astype(np.float32)
    ap = a_f * (dim * (1 - mix)).take(P["rho_i"])[..., None]
    if T > 0:
        ap += _soften(a, P, 5.0 * s) * (dim * mix).take(P["rho_i"])[..., None]
    out = (ap + 0.5).astype(np.uint8)
    np.copyto(out, b, where=inside_lut.take(P["rho_i"])[..., None])

    # ---- refraction band ---------------------------------------------------------------
    rho_s, order = P["rho_s"], P["order"]
    aa = max(1.0, 1.25 * s)
    ss = np.searchsorted
    lo, hi = ss(rho_s, r - bw_in), ss(rho_s, r + bw_out)
    mid_out, mid_in = ss(rho_s, r - aa), ss(rho_s, r + aa)
    fringe = 1.5 * s
    Bw = _warp(b.reshape(-1), P, lo, mid_in, r, +1, bw_in, 13.0 * s, fringe, ring_env)
    Aw = _warp(ap.reshape(-1), P, mid_out, hi, r, -1, bw_out, 9.0 * s, fringe, ring_env)
    band = np.empty((hi - lo, 3), np.float32)
    n_in, k = mid_out - lo, mid_in - mid_out
    band[:n_in] = Bw[:n_in]
    cov = np.clip(0.5 - (rho_s[mid_out:mid_in] - r) / aa, 0.0, 1.0)[:, None]
    band[n_in:n_in + k] = cov * Bw[n_in:] + (1 - cov) * Aw[:k]
    band[n_in + k:] = Aw[k:]

    # ---- the rim light, screened over a gathered strip of pixels ---------------------------
    g_in, g_out = 56.0 * s, 130.0 * s
    lo_g, hi_g = ss(rho_s, r - g_in), ss(rho_s, r + g_out)
    idx = order[lo_g:hi_g]
    flat = out.reshape(-1, 3)
    base = flat[idx].astype(np.float32)
    base[lo - lo_g:hi - lo_g] = band
    d = rho_s[lo_g:hi_g] - r
    ad = np.abs(d)
    inner = d < 0
    sig = max(0.8, 1.15 * s)
    core = np.exp(-0.5 * (d / sig) ** 2)
    h1 = np.exp(-ad / np.where(inner, 5.0 * s, 7.0 * s))
    h2 = np.exp(-ad / np.where(inner, 12.0 * s, 34.0 * s))
    h3 = np.where(inner, 0.0, np.exp(-ad / (85.0 * s)))        # wide, faint bloom outside only
    side = np.where(inner, 0.55, 1.0).astype(np.float32)        # keep the inside calmer
    hl = P["hl_s"][lo_g:hi_g] * np.float32(ring_env)
    L = ((1.10 * core * hl)[:, None]
         + (0.55 * h1 * hl * side)[:, None] * _CYAN
         + (0.40 * h2 * hl * side)[:, None] * _BLUE
         + (0.075 * h3 * hl)[:, None] * _BLUE)
    np.clip(L, 0.0, 1.0, out=L)
    base += (255.0 - base) * L
    base += P["dither_s"][lo_g:hi_g, None]
    np.clip(base, 0.0, 255.0, out=base)
    flat[idx] = base.astype(np.uint8)
    return out
