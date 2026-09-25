# bot/ — clipbot (Kyle's lane)

Turns a recording into a validated **edit plan** (`contract/`). The renderer
(`render/`, Ramsey's lane) turns the plan into video; nothing else crosses the
line. Everything here runs on Windows, macOS and Linux (incl. ARM) with
Python 3.10+, `uv`, and ffmpeg/ffprobe 4.4+ on PATH (or the pinned build in
`.tools/ffmpeg/`, which clipbot prefers when present).

```bash
uv run --project bot clipbot reel    --source talk.mp4 --minutes 4 --title "Talk #2" --out out/talk/plan.json [--render]
uv run --project bot clipbot outline --source talk.mp4 --out out/talk/outline.md
uv run --project bot clipbot plan    --source talk.mp4 --request "the part about beads" --out out/talk/plan.json
uv run --project bot clipbot summarize --source talk.mp4 --out out/talk/summary.md
```

## Why a reel

The product goal is a 2.5–5 minute *summary* of a 1–2 hour session: several
key moments in chronological order, an intro slide, a short explainer card
before each moment, plus a markdown executive summary and transcript. One
16-second clip is not that. `clipbot reel` writes, next to the plan:

- `plan.json` — contract v1.1 (`output.reel` + `clips[].card`), validated.
- `moments.json` — the chosen moments in the `--moments` shape (below), so a
  person can edit them and re-run with `--moments moments.json`.
- `summary.md` — executive summary, a **Reel** section listing the chapters
  with source timestamps, and the full transcript.
- `<source>.srt` when `--transcribe` was used (reused on the next run).

## How moments are chosen

1. **`--moments FILE`** — you decide. A JSON list of
   `{"start", "end", "title", "lines", "why"}`; `start`/`end` in seconds or
   `h:mm:ss` (copy them from the outline). Spans are snapped outwards to caption
   cue boundaries so clips never open or close mid-caption; order is kept.
   `title` (≤ 80) is the card title and clip takeaway; `lines` (≤ 4 × ≤ 120) the
   card body; `why` is the single card line when `lines` is absent.
2. **`--llm`** — Claude reads the outline in ≤ 8k-token chunks and proposes
   moments as structured JSON (`clipbot/llm.py`). Needs `ANTHROPIC_API_KEY` and
   `uv run --project bot --extra llm ...`; without either it says so on one line
   and uses the heuristic. Any API error does the same: a reel is never blocked
   on the network.
3. **Heuristic v2** (default, `clipbot/reel.py`) — the session is split into
   K = round(minutes·60/35) buckets so picks *spread* across the recording; each
   bucket keeps its densest sentence-aligned 15–60 s window (word density,
   questions, decision language); near-duplicates (keyword Jaccard > 0.5 or the
   same takeaway sentence) are dropped; windows are added or removed until the
   runtime **including cards** (intro 4 s + 3 s per chapter) is within ±20 % of
   `--minutes`. Each card gets a one-line why: `Decision`, `Demo`, `Q&A` and/or
   the dominant speaker.

`clipbot outline` is the reading companion: a 30-second-block transcript
(`[h:mm:ss] Speaker: text`) and a JSON skeleton for `--moments` at the end,
also printed to stdout.

## Captions and speakers

Captions come from the source's embedded subtitle stream (Meet's mov_text, with
`(Speaker)` lines), `--srt FILE`, or `--transcribe`. Transcription uses
faster-whisper (`clipbot/transcribe.py`, model `base`, int8 on CPU, ≈ 5.7×
realtime on the Pi) and is an optional extra:

```bash
uv run --project bot --extra whisper clipbot reel --source talk.mp4 --transcribe ...
```

Whisper has no idea who is speaking. `--speakers gemini.txt` aligns speaker
names from a Google Meet "Notes by Gemini" transcript onto unlabelled cues by
interpolated time + word overlap (`clipbot/speakers.py`); pass
`--speakers-offset 0:22:00` when the notes' clock started before the video.
It is best effort and never fails the run.

## Rendering

`--render` runs `uv run --project render cliprender <plan> --root <repo> --overwrite`
and echoes what it prints; the reel path is reported when the renderer writes a
`reel<TAB>path<TAB>seconds` line. Until `render/` supports v1.1 reels it still
writes every clip and clipbot says so.

## Tests

```bash
uv run --project bot python -m pytest -q bot/tests
FASTER_WHISPER_TEST=1 uv run --project bot --extra whisper python -m pytest -q bot/tests -m slow
```

No test runs real whisper or the network unless asked for: the whisper adapter
is tested with a fake model, the LLM selector with a fake client.
