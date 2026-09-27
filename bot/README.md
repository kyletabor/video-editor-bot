# bot/ — clipbot (Kyle's lane)

Turns a recording into a validated **edit plan** (`contract/`). The renderer
(`render/`, Ramsey's lane) turns the plan into video; nothing else crosses the
line. Everything here runs on Windows, macOS and Linux (incl. ARM) with
Python 3.10+, `uv`, and ffmpeg/ffprobe 4.4+ on PATH (or the pinned build in
`.tools/ffmpeg/`, which clipbot prefers when present).

```bash
uv run --project bot clipbot reel    --source talk.mp4 --minutes 4 --title "Talk #2" --out out/talk/plan.json [--render]
uv run --project bot clipbot reel    --source talk.mp4 --srt talk.srt --words talk.words.json --moments moments.json --out out/talk/plan.json
uv run --project bot clipbot outline --source talk.mp4 --out out/talk/outline.md
uv run --project bot clipbot plan    --source talk.mp4 --request "the part about beads" --out out/talk/plan.json
uv run --project bot clipbot summarize --source talk.mp4 --out out/talk/summary.md
```

## Why a reel

The product goal is a 2.5–5 minute *summary* of a 1–2 hour session: several
key moments in chronological order, an intro slide, a short explainer card
before each moment, plus a markdown executive summary and transcript. One
16-second clip is not that. `clipbot reel` writes, next to the plan:

- `plan.json` — contract v1.2: `output.reel` with intro, a "What you'll learn"
  opening card, one card per clip, "Takeaways" closing cards, a sign-off outro,
  `dip` transitions (0.4 s) and clip audio fades (0.15 s); every clip is a
  keep-list of segments (fillers and long pauses cut out). Validated.
- `moments.json` — the chosen moments in the `--moments` shape (below), so a
  person can edit them and re-run with `--moments moments.json`. It stores the
  speech edges (not the padded cut points), so feeding it back is stable.
- `summary.md` — **Takeaways** (the same lines the reel closes on), executive
  summary, a **Reel** section listing the chapters with source timestamps, and
  the full transcript.
- `<source>.srt` and `<source>.words.json` when `--transcribe` was used (reused
  on the next run).

## How moments are chosen

1. **`--moments FILE`** — you decide. A JSON list of
   `{"start", "end", "title", "lesson", "context", "lines", "why"}`; `start`/`end`
   in seconds or `h:mm:ss` (copy them from the outline). Spans are snapped
   outwards to **sentence** boundaries (below); order is kept. `title` (≤ 80) is
   the card title and clip takeaway; `lines` (≤ 4 × ≤ 120) the card body; `why`
   is the single card line when `lines` is absent. `lesson` (≤ 80, what a viewer
   who missed the session learns) and `context` (≤ 120, what the room already
   knew) frame the moment: the card then reads title = lesson, lines =
   [context, why] and the lesson becomes the clip takeaway.
2. **`--llm`** — Claude reads the outline in ≤ 8k-token chunks and proposes
   moments as structured JSON (`clipbot/llm.py`), each with a `lesson` and a
   `context` line written for "a founder who missed the session". Needs
   `ANTHROPIC_API_KEY` and `uv run --project bot --extra llm ...`; without either
   it says so on one line and uses the heuristic. Any API error does the same: a
   reel is never blocked on the network.
3. **Heuristic v2** (default, `clipbot/reel.py`) — the session is split into
   K = round(minutes·60/35) buckets so picks *spread* across the recording; each
   bucket keeps its densest sentence-aligned 15–60 s window (word density,
   questions, decision language, and a *teachable* score from
   `clipbot/lessons.py`: explanations, rules, numbers and how-to phrasing up,
   banter and logistics down); near-duplicates (keyword Jaccard > 0.5 or the
   same takeaway sentence) are dropped; windows are added or removed until the
   runtime **including cards** (intro 4 s + 3 s per chapter) is within ±20 % of
   `--minutes`. Each card gets a one-line why: `Decision`, `Demo`, `Q&A`,
   `Lesson` and/or the dominant speaker.

## Cuts that never clip speech (`clipbot/cuts.py`)

Kyle's review of the first reel: segments cut people off mid-sentence, "ums"
stayed in, long silences stayed in, and there was no summary at the end. Every
moment, whichever way it was chosen, now goes through the same steps:

1. **Snap to sentence boundaries, on word edges.** With word timings (`--words`,
   or what `--transcribe` writes) every edge lands on a word edge, never inside a
   word: the end goes forward to the first word that ends with `. ? !` or is
   followed by a pause of 0.45 s (at most 8 s; past that, the last word end
   followed by a 0.25 s breath), the start goes back to the start of its
   sentence (at most 6 s). An edge inside a word moves outward to include it; an
   edge in the pause after a sentence moves back across the silence to the word
   end, so a request copied from a caption cue that ends in dead air does not
   drag the next speaker's "So" into the clip. Speech is never lost. Without word
   timings the old rule holds: outward to the sentence estimated from the caption
   cues joined at punctuation, speaker changes and pauses > 1.5 s, cutting on the
   cue edge, at most 12 s. Two moments that end up sharing a sentence are
   separated at that boundary and the run says so.
2. **Air.** `--lead-seconds` (0.15) before the first word, `--tail-seconds`
   (0.3) after the last: ASR word edges run early, and the renderer's audio fade
   needs room that is not speech. The air stops 0.05 s short of the neighbouring
   word; the second reel's "…before it merges. [1.6 s] So it|" was a tail that ran
   into the next word.
3. **Fillers and pauses** (on by default; `--keep-fillers` turns it off).
   "um", "uh", "ah", "er", "hmm", and "like" / "you know" when set off by commas
   or pauses, are cut with 0.15 s of breath on each side. A pause is a gap
   between two words that the audio agrees is silent (`ffmpeg -af
   silencedetect=noise=-35dB` on the source, one seek per moment, in parallel);
   every silent stretch longer than `--max-silence` (0.7 s) is shortened to
   `--keep-pause` (0.35 s) by cutting its middle. Both sources are needed:
   whisper's word edges are approximate and it emits zero-length words, and on
   talk2 a "gap" between words held 1.7 s of untranscribed speech at full
   volume, so a gap alone can be speech; and a silence alone cannot tell a quiet
   word end from a pause, so it is clipped to the word edges. What is neither a
   word nor silence (a laugh, cross-talk, a cough) stays. Guardrails: a cut never
   lands inside a word, so a pause whisper stretched a word over (talk2's "very"
   spans 1.9 s with 0.84 s of silence inside) stays too; no kept fragment is
   shorter than 1.5 s because of a filler cut (the cut is cancelled instead;
   pause cuts remove no speech and are exempt); at most 20 segments per clip; and
   if the cuts would remove more than 40 % of a moment it is kept whole with a
   warning — that much "silence" is dead air worth re-picking or a stretch
   whisper did not transcribe. With Meet captions only (no word timings) fillers
   stay, pauses come from the audio alone and are shortened to 0.7 s.

The run prints, per moment, the requested span, the snapped span and what was
cut; the moment's `segments` in the plan are the keep-list. The runtime on the
intro card and in the `reel:` line is intro + opening + every card + kept speech
+ closing + outro, rounded to the second.

## Lessons and the summary at the end (`clipbot/lessons.py`)

A clip of a meeting only helps someone who was not there when it is framed.
Moments may carry `lesson` and `context` (from `--moments` or `--llm`); the
reel opens with a "What you'll learn" card (one line per lesson, or "N moments
on a, b" plus the first three when there are more than four) and closes with
one or two "Takeaways" cards (≤ 4 lines each) and a sign-off outro. Takeaway
lines come from, in order: `--takeaways FILE` (one per line) > the moments'
`lesson` fields > the titles of hand-picked or model moments > the extractive
executive summary. `summary.md` repeats them under **Takeaways**. `--music FILE`
adds a bed under the cards (an audio file you have the rights to;
`contract/README.md`), `--transition cut|dip|dissolve` picks the join (default
`dip`).

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

`--transcribe` also writes `<name>.words.json` (`[{start, end, word}]`,
`clipbot/words.py`) next to the SRT and prompts whisper to keep disfluencies
(it drops "um"/"uh" by default: the first talk2 pass kept 5 of ~270). `--srt`
users pass `--words FILE`; a `<name>.words.json` next to the SRT is picked up
automatically. Without word timings the sentence logic estimates word positions
inside each cue by character count, only to decide *which* sentence a time falls
in; cuts still land on cue edges. Whisper holds the whole recording in memory:
a 79-minute file needs about 2 GB free.

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
