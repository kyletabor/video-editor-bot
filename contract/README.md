# contract/ — the edit plan

The only interface between `bot/` (Kyle's lane, produces a plan) and `render/`
(Ramsey's lane, consumes it). Shared ownership: change only via a PR reviewed
by the other side.

- [`edit-plan.schema.json`](edit-plan.schema.json) — JSON Schema 2020-12, the
  source of truth.
- [`examples/one-clip-trim.json`](examples/one-clip-trim.json) — minimum viable
  plan: one clip, one segment, embedded captions, 16:9.
- [`examples/two-clips-concat-vertical.json`](examples/two-clips-concat-vertical.json)
  — two clips, non-contiguous segments concatenated, sidecar SRT, 9:16 crop,
  companion summary.

## Mental model

```
source ──▶ clips[] ──▶ one output file per clip
             │
             └─ segments[]  keep-list of {start,end} seconds, concatenated in order
```

Everything the renderer needs is in the plan. The renderer never reads the
transcript, never decides what is interesting, and never edits outside the
listed segments. The bot never touches ffmpeg.

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

## Bot obligations (what `bot/` must do)

1. Emit a plan that validates. Run the validator before handing off.
2. Order `segments` so the payoff comes first (`hook_offset_seconds` stays 0).
3. One idea per clip; `takeaway` is that idea in one sentence.
4. Use `source.captions.kind = embedded` when the source has a subtitle
   stream, else write an SRT and point at it, else `none`.

## Validating a plan

```bash
uv run --with jsonschema python -c "
import json, jsonschema, sys
schema = json.load(open('contract/edit-plan.schema.json'))
for p in sys.argv[1:]:
    jsonschema.validate(json.load(open(p)), schema); print('ok', p)
" contract/examples/*.json
```

`make check` (`veb-uv0`) will run this over `contract/examples/`.

## Presets

| preset | aspect | length bounds (warn) | intent |
|---|---|---|---|
| `internal` | 16:9 | 15–120 s | Slack / team share, keep the screen |
| `linkedin` | 16:9 | 15–90 s | Feed post |
| `shorts` | 9:16 | 15–60 s | YouTube Shorts / Reels |
| `email` | 16:9 | 15–60 s | Embedded in a mail with a text takeaway |

## Changing this contract

Bump `version` only for breaking changes. Add optional fields freely, with
defaults, and update both examples. The other lane reviews the PR.
