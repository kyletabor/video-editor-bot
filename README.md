# video-editor-bot

Turns a long recorded session (a 1–2 hour Meet/Zoom talk, workshop or demo) into
a short **summary reel** of its key moments — an intro slide, a handful of clips in
chronological order with sparse explainer cards between them — plus a written
**executive summary and transcript** in Markdown. It was built live on stage by two
AI coding agents from different vendors, working in one repo under two humans: see
the [retrospective](https://claude.ai/artifact/NewJDksbu4ysz3kSCrs6xs) and the
[timeline](https://claude.ai/artifact/4nMDqZgLjo1N7izvRGyXC3).

## Quickstart

### 1. Prerequisites (Windows, macOS, Linux including ARM)

| Need | Get it | Check |
|---|---|---|
| git | [git-scm.com](https://git-scm.com/) | `git --version` |
| uv | [docs.astral.sh/uv](https://docs.astral.sh/uv/) (it fetches a Python for you) | `uv --version` |
| FFmpeg + ffprobe, 4.4 or newer | below | `ffmpeg -version`, `ffprobe -version` |

Recommended FFmpeg: the pinned build the project is tested against (7.0.2, SHA-256
verified, Windows x64 and Linux x64/ARM). From a fresh clone:

```bash
git clone https://github.com/kyletabor/video-editor-bot.git
cd video-editor-bot
uv run --python 3.12 python scripts/install_ffmpeg.py
```

It lands in `.tools/ffmpeg/`, and `clipbot` and the check gate use it from there
on their own. Only when you call `cliprender` directly does it need to be on
`PATH` — Bash: `export PATH="$PWD/.tools/ffmpeg:$PATH"`; PowerShell:
`$env:PATH = "$(Resolve-Path .tools/ffmpeg);$env:PATH"` — or pass
`--ffmpeg .tools/ffmpeg/ffmpeg --ffprobe .tools/ffmpeg/ffprobe`. On macOS there is
no pinned build; `brew install ffmpeg` is fine. A system FFmpeg 4.4 (Ubuntu 22.04)
renders correctly too, but five renderer edge-case tests need 7.x, so prefer the
pinned build.

### 2. Make a reel from the sample clip

One command, from the repository root (`uv` builds the virtualenv on first run):

```bash
uv run --project bot clipbot reel --source assets/demo-clip.mp4 --minutes 0.5 --render --out out/demo/plan.json
```

About a minute on an 8-core ARM box. Outputs in `out/demo/`:

| File | What it is |
|---|---|
| `reel.mp4` | the summary video: intro card, clips in order, a card before each section |
| `clip-01-….mp4`, … | every moment as its own file, for Slack, a post or an email |
| `summary.md` | executive summary, clip list, full transcript |
| `plan.json` | the edit plan the renderer executed (see *How it works*) |
| `moments.json` | the moments it chose, in the `--moments` format: edit and re-run (step 4) |

### 3. Your own recording

```bash
uv run --project bot clipbot reel --source path/to/session.mp4 --minutes 4 --render --title "Q3 architecture review" --out out/q3-review/plan.json
```

- `--minutes 4` targets a 4-minute reel (±20 %, cards included). 2.5–5 minutes
  suits a 1–2 hour session. `--title` and `--date` fill the intro card; they
  default to the file's name and date.
- No captions in the file? (Meet and Zoom exports usually carry them;
  `ffprobe path/to/session.mp4` lists a subtitle stream if so.) Add `--transcribe`
  and run through the optional `whisper` extra:
  `uv run --project bot --extra whisper clipbot reel --transcribe --source path/to/session.mp4 --minutes 4 --render --out out/q3-review/plan.json`.
  Transcription runs locally on the CPU and costs about one sixth of the
  recording's length — roughly 10 minutes for an hour of video. The transcript is
  saved as an `.srt` next to the outputs and reused on later runs.
- Leave off `--render` to get only `plan.json`, `moments.json` and `summary.md`.
  Review or hand-edit the plan, then render it:
  `uv run --project render cliprender out/q3-review/plan.json --root .`

### 4. Pick the moments yourself

```bash
uv run --project bot clipbot outline --source path/to/session.mp4 --out out/q3-review/outline.md
```

`outline.md` is the session as a table of contents: the transcript in 30-second
blocks with speaker names, then a *Moments skeleton* — a JSON template in the
`--moments` format (also printed to the terminal). Copy it to `moments.json`, fill
in the moments you want, and build the reel from those instead of the automatic
pick:

```json
[
  {"start": "12:30", "end": "13:12", "title": "Why the contract is the only interface",
   "lines": ["bot/ writes the plan, render/ executes it"]}
]
```

```bash
uv run --project bot clipbot reel --source path/to/session.mp4 --moments moments.json --render --out out/q3-review/plan.json
```

`start` and `end` are seconds or `h:mm:ss` times copied from the outline; they
snap to caption boundaries. `title` (up to 80 characters) becomes the chapter
card and the clip's takeaway; `lines` (up to 4) are the card's text. Every
`clipbot reel` run also writes its own choices to `moments.json` next to
`plan.json`, so the quickest edit loop is: run, tweak that file, re-run with
`--moments`.

### One clip, or just the summary

`clipbot plan --request "…"` finds the single best 15–120 s window for a request
and writes a one-clip plan; `clipbot summarize` writes the Markdown summary alone.

```bash
uv run --project bot clipbot plan --source assets/demo-clip.mp4 --request "the part where I question whether the clip bot will work" --summary --out out/one-clip/plan.json
uv run --project render cliprender out/one-clip/plan.json --root .
uv run --project bot clipbot summarize --source assets/demo-clip.mp4 --out out/one-clip/summary.md
```

Driving it from an AI agent (Claude Code, Codex, Cursor)? The
[clipbot skill](.agents/skills/clipbot/SKILL.md) tells it which command answers
which request and how to check the result.

## How it works

```text
recording.mp4 ──▶ transcript ──▶ moments ──▶ edit plan ──▶ renderer ──▶ reel.mp4 + clips + summary.md
                  captions in the   key moments,    contract/          cliprender: ffmpeg
                  file, a sidecar   chronological,  edit-plan.schema   cuts, burns captions,
                  .srt or           sentence-       .json (seconds,    draws cards, joins
                  --transcribe      aligned         cards, outputs)    the pieces
```

`bot/` (`clipbot`) reads the transcript, picks the moments and writes the plan; it
never cuts a frame. `render/` (`cliprender`) executes the plan and never decides
what to keep. The **edit plan in [`contract/`](contract/README.md)** is the only
thing that crosses between them: a JSON file naming the source, the segments to
keep in seconds, the cards to draw and where the outputs go. That boundary is what
let two agents from different vendors build the two halves in parallel without
talking to each other, and it is why you can hand-edit `plan.json` — nudge a cut,
rewrite a card — and re-render.

## Verify your setup

```bash
uv run --locked scripts/check.py
```

Prints `check: OK` after validating the contract examples, running the bot and
renderer tests and rendering a generated test pattern with your FFmpeg. Same
command on every OS (`make check` is an alias). Details, per-check runs and the
`--require-pinned` flag: [docs/checks.md](docs/checks.md).

## Status

| Works today | Next |
|---|---|
| Summary reel from a captioned or transcribed recording (`clipbot reel`) | Filler-word and long-pause removal inside clips |
| Session outline and hand-picked moments (`clipbot outline`, `--moments`) | LLM moment selection by default (today: keyword and density scoring; `--llm` opt-in via the Anthropic API) |
| One-clip plans for a request; presets `internal`, `linkedin`, `shorts`, `email` | PDF export of the summary |
| Frame-accurate cuts, burned-in captions, intro/chapter/outro cards, 16:9 or 9:16 | Transitions and music between sections |
| Executive summary and transcript in Markdown | |
| Windows, macOS, Linux/ARM; CI runs the gate on Ubuntu and Windows | |

## Contributing

House rules for humans — lanes, beads, the PR protocol, Windows setup — are in
[CONTRIBUTING.md](CONTRIBUTING.md); the rules the agents follow are in
[AGENTS.md](AGENTS.md).
