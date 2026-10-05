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
uv run --project bot clipbot audit-plan out/talk/plan.json --audio talk.wav [--words talk.words.json]
uv run --project bot clipbot redact  --source talk.mp4 --redact auto --plan out/talk/plan.json
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
   or pauses, are cut from the pause before them to the pause after them (point
   5 below). A pause is where the **audio** is silent
   (`ffmpeg -af silencedetect=noise=-35dB` on the source, downmixed to mono, one
   seek per moment, in parallel, every silence of at least 0.12 s; silences
   separated by less than 0.1 s of sound, a click, count as one), not where
   whisper has no word: every silent stretch longer than `--max-silence` (0.7 s)
   between two pieces of speech is shortened to `--keep-pause` (0.35 s) by
   cutting its middle, and silence at a moment's own edges is trimmed to the
   lead/tail.
   Both sources are needed: whisper emits zero-length words and misses speech
   (on talk2 a "gap" between words held 1.7 s of untranscribed speech at full
   volume), so a gap alone can be speech; and the detector alone cannot tell a
   soft word edge from a pause, so a cut keeps 0.175 s of the silence on each
   side. What is neither a word nor silence (a laugh, cross-talk, a cough)
   stays. Guardrails: no kept fragment is shorter than 1.5 s because of a
   filler cut (the cut is cancelled instead; pause cuts remove no speech and
   are exempt); at most 20 segments per clip; and if the cuts that could hold
   speech (filler cuts, and pause cuts made without silence data) would remove
   more than 40 % of a moment it is kept whole with a warning — that much is a
   stretch whisper did not transcribe. A pause the audio confirmed is exempt:
   talk2's moment 6 gives up 13.8 s of dead air out of 36 s and should. With
   Meet captions only (no word timings) fillers stay, pauses come from the
   audio alone and are shortened to 0.7 s.
4. **Where the speech is: the audio decides, not whisper's span.** The third
   reel had no cut inside a word and still kept ten pauses over 0.9 s: whisper
   had stretched "very" over 1.9 s with 0.84 s of dead air inside, ended "for"
   1.1 s after the sound stopped (so the splice "for | us" held 1.39 s of
   nothing), and placed "But well," 0.8 s early, inside -80 dB silence. A run
   of words that abut is whisper's one span estimate; a run gives up a silence
   inside it when it still has 0.12 s of sound on every side where it continues
   past that silence, and that silence is then a pause like any other. A run
   that lies wholly (or all but a sliver) in silence is left alone: a quiet
   speaker or a hallucination, nobody knows where the words are. Voiced audio is
   never cut: a cut only ever lies inside a detected silence, with 0.175 s (or
   the lead/tail at a moment's edges) of that silence kept on each side.
5. **A filler, and a clip's first and last word, are anchored to pauses, not to
   whisper's timestamps.** The fourth reel's verifier measured the audio at all
   86 segment edges: 11 had speech above -25 dB on both sides, nine of them
   filler cuts. Whisper times an "uh" 50-150 ms off, so "word edge + 0.15 s of
   breath" landed inside the filler or inside the next word. Now a filler is the
   voiced blob between the pause that ends just before it and the pause that
   begins just after it (each within 0.35 s of the filler's acoustic edges), and
   the cut runs from 0.175 s into the first pause to 0.175 s before the end of
   the second, so the join is a pause. A filler with no pause on one side ("Uh,
   it's just..." in one breath, which is how most are said) **stays** and is
   counted (`N fillers kept: no pause beside them`): an "uh" is a lesser fault
   than a cut through a word. A pause that begins more than 0.05 s into the
   neighbouring word is a stop closure inside that word ("crea-t-ing" holds a
   0.13 s silence), not a pause beside it, and does not count. The clip's start
   moves to 0.15 s before the end of the pause nearest its first word (within
   0.8 s: whisper put a zero-length "So" 0.68 s after the sound began) and its
   end to 0.3 s after the start of the pause nearest its last word, unless
   whisper's neighbouring word lies past that pause. Where the speaker ran two
   sentences together and no pause exists within reach, the edge stays on the
   word boundary and the audit below reports it (talk2 has two such starts,
   "cases. What could go wrong?" and "this. And so").

**`clipbot audit-plan PLAN --audio FILE`** is the check the cutter is held to
(`clipbot/audit.py`): it reads the 40 ms before and after every segment edge
straight from the waveform and prints each edge where the peak is above -25 dB
on **both** sides (the cut runs through sound), then the count line the tests and
PRs quote: `edges: 86, voiced on both sides (> -25 dB within 40 ms): 2`. It
shares nothing with the cutter, needs no ffmpeg when `--audio` is a 16-bit PCM
WAV (anything else is decoded first), and exits 1 when there is an offender, so
it can gate a script. `--words FILE` names the word whisper puts at each
offending edge; `--all` prints every edge.

The run prints, per moment, the requested span, the snapped span and what was
cut; the moment's `segments` in the plan are the keep-list. The runtime on the
intro card and in the `reel:` line is intro + opening + every card + kept speech
+ closing + outro, rounded to the second, computed after every card is final.

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

`--framing FILE` (`clipbot/framing.py`) sets all of that from one JSON file, so
nothing has to be patched into `plan.json` afterwards (the third reel's intro
said 4:50 while the file ran 4:55 because the cards were edited after the
runtime line was computed). Every key is optional and overrides the matching
flag: `title`, `date`, `what_you_will_learn` (≤ 4 lines, the opening card),
`takeaways` (≤ 12 lines, 4 per closing card), `outro {title, lines}`,
`opening_seconds` / `closing_seconds` / `outro_seconds` (1–10, per card),
`music_gain_db` (−40..0) and `music_fade_seconds` (0–5) for the `--music` bed,
`transition` (`"dip"` or `{kind, seconds}`). Text over the contract's limits
(80-character titles, 120-character lines) is an error, not an ellipsis.

```bash
uv run --project bot clipbot reel --source talk.mp4 --srt talk.srt --words talk.words.json \
    --moments moments.json --music assets/music/bed.mp3 --framing framing.json --out out/talk/plan.json
```

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

## Storyboard before rendering (`clipbot storyboard`)

Reviewing a reel by watching it was slow: by the time Kyle heard the music jump and saw
the wrong clip open the Talk #3 reel, an hour of rendering and blurring had been spent.
Editors agree on paper first, so the bot writes the paper: one page with every scene in
playback order. Each scene shows its time on the reel and length, a frame, who talks
(read with OCR off the Meet tile, since the recording names the active speaker), what
they say (word timings, else captions), the slide or label text, what the audio does
(theme in, sting, ending, speech only) and the transition. A strip at the top draws the
whole reel to scale with the music under it. Nothing is rendered.

```bash
uv run --project bot --extra vision clipbot reel ... --out out/talk/plan.json        # plan only, no --render
uv run --project bot --extra vision clipbot storyboard --plan out/talk/plan.json --out out/talk/storyboard.html \
  --words talk.words.json --thumbs-from talk.redacted.mp4 --note "Music only at the ends"
```

`--thumbs-from` takes the frames from another copy (a blurred one, so the page never
shows what the reel will hide); `--note` adds lines to a "decisions for review" box.
Publish the page where the reviewer can comment, settle it, then render.

## Telling the reel as a story (`clipbot/story.py`, contract v1.4)

A reel of good moments with one title card each did not teach Talk #3 to someone who
missed it. Kyle's review (2026-10-05) asked for a story: a slide that opens each part,
a "what this covers" slide, the presenter's own overview, then each step with a slide
on why it matters; a link or QR code whenever a repo comes up; labels that say what the
shared screen shows; and the shared screen large with the speaker small in a corner.
The author writes these into the moments and framing files; the bot checks them
against the contract and puts them in the plan.

Per moment in `--moments` (all optional):

```json
{"start": "8:24", "end": "9:16", "title": "Clip bot recap",
 "cards": [{"title": "Part 1: the clip bot", "lines": ["What to remember"]},
           {"title": "Get the clip bot", "lines": ["Open source"], "qr": "https://github.com/kyletabor/video-editor-bot"}],
 "image": "shots/artifact.png",
 "overlays": [{"start": "8:30", "end": "8:41", "text": "The bot's repo is public"}],
 "layout": "full"}
```

- `cards`: up to 4 slides before the moment's own chapter card, e.g. a part opener
  and a QR card. Each takes `image` or `qr` like the chapter card.
- `image` / `qr`: a screenshot or a QR code on the moment's chapter card. Image paths
  are relative to the moments file and written to the plan as absolute paths.
- `overlays`: labels in source time; any that fall on cut-away seconds are dropped.
- `layout`: `"full"` keeps the plain frame, `"pip"` uses the framing default, or an
  object with its own `screen` and `speaker` regions.

In `--framing`, `"layout": {"kind": "pip", "screen": [x, y, w, h], "speaker": [x, y, w, h]}`
is every moment's default. In a Google Meet recording of a screen share, the share and
the active-speaker tile sit in fixed places: find them on one frame grab
(`ffmpeg -ss 20:00 -i talk.mp4 -frames:v 1 frame.png`). Talk #3 was
`screen [0, 240, 1440, 600]` and `speaker [1440, 270, 480, 270]`. Moments where the
call is in gallery view (nobody sharing) take `"layout": "full"`.

## Blurring private details on screen (`clipbot/redact.py`)

A shared screen can show an inbox, a calendar, a client list or a key clearly
enough to read in a reel. `--redact` blurs it:

```bash
uv run --project bot clipbot reel --source talk.mp4 --minutes 4 --redact auto --out out/talk/plan.json --render
uv run --project bot clipbot reel --source talk.mp4 --minutes 4 --redact auto --redact-terms names.txt ...
uv run --project bot clipbot reel --source talk.mp4 --minutes 4 --redact boxes.json ...
```

- **`auto`** reads a frame every second (`--redact-every`) inside the parts of
  the recording the reel shows, with OCR (below), and blurs every word that
  looks private: e-mail addresses, phone numbers, card numbers (Luhn-checked),
  SSNs, API keys (`sk-`, `ghp_`, `AKIA`, ...) and long random-looking tokens,
  plus anything in `--redact-terms FILE` (one name or phrase per line,
  case-insensitive, `re:` for a regular expression). A word seen at one sample
  is blurred from the sample before to the sample after; a frame that has not
  changed since the last sample is not read twice.
- **`text`** blurs every line of text OCR finds: the automatic version of
  blurring the whole shared screen.
- **A file** applies boxes drawn by hand: `[{"start": "12:30", "end": "13:05",
  "box": [x, y, w, h], "why": "screen share"}]`, times in seconds or h:mm:ss,
  the box in source pixels (find them on a frame grab:
  `ffmpeg -ss 12:40 -i talk.mp4 -frames:v 1 frame.png`).

Where two neighbouring samples differ a lot (a scroll, a page switch) the bot takes
more samples in between, down to one frame apart, so a moving word is boxed where it is
at each moment; on Talk #3 a client's name was readable for a second mid-scroll before
this. Samples showing the same screen form a run, which is read a few times (every 3 s
and at its end) and everything any reading found is blurred for the whole run: OCR
misses a word in one frame and reads it in the next.

The blur is one blurred copy of each frame shown through a mask track (one still per
interval, stitched with the concat demuxer), so 5 boxes and 5,000 cost the same: Talk
#3's 471 boxes went from more than an hour to about 12 minutes for a 67-minute recording.

Check the result before anyone sees it:

```bash
uv run --project bot --extra vision clipbot check-redaction --video out/talk/reel.mp4 --terms names.txt
```

reads the rendered video every 0.5 s and lists any e-mail address, phone number, key or
term still readable (exit 1), or says nothing private was readable (exit 0). Hits under
three characters are OCR noise ("ct" inside "Oct"); `--min-length` changes that. A clean
check is evidence, not proof: the presenter still watches the reel before it is shared.

The blur is written to a **redacted copy of the source**,
`<out dir>/<source>.redacted.mp4`: same length and timestamps, audio copied
untouched, text captions kept (as mp4 `mov_text`, so an MKV's SRT track comes
along too), only the video re-encoded (about a quarter
of the recording's length on a laptop; a re-run with the same boxes reuses
the copy). The plan's `source.path` names the copy, so the renderer and the
contract are unchanged and the original recording is never modified. Every
run writes the boxes it used to `redactions.json` next to the plan, in the
file format above: add a box OCR missed and re-run with
`--redact out/talk/redactions.json`.

`clipbot redact --source talk.mp4 --redact auto --plan out/talk/plan.json`
does the same for a plan that already exists (it reads only the plan's
segments and points the plan at the copy; render it again). Without `--plan`
it reads the whole recording, one frame every 2 s.

`auto` and `text` need OCR, picked with `--ocr` (`--redact-ocr` on `reel`):

- **`vision`**: Apple Vision, on a Mac with the `vision` extra
  (`uv run --project bot --extra vision clipbot ...`). It reads each frame as
  overlapping tiles, each upscaled 3x, which is what finds the 7-10 px text of
  a shared screen in a 1080p Meet recording. On Talk #3's shared screen it
  found 10 private spots (5 e-mail addresses, 4 phone numbers, a token) where
  tesseract found 1.
- **`tesseract`**: everywhere else (`brew install tesseract`,
  `sudo apt install tesseract-ocr`, or the UB Mannheim installer on Windows).
  Good on slides and large text; it misses much of a small shared screen.
- **`auto`** (the default): `vision` when it loads, else `tesseract`.

Privacy fails closed: without the OCR engine asked for (`--ocr vision` never
falls back to tesseract), or when ffmpeg fails, the run stops instead of
rendering an unblurred reel. OCR is not perfect (tiny or
low-contrast text, images of text, handwriting), and the patterns are a net,
not a guarantee: a street address, a name not in `--redact-terms`, a number
written as words or a phone number with no separators and a country code
(`14155550142`) is not caught. Use `text` or a hand-drawn box when a screen is
sensitive throughout, and the presenter still watches the reel before it is
shared.

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
