# Brief for one transition agent

Fill in the bracketed parts. One agent per transition; give each ONE distinct concept.

---

Write one code-drawn video transition as a single Python file. You draw every frame with
numpy; no video model is involved.

Read the docstring of `scripts/style_lab.py`: it is the contract (NAME, TITLE, SECONDS,
DESCRIPTION and `render(a, b, t, state)` returning an RGB uint8 frame).
`assets/styles/transitions/plain_dissolve.py` is the minimal example.

CONTEXT: it joins title cards and talking clips in [WHAT THE REEL IS]. The footage is [e.g.
dark screen recordings with a small speaker tile; dark cards with white text], so [e.g. a
transition only reads if it brings its own light]. The feel: [THE STYLE'S FEEL, and what to
avoid, e.g. no full-frame flashes, no strobing, no broken-video glitches]. Palette for
anything you add: [COLOURS].

YOUR CONCEPT, "[name]" (file `assets/styles/transitions/[file].py`): [three or four sentences
on what moves, what reveals the incoming picture, and what the leading edge looks like].
SECONDS between [0.8] and [1.3].

QUALITY BAR:
- At t = 0.5 both pictures are clearly present and the frame looks designed.
- Eased motion, a clear leading edge, anti-aliased masks (float masks with soft falloff).
- Resolution independent: scale every pixel size by H / 1080.
- Deterministic from `state["seed"]`; precompute into `state` on the first call.
- t = 0 returns `a` unchanged, t = 1 returns `b` unchanged.
- Under 1.0 s per frame on average at 1920x1080: float32, precomputed grids, no Python loops
  over pixels.

HOW TO WORK (RUN = `uv run --python 3.12 --with numpy --with scipy --with pillow python scripts/style_lab.py`):
1. `RUN sheet <file> --a [A.mp4] --b [B.mp4] --out out/lab/<name>.png`, then OPEN the PNG and
   criticise it against the quality bar. Do it for a card-to-clip pair and a clip-to-card
   pair. Iterate at least twice after the first working version.
2. `RUN check <file> --a [A.mp4] --b [B.mp4]` until it prints ALL PASS.

RULES: write only your transition file and files under out/. Only numpy, scipy, Pillow and
the standard library. No git.

RETURN, under 200 words: the file, SECONDS, the PASS lines verbatim, what it looks like in two
sentences, and what you would improve.
