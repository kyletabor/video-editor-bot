# contract/ — the edit plan

The only interface between `bot/` (Kyle's lane, produces a plan) and `render/`
(Ramsey's lane, consumes it). Shared ownership: change only via a PR reviewed
by the other side.

- [`edit-plan.schema.json`](edit-plan.schema.json) — JSON Schema 2020-12, the
  source of truth. Currently **v1.3**: v1 plus an optional reel, music, transitions (built in or drawn by a Python module) and opening/closing cards (see below).
- [`examples/one-clip-trim.json`](examples/one-clip-trim.json) — minimum viable
  plan: one clip, one segment, embedded captions, 16:9.
- [`examples/two-clips-concat-vertical.json`](examples/two-clips-concat-vertical.json)
  — two clips, non-contiguous segments concatenated, sidecar SRT, 9:16 crop,
  companion summary.
- [`examples/reel-with-cards.json`](examples/reel-with-cards.json) — v1.1: two
  clips assembled into one summary video with an intro slide, one chapter card
  and an outro.
- [`examples/reel-with-module-transition.json`](examples/reel-with-module-transition.json)
  — v1.3: the same reel with every join drawn by
  [`examples/transitions/wipe.py`](examples/transitions/wipe.py).

## Mental model

```
source ──▶ clips[] ──▶ one output file per clip
             │
             ├─ segments[]  keep-list of {start,end} seconds, concatenated in order
             │
             └─ (v1.1) output.reel ──▶ ONE more file: [intro] + Σ([card] + clip) + [outro]
```

Everything the renderer needs is in the plan. The renderer never reads the
transcript, never decides what is interesting, and never edits outside the
listed segments. The bot never touches ffmpeg.

## Reel (v1.1)

A reel is the product Kyle asked for: a 2.5 to 5 minute summary video of a long
session, built from several moments in chronological order, opened by an intro
slide, with sparse explainer slides between sections.

- `output.reel` (optional) turns it on. `filename` defaults to `reel.mp4` inside
  `output.dir`. `intro` and `outro` are optional cards. `chapter_cards` is
  `auto` (card only where a clip carries its own `card`), `all` (every clip gets
  one, synthesized from `takeaway` when missing) or `none`.
- `clips[].card` (optional) is the explainer slide shown right before that clip.
- A **card** is `{title (≤80), lines[] (≤4 × ≤120), seconds (1–10, default 3)}`.
  The renderer draws it full-frame (dark background, title, lines, a small
  footer "k of N · source timestamp"); no image files cross the contract.
- Timeline: `[intro] + for each clip in order: [card] + clip + [outro]`. Hard
  cuts only. Transitions, music and effects are future fields (veb-t2b.7,
  Ramsey's lane) and must not change this base timeline.
- The reel is normalized to one resolution, frame rate, sample rate and channel
  layout (the first clip's, or the preset's). Cards carry silent audio so the
  audio track is continuous.
- Individual clip files are still written exactly as in v1.
- Total length is the bot's responsibility (`clipbot reel --minutes N` targets N
  ±20 % including cards). The renderer warns above 10 minutes, never rejects.

### v1.2 additions (2026-09-26, from Kyle's review of the first reel)

- `output.reel.opening[]` (≤2 cards) after the intro, e.g. "What you'll learn";
  `output.reel.closing[]` (≤3 cards) before the outro: the summary at the end.
  Timeline becomes `[intro] + opening + Σ([card] + clip) + closing + [outro]`.
- `output.reel.music {path, under, gain_db, duck_db, fade_seconds, loop}`: a bed
  that plays under every card run (`under: cards`, default) faded at the run
  edges, or under everything ducked below speech (`under: all`). The file must
  be public domain or licensed for the project; `assets/music/LICENSE` records
  provenance for anything checked in.
- `output.reel.transition {kind: cut|dip|dissolve, seconds}` at every join.
- `output.reel.audio_fade_seconds` (default 0.15): clip audio fades in and out
  at each cut so speech never starts or stops abruptly.
- Example: [`examples/reel-with-music.json`](examples/reel-with-music.json).
  The base timeline and all v1.1 fields are unchanged; a renderer that ignores
  v1.2 fields still produces a valid v1.1 reel.

### v1.3 addition: code-drawn transitions (2026-10-01)

- `output.reel.transition {kind: module, module: <path>.py, seconds}`: every join is
  drawn by a Python file. `module` is repo-root-relative or absolute, and is required
  exactly when `kind` is `module` (and rejected with any other kind).
- Timing is a dissolve's: the two neighbours overlap by `seconds` (0.1 to 1.5, in whole
  frames, shortened when a segment cannot hold it) and the audio cross-fades. Only the
  picture differs.
- The file exposes `render(a, b, t, state)`:
  - `a`, `b`: HxWx3 uint8 RGB numpy arrays, the outgoing and the incoming frame at the
    same instant (both keep moving).
  - `t`: 0.0 on the first frame of the join, 1.0 on the last; 0.5 when the join is a
    single frame. Return `a` at 0 and `b` at 1, or the join will pop.
  - `state`: a dict that lives for one join, pre-filled with `size` (W, H), `fps`,
    `n_frames`, `seed` (7) and `frame_index`. Precompute into it on the first call; use
    `seed` for every random choice so a reel renders the same twice.
  - Returns an HxWx3 uint8 RGB array. Anything else, or an exception, fails the reel
    with the module's path and the frame index.
- A `SECONDS` constant in the file is advisory; the plan's `seconds` decides.
- The module may import numpy, scipy, Pillow and the standard library. numpy and scipy
  are an optional extra of the renderer:
  `uv run --project render --extra styles cliprender plan.json`.
- A module is code the renderer runs with the operator's rights. Name only files you
  would run yourself; the bot must never take a module path from untrusted input.
- Example: [`examples/reel-with-module-transition.json`](examples/reel-with-module-transition.json).

## Renderer obligations (what `render/` must do)

1. Validate against the schema first. Invalid plan → exit non-zero, print the
   schema error, write nothing.
2. Reject if `version != "1"`, if any `segment.end <= segment.start`, if any
   `segment.end > source.duration_seconds` (when given), or if two clips share
   an `id`.
3. Write `output.dir/<clip.id>.mp4` for every clip. Partial success is a
   failure: if clip 3 of 3 fails, exit non-zero and say which one.
4. Honour `output.aspect`, `output.captions`, `output.max_height`. Never
   upscale.
5. `trim_silence` may shave up to 0.5 s at segment edges. Never cut speech.
6. Warn (stderr) when a clip's kept duration falls outside the preset's bounds
   (see `bot/research/clip-guidelines.md`). Do not reject.
7. Print one line per written file on stdout: `<clip.id>\t<path>\t<seconds>`.
8. (v1.1) When `output.reel` is present, also write the reel as described
   above, verify it like a clip (decode, streams, duration = sum of parts), and
   print one more line: `reel\t<path>\t<seconds>`. A reel failure publishes
   nothing from the run.

## Bot obligations (what `bot/` must do)

1. Emit a plan that validates. Run the validator before handing off.
2. Order `segments` so the payoff comes first (`hook_offset_seconds` stays 0).
3. One idea per clip; `takeaway` is that idea in one sentence.
4. Use `source.captions.kind = embedded` when the source has a subtitle
   stream, else write an SRT and point at it, else `none`.
5. (v1.1) For a reel: moments in chronological order, spread across the whole
   session, each clip opening and closing on a sentence boundary; total length
   within ±20 % of the requested minutes including cards.

## Validating a plan

```bash
uv run --with jsonschema python -c "
import json, jsonschema, sys
schema = json.load(open('contract/edit-plan.schema.json'))
for p in sys.argv[1:]:
    jsonschema.validate(json.load(open(p)), schema); print('ok', p)
" contract/examples/*.json
```

`uv run --locked scripts/check.py` runs this over `contract/examples/` plus a
set of plans that must be rejected.

## Presets

| preset | aspect | length bounds (warn) | intent |
|---|---|---|---|
| `internal` | 16:9 | 15–120 s | Slack / team share, keep the screen |
| `linkedin` | 16:9 | 15–90 s | Feed post |
| `shorts` | 9:16 | 15–60 s | YouTube Shorts / Reels |
| `email` | 16:9 | 15–60 s | Embedded in a mail with a text takeaway |

## Changing this contract

Bump `version` only for breaking changes. Add optional fields freely, with
defaults, and update the examples. The other lane reviews the PR.

Changelog
- v1.3 (2026-10-01, veb-iyl): `output.reel.transition.kind: module` with `transition.module`,
  a Python file that draws every join. Backward compatible.
- v1.2 (2026-09-26, Kyle's agent): `output.reel.{opening,closing,music,transition,audio_fade_seconds}`. Backward compatible.
- v1.1 (2026-09-25, Kyle's agent under Kyle's authority): `output.reel`,
  `clips[].card`, `$defs.card`. Backward compatible.
- v1 (2026-09-25): initial.
