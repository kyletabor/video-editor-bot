---
name: clipbot
description: Drive video-editor-bot's local CLI (clipbot and cliprender) for a user. Turn a long recording on disk into a summary reel of its key moments, outline it and hand-pick moments, cut one clip for a request, or write the Markdown executive summary and transcript. Use when a user has a recording and asks for a reel, highlights, a summary video, an outline, chapters, a transcript or a clip.
---

# clipbot: reels, clips and summaries from a recording

You are the interface; there is no web page. `clipbot` (`bot/`) turns a recording
plus a request into a validated edit plan and a Markdown summary. `cliprender`
(`render/`) turns the plan into files. Everything runs locally from the repository
root with `uv`; nothing is uploaded. The plan format in `contract/` is the only
thing that crosses between the two, so every edit you make happens in the plan
(or the moments file), never in ffmpeg by hand.

## Before the first command

- Confirm the file exists, then probe it: `ffprobe -hide_banner <file>`. Note the
  duration and whether a `Subtitle` stream is listed (embedded captions). No
  subtitle stream and no `.srt` next to it means you will need `--transcribe`.
- FFmpeg: the pinned build in `.tools/ffmpeg/` (installed by
  `uv run --python 3.12 python scripts/install_ffmpeg.py`) is picked up by
  `clipbot` automatically. When you run `cliprender` yourself, put that directory
  first on `PATH` or pass `--ffmpeg .tools/ffmpeg/ffmpeg --ffprobe .tools/ffmpeg/ffprobe`.
- Pick an output name: everything goes to `out/<name>/`. Never write into
  `assets/` or next to the source, and never overwrite the source.
- Windows PowerShell 5.1 cannot chain with `&&`; run one command per line.

## Which command for which request

| The user asks for | Run (from the repository root) |
|---|---|
| A summary video, reel or highlights of a session | `uv run --project bot clipbot reel --source <file> --minutes 4 --render --title "<title>" --out out/<name>/plan.json` |
| The same, but the file has no captions | `uv run --project bot --extra whisper clipbot reel --transcribe --source <file> --minutes 4 --render --out out/<name>/plan.json` |
| "What's in this recording", chapters, an outline | `uv run --project bot clipbot outline --source <file> --out out/<name>/outline.md` |
| To choose the moments themselves | outline, then a `moments.json` (below), then `clipbot reel --moments out/<name>/moments.json --source <file> --render --out out/<name>/plan.json` |
| One clip: "the part where …" | `uv run --project bot clipbot plan --source <file> --request "<what it is about>" --summary --out out/<name>/plan.json`, then render |
| Only the summary and transcript | `uv run --project bot clipbot summarize --source <file> --title "<title>" --out out/<name>/summary.md` |
| Render an existing or hand-edited plan | `uv run --project render cliprender out/<name>/plan.json --root .` (`--overwrite` to replace earlier outputs) |

- `--minutes N` targets N minutes ±20 % including cards (a 4 s intro and a 3 s
  chapter card per clip). 2.5–5 minutes suits a 1–2 hour session; the sample
  `assets/demo-clip.mp4` is 72 s, so use `--minutes 0.5` there.
- `--transcribe` uses faster-whisper on the CPU, about one sixth of the
  recording's length (an hour of video takes roughly 10 minutes). Tell the user
  before starting. It saves an `.srt` next to the outputs and reuses it next time.
  A sidecar transcript the user already has goes in with `--srt <file>`.
- `--llm` asks the Anthropic API to pick the moments when `ANTHROPIC_API_KEY` is
  set (optional extra `llm`); otherwise the keyword-and-density heuristic runs and
  says so. Do not promise LLM selection without the key.
- Single clips take `--preset internal|linkedin|shorts|email` (bounds 15–120,
  15–90, 15–60, 15–60 s; `shorts` crops to 9:16). Keep 16:9 whenever the screen
  matters: slides, code, terminals, grids.
- Speaker names from a "Notes by Gemini" transcript, when the captions have none:
  `--speakers <notes.txt> --speakers-offset <h:mm:ss>` on `outline` and `reel`.

## Reading an outline and hand-picking moments

`outline.md` has a header (title, source, duration, cue count), `## Transcript` in
30-second blocks (`[h:mm:ss] Speaker: text …`), and `## Moments skeleton`: a JSON
template in the `--moments` shape, also printed to the terminal.

Show the user the outline in their terms (what happens when, who speaks), then
build `moments.json`, a top-level list in the order the reel should play:

```json
[
  {"start": "0:12:30", "end": "0:13:12",
   "title": "Why the contract is the only interface",
   "lines": ["bot/ writes the plan", "render/ executes it"],
   "why": "the decision the rest of the session builds on"}
]
```

- `start`/`end`: seconds or clock strings copied from the outline; they snap to
  caption-cue boundaries, so aim at sentence starts and ends.
- `title` (optional, ≤ 80 chars) becomes the chapter card title and the clip's
  takeaway. `lines` (optional, ≤ 4 × ≤ 120 chars) are the card's text; `why`
  (optional) is used as the single card line when `lines` is absent.
- Choose like an editor: one idea per moment, chronological, spread across the
  whole session, 20–60 s each, payoff first, no greetings or "can you see my
  screen". Total ≈ target minutes minus cards.
- Every `clipbot reel` run writes its own choices to `out/<name>/moments.json`.
  The fastest revision loop is: run, edit that file with the user, re-run with
  `--moments`.

## Review the plan before rendering

Open `out/<name>/plan.json` and check, in this order:

1. `clipbot` validated it against `contract/edit-plan.schema.json`; after any hand
   edit, re-validate with the snippet in `contract/README.md`.
2. Clips are chronological, one idea each, every segment inside
   `source.duration_seconds`, `end > start`, unique ids.
3. For a reel: `output.reel` is present; card titles ≤ 80 chars and ≤ 4 lines of
   ≤ 120 chars; total of segments plus cards within ±20 % of the target.
4. `source.captions.kind` matches reality (`embedded`, `srt` with a path, or `none`).

Show the user the cut list (id, `start–end`, takeaway) and the total length. An
explicit "make the reel" already authorizes rendering; ask first only when the
request was ambiguous or the list looks wrong (a 5-second clip, a moment from the
intro chatter, two clips saying the same thing).

## Render and verify

- `--render` (or `cliprender` directly) prints one `<id>\t<path>\t<seconds>` row
  per file and a final `reel\t<path>\t<seconds>` row. Exit 0 means every file was
  decoded and verified; a failure names the clip and publishes nothing from that
  run; 130 is Ctrl-C. Existing outputs need `--overwrite`.
- Confirm with `ffprobe` that the reel and clips exist and their durations match
  the rows; the reel is the clips plus cards, in order.
- Never claim a render without the output file. Never "fix" a cut with ffmpeg
  directly: change the plan or the moments file and re-render.

## Report to the user

- Paths: `reel.mp4`, each clip, `summary.md`, `plan.json`, `moments.json`.
- The moments chosen: timestamp, length, takeaway, in order, plus the total.
- Warnings from stderr (length outside the preset, fallback framing) in plain words.
- What you did not verify (you did not watch it; speech and lip sync are for a human).
- How to adjust: edit `moments.json` and re-run with `--moments`; change
  `--minutes`; add a `--title`.

## Exit codes and common errors

| Symptom | Meaning / what to do |
|---|---|
| `clipbot` exits 2: no captions and no `--srt` | Add `--srt <file>` or `--transcribe` (with `--extra whisper`). |
| `clipbot` exits 3: nothing matched | Widen the request wording or the `--min-seconds`/`--max-seconds` bounds. |
| `clipbot` exits 1 / `cliprender` exits 1 | Missing file, ffmpeg error or an invalid plan; the message names it. |
| `ffmpeg not found on PATH` | Run the pinned installer, or `brew install ffmpeg` on macOS. |
| `install with: uv run --project bot --extra whisper …` | The transcription extra is not installed; use that command form. |

Check the whole setup with `uv run --locked scripts/check.py` (`check: OK`).
