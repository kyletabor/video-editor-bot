"""Neural mesh: a network of nodes and edges lights up from the left and flips a -> b cell by cell.

Geometry (jittered-grid nodes, Delaunay edges, activation times from a graph shortest-path
spread) is built once into `state`. Each node owns its nearest-neighbour (Voronoi) cell; a cell
opens from its node outward shortly after the node fires, with a soft edge. The light layers
(edges, node pulses, expanding rings, bloom) are drawn with Pillow at 2x supersampling, then
glowed with small-resolution gaussian blurs. Palette: cool white, cyan #5ad1ff, blue #4a7dff.
"""
import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import gaussian_filter
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import Delaunay, cKDTree

NAME = "neural-mesh"
TITLE = "Neural mesh"
SECONDS = 1.1
DESCRIPTION = ("A glowing network of nodes and edges activates from the left and the incoming "
               "video opens cell by cell behind the front before the mesh fades away.")

# Timeline (fractions of t)
FRONT_START, FRONT_END = 0.05, 0.62   # when the activation front leaves / reaches the far side
EDGE_LEAD = 0.06                      # edges start drawing this far ahead of their node firing
FLIP_LAG = 0.05                       # cell starts opening this long after its node fires
FLIP_DUR = 0.09                       # how long a cell takes to open
IRIS = 0.035                          # extra delay from a cell's node to its far corners
FADE_START, FADE_END = 0.62, 0.93     # whole mesh fades out
PULSE_DUR = 0.17                      # node ring/flare life

SPACING = 124.0 / 1080.0              # node spacing, in units of frame height
WHITE = np.array([0.84, 0.97, 1.0], np.float32)
CYAN = np.array([0.353, 0.820, 1.0], np.float32)
BLUE = np.array([0.290, 0.490, 1.0], np.float32)


def _smooth(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _front_ease(x):
    """Eased front position: mostly linear with soft start and landing."""
    return 0.45 * x + 0.55 * _smooth(x)


def _resize(arr, size, resample=Image.BILINEAR):
    return np.asarray(Image.fromarray(np.ascontiguousarray(arr, np.float32), "F").resize(size, resample))


def _setup(state):
    W, H = state["size"]
    s = H / 1080.0
    rng = np.random.default_rng(state["seed"])
    aspect = W / H

    # ---- nodes: jittered grid with a one-cell margin outside the frame -------------------
    cols = int(np.ceil(aspect / SPACING)) + 3
    rows = int(np.ceil(1.0 / SPACING)) + 3
    gx, gy = np.meshgrid(np.arange(cols), np.arange(rows))
    pts = np.stack([(gx.ravel() - 1) * SPACING, (gy.ravel() - 1) * SPACING], 1)
    pts += rng.uniform(-0.40, 0.40, pts.shape) * SPACING
    n = len(pts)

    # ---- edges: Delaunay, drop overlong ones ---------------------------------------------
    tri = Delaunay(pts)
    es = set()
    for a_, b_, c_ in tri.simplices:
        for u, v in ((a_, b_), (b_, c_), (c_, a_)):
            es.add((min(u, v), max(u, v)))
    es = np.array(sorted(es))
    ln = np.hypot(*(pts[es[:, 0]] - pts[es[:, 1]]).T)
    es, ln = es[ln < 1.7 * SPACING], ln[ln < 1.7 * SPACING]

    # ---- activation: graph shortest path from a focal point left of centre ---------------
    aniso = np.array([1.0, 0.62])
    wlen = np.hypot(*((pts[es[:, 0]] - pts[es[:, 1]]) * aniso).T) * rng.uniform(0.88, 1.20, len(es))
    focal = np.array([-0.10 * aspect, 0.5])
    seed_d = np.hypot(*((pts - focal) * aniso).T)
    seeds = np.argsort(seed_d)[:6]
    rows_i = np.concatenate([es[:, 0], es[:, 1], np.full(len(seeds), n)])
    cols_i = np.concatenate([es[:, 1], es[:, 0], seeds])
    vals = np.concatenate([wlen, wlen, seed_d[seeds]])
    graph = csr_matrix((vals, (rows_i, cols_i)), shape=(n + 1, n + 1))
    dist, pred = dijkstra(graph, directed=False, indices=n, return_predecessors=True)
    dist = dist[:n]
    inside = (pts[:, 0] > -0.5 * SPACING) & (pts[:, 0] < aspect + 0.5 * SPACING) & \
             (pts[:, 1] > -0.5 * SPACING) & (pts[:, 1] < 1 + 0.5 * SPACING)
    dn = np.clip((dist - dist.min()) / (dist[inside].max() - dist.min()), 0.0, 1.0)
    xs = np.linspace(0, 1, 512)
    t_of_p = FRONT_START + xs * (FRONT_END - FRONT_START)
    tau = np.interp(dn, _front_ease(xs), t_of_p)

    # keep every edge of the activation tree, thin out the rest for an organic look
    tree = {(min(int(pred[j]), j), max(int(pred[j]), j)) for j in range(n) if 0 <= pred[j] < n}
    keep = np.array([((int(u), int(v)) in tree) or rng.random() < 0.72 for u, v in es])
    es = es[keep]
    first = np.where(tau[es[:, 0]] <= tau[es[:, 1]], es[:, 0], es[:, 1])
    second = np.where(first == es[:, 0], es[:, 1], es[:, 0])
    end_e = tau[second] - 0.015
    start_e = np.minimum(tau[first] - EDGE_LEAD, end_e - 0.055)

    # ---- Voronoi cells at working resolution ---------------------------------------------
    ds = 2 if H >= 720 else 1
    ww, wh = W // ds, H // ds
    px = (np.arange(ww) + 0.5) * ds / H
    py = (np.arange(wh) + 0.5) * ds / H
    grid = np.stack(np.meshgrid(px, py), -1).reshape(-1, 2)
    d12, idx = cKDTree(pts).query(grid, k=2)
    owner = idx[:, 0]
    d1n = (d12[:, 0] / (0.62 * SPACING)).astype(np.float32)
    flip = (tau + FLIP_LAG).astype(np.float32)
    ft = (flip[owner] + IRIS * d1n).reshape(wh, ww).astype(np.float32)
    gap_px = (d12[:, 1] - d12[:, 0]) * H                     # ~2 x distance to cell border
    border = np.exp(-(gap_px / max(1.5, 2.6 * s)) ** 2).reshape(wh, ww).astype(np.float32)

    return dict(
        s=s, ds=ds, work=(ww, wh), pts_px=(pts * H).astype(np.float32), tau=tau.astype(np.float32),
        ei=first, ej=second, start_e=start_e.astype(np.float32), end_e=end_e.astype(np.float32),
        ft=ft, border=border, W=W, H=H,
    )


def _draw_light(g, t):
    """Return (core, glow_rgb_small, Q) light layers for time t."""
    W, H, s = g["W"], g["H"], g["s"]
    SS = 2
    Q = max(2, int(round(4 * s)))
    pts, tau = g["pts_px"], g["tau"]
    core = Image.new("L", (W * SS, H * SS), 0)
    dr = ImageDraw.Draw(core)
    bloom = Image.new("L", (W // Q + 1, H // Q + 1), 0)
    db = ImageDraw.Draw(bloom)

    # edges
    x = np.clip((t - g["start_e"]) / (g["end_e"] - g["start_e"]), 0.0, 1.0)
    prog = 1.0 - (1.0 - x) ** 2
    since = t - g["end_e"]
    bright = np.where(x < 1.0, 1.0, 0.20 + 0.80 * np.exp(-np.maximum(since, 0) / 0.09))
    vis = np.nonzero(x > 0)[0]
    vis = vis[np.argsort(bright[vis])]
    lw = max(2, int(round(1.5 * s * SS)))
    P0, P1 = pts[g["ei"]], pts[g["ej"]]
    for k in vis:
        p0 = P0[k] * SS
        p1 = p0 + (P1[k] * SS - p0) * prog[k]
        dr.line([float(p0[0]), float(p0[1]), float(p1[0]), float(p1[1])],
                fill=int(255 * 0.80 * bright[k]), width=lw)
    spark_r = 2.4 * s * SS
    for k in vis:
        if x[k] < 1.0:
            p0 = P0[k] * SS
            p1 = p0 + (P1[k] * SS - p0) * prog[k]
            dr.ellipse([p1[0] - spark_r, p1[1] - spark_r, p1[0] + spark_r, p1[1] + spark_r], fill=255)

    # nodes: dim dot as edges arrive, one pulse (flare + expanding ring) when they fire
    sec = t - tau
    appear = _smooth((t - (tau - EDGE_LEAD - 0.01)) / 0.05)
    u = np.clip(sec / PULSE_DUR, 0.0, 1.0)
    flare = np.where(sec >= 0, (1.0 - u) ** 2, 0.0)
    nb = appear * (0.30 + 0.45 * _smooth(sec / 0.03)) + 0.35 * flare
    for i in np.nonzero(appear > 0.02)[0]:
        cx, cy = pts[i] * SS
        r = (2.0 + 2.6 * flare[i]) * s * SS
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], fill=int(255 * min(1.0, nb[i])))
    ring_w = max(1, int(round(1.3 * s * SS)))
    for i in np.nonzero((sec > 0) & (sec < PULSE_DUR))[0]:
        cx, cy = pts[i] * SS
        e = 1.0 - (1.0 - u[i]) ** 2.2
        r = (5.0 + 30.0 * e) * s * SS
        al = (1.0 - u[i]) ** 1.4 * 0.75
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], outline=int(255 * al), width=ring_w)
        if flare[i] > 0.03:
            bx, by = pts[i] / Q
            br = (10.0 + 14.0 * e) * s / Q
            db.ellipse([bx - br, by - br, bx + br, by + br], fill=int(255 * 0.9 * flare[i]))

    core1 = core.reduce(SS)
    small = np.asarray(core1.reduce(Q), np.float32) / 255.0
    bl = np.asarray(bloom, np.float32) / 255.0
    bl = bl[: small.shape[0], : small.shape[1]]
    g1 = gaussian_filter(small, 5.0 * s / Q * 1.0 + 0.3)
    g2 = gaussian_filter(small * 0.8 + bl * 1.25, 16.0 * s / Q + 0.3)
    glow = g1[..., None] * (CYAN * 1.6) + g2[..., None] * (BLUE * 1.7)
    return np.asarray(core1, np.float32) / 255.0, glow, small.shape[::-1]


def render(a, b, t, state):
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    g = state.get("_neural_mesh")
    if g is None:
        g = state["_neural_mesh"] = _setup(state)
    W, H, s, ds = g["W"], g["H"], g["s"], g["ds"]

    # ---- reveal mask: cells open, soft-edged -----------------------------------------------
    m_raw = _smooth((t - g["ft"]) / FLIP_DUR)
    m_w = gaussian_filter(m_raw, 4.0 * s / ds + 0.4)
    band = 4.0 * m_w * (1.0 - m_w)
    m = _resize(m_w, (W, H)) if ds > 1 else m_w

    # ---- light ---------------------------------------------------------------------------
    fade = 1.0 - _smooth((t - FADE_START) / (FADE_END - FADE_START))
    fade *= float(_smooth(t / 0.05))
    core, glow, (gw, gh) = _draw_light(g, t)
    glow_full = np.stack([_resize(glow[..., c], (W, H)) for c in range(3)], -1)
    cell = (0.10 * band + 0.40 * g["border"] * band) * fade
    cell_full = _resize(cell, (W, H)) if ds > 1 else cell
    light = core[..., None] * WHITE * 1.0 + glow_full + cell_full[..., None] * CYAN
    light *= fade
    np.clip(light, 0.0, 1.0, out=light)

    # ---- composite: a -> b by mask, then screen the light on top -----------------------------
    af = a.astype(np.float32)
    base = af + (b.astype(np.float32) - af) * m[..., None]
    base *= 1.0 / 255.0
    out = 1.0 - (1.0 - base) * (1.0 - light)
    return (out * 255.0 + 0.5).astype(np.uint8)
