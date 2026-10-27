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
| Render an existing or hand-edited plan | `uv run --project render cliprender out/<name>/plan.json --root .` (`--overwrite` to replace earlier outputs; add `--theme out/<name>/plan.theme.json` when that file is there) |
| Music, transitions, "make it sound like ..." | add `--style <name>` to `clipbot reel` (`clipbot styles` lists them); a new sound or transition is the `clipbot-style` skill |
| Day One AI's "Art of Intelligence" look (its title card, chapter counter, navy captions) | add `--style aoi --theme <theme.json>` to `clipbot reel`, with a theme file `{"theme": "aoi", "episode": "04", "date": "Friday, October 9"}` (or `--theme aoi` without the episode chip and date) |

- `--minutes N` targets N minutes ±20 % including cards (a 4 s intro and a 3 s
  chapter card per clip). 2.5–5 minutes suits a 1–2 hour session; the sample
  `assets/demo-clip.mp4` is 72 s, so use `--minutes 0.5` there.
- `--transcribe` uses faster-whisper on the CPU, about one sixth of the
  recording's length (an hour of video takes roughly 10 minutes). Tell the user
  before starting. It saves an `.srt` next to the outputs and reuses it next time.
  A sidecar transcript the user already has goes in with `--srt <file>`.
- A Pipeline AI Talks video always gets the Pipeline music: `--style pipeline` (the series
  theme), or `--style aoi` when the user asks for the Art of Intelligence look (the same music
  with the brand's horizon wipe). For anything else, offer the styles once and keep the answer
  in the framing file (`"style": "<name>"`). Music plays under the cards only; after
  hand-editing card seconds in a plan that uses a cue style, run
  `clipbot score out/<name>/plan.json --style <name>` before rendering.
- A theme (`--theme`) is the renderer's look for the cards, captions and labels; the plan
  does not change. Use one only when the user asks for that look: today that is `aoi`, Day
  One AI's Art of Intelligence on-air package, together with `--style aoi` (the look's join,
  on the Pipeline music). Put the episode number (up to 4 characters) and the on-air date (up
  to 40) in a theme file, or as `"theme": {...}` in the framing file. clipbot writes it next
  to the plan as `plan.theme.json`, and `--render` passes it on.
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
- **End on the point, not on the setup.** Before you fix an `end`, read the
  outline past it. If the last words are a lead-in ("and the cool thing is…",
  "here's the thing", "what I love about it is", "for example", "let me show
  you", a sentence ending in "and" or "the"), the payoff is in the next sentence:
  move `end` past it, or pick an earlier end that closes a finished point. A
  sentence boundary is not enough: "And the cool thing is." is a sentence to the
  snapper and a cliffhanger to the viewer (Kyle on the Talk #4 reel, 2026-10-19:
  clips "just got cut off" right before what "sounded like was gonna be a great
  sound bite"). `clipbot` carries such an end on by itself
  (`reel.finish_thought`) to the first finished sentence after it by the same
  speaker (skipping asides like "I don't know how else to show this"), within
  30 s. That keeps clips short, so a payoff that runs several sentences still
  needs your `end` past it; and when someone else answers or nothing finishes
  within 30 s the end is left alone. The check is still yours.
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

5. Read the last sentence of every clip (the storyboard shows it, or the
   captions at each clip's end). Each one must finish a point. A clip that stops
   on a setup ("the cool thing is…", "here's what happened…", a question the
   speaker is about to answer) gets its `end` moved past the payoff in
   `moments.json`, then re-run.

Show the user the cut list (id, `start–end`, takeaway) and the total length. An
explicit "make the reel" already authorizes rendering; ask first only when the
request was ambiguous or the list looks wrong (a 5-second clip, a moment from the
intro chatter, two clips saying the same thing).

## Blurring shared screens

Screens in a reel are there so viewers see how the tool works: blur the
private **words**, never the whole screen ([docs/redaction.md](../../../docs/redaction.md)).

- Use `--redact` (auto) plus a `--redact-terms` file of the people, clients,
  companies, projects and places on screen, OCR misreadings included. Start
  from the last talk's terms file.
- Hide: real people other than the presenters, customer and client names,
  money, personal details (a child, a personal inbox, health, home, town),
  e-mail addresses, phones, keys, and links or IDs that point at one record.
- Keep: the presenters' bots, their names and avatars, generic task and chat
  text ("Submit marketing by Friday"), product names, menus, the app's layout.
- Hand-drawn boxes are word- or line-sized. The run refuses boxes covering
  more than 40% of the frame; pass `--allow-large-blur` only for a screen
  that is private edge to edge, and say so in the report.
- Then run `clipbot check-redaction` and look at every distinct screen
  zoomed in: each sensitive word is gone, and the screen is still readable.

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

- Paths: `reel.mp4`, each clip, `summary.md`, `plan.json`, `moments.json` (and
  `plan.theme.json` when the reel has a theme).
- The moments chosen: timestamp, length, takeaway, in order, plus the total.
- Warnings from stderr (length outside the preset, fallback framing) in plain words.
- What you did not verify (you did not watch it; speech and lip sync are for a human).
- How to adjust: edit `moments.json` and re-run with `--moments`; change
  `--minutes`; add a `--title`.

## Exit codes and common errors

| Symptom | Meaning / what to do |
|---|---|
| `clipbot` exits 1 with `no captions in the source` | Add `--srt <file>` or `--transcribe` (with `--extra whisper`). |
| `clipbot` exits 3: nothing matched | Widen the request wording or the `--min-seconds`/`--max-seconds` bounds. |
| `clipbot` exits 1 / `cliprender` exits 1 | `file not found: <path>`, an ffmpeg error or an invalid plan; the message names it. |
| `ffmpeg not found on PATH` | Run the pinned installer, or `brew install ffmpeg` on macOS. |
| `install with: uv run --project bot --extra whisper …` | The transcription extra is not installed; use that command form. |
| `clipbot` exits 1 with `unknown theme` / `cannot read theme file` | A typo in `--theme` or a broken theme file; nothing ran yet. `cliprender` exits 1 with `Theme aoi: unknown key(s)` for a key the theme does not take (aoi takes `episode` and `date`). |

Check the whole setup with `uv run --locked scripts/check.py` (`check: OK`).
