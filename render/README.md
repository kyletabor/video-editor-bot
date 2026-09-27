# Local edit-plan renderer

`cliprender` consumes the repository's [v1 contract](../contract/README.md) and
writes one H.264/AAC MP4 per clip; a v1.1 plan with `output.reel` also gets one
summary video assembled from intro/chapter/outro cards and the clips (see
[Reel assembly](#reel-assembly-v11)), and v1.2 adds opening/closing cards, a music
bed, transitions and audio fades to it (see [Reel v1.2](#reel-v12-openingclosing-cards-music-transitions-fades)).
Install Python 3.11+, uv, and FFmpeg/ffprobe
with `libx264`, AAC, and (for burned captions) `libass`/the `subtitles` filter.
Run from the repository root:

```sh
uv sync --project render --locked
uv run --project render cliprender contract/examples/one-clip-trim.json
```

The first example creates `out/demo/clip-01-recording-test.mp4` (28 seconds)
with captions from the source's embedded subtitle track. The second contract
example requires its referenced `out/demo/transcript.srt` and
`out/demo-shorts/summary.md` to exist first. The renderer does not invent them.

```sh
uv run --project render cliprender plan.json --root /path/to/video-editor-bot
uv run --project render cliprender plan.json --overwrite --timeout 3600
uv run --project render cliprender plan.json --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe
```

Relative paths **inside the plan** resolve from the repository root, including
the source, SRT, summary and output directory. The default root is the repository
containing this editable package; `--root` also locates the authoritative schema.
The plan argument itself resolves from the command's working directory. Spaces
and Unicode are supported; media processes receive argument arrays without a shell.
Use forward slashes inside plan JSON, as specified by the contract.

The package is intended to run from this repository using uv; a separately
installed wheel needs `--root` pointing at a checkout containing `contract/`.
FFmpeg option discovery supports older `-vsync`/filter-script builds and newer
`-fps_mode`/file-option builds. Use the shared gate's prescribed toolchain when
available; renderer flags are not a substitute for its version checks.

## Output and failures

After all clips pass verification, stdout reports every newly written file:

```text
clip-id<TAB>/absolute/output/clip-id.mp4<TAB>28.000000
clip-id<TAB>/absolute/output/clip-id.srt<TAB>28.000000
reel<TAB>/absolute/output/reel.mp4<TAB>38.041667
summary<TAB>/absolute/output/summary.md<TAB>0.000000
```

SRT rows appear only for `sidecar_srt`. The `reel` row appears only when the plan
has `output.reel`; no flag is needed. Summary rows have zero duration and appear
only when a companion document is copied; a summary already at its destination
is preserved in place. Warnings and actionable failures go to stderr. Success
returns 0, failures return 1, and Ctrl-C returns 130. A failed clip names its ID.

Existing outputs require `--overwrite`. Inputs are protected even with that flag,
including symlink/hard-link aliases. All clips are staged before publication;
each file is installed atomically and a failed publication rolls back the batch.
This is not a filesystem-wide atomic transaction: another process can observe
individual files during publication. Do not run concurrent overwrite jobs against
the same destinations. The output filesystem must support hard links for the
default atomic, no-overwrite publication (NTFS/ext4/APFS do).

Each media process has a configurable timeout (default 1,800 seconds). Failure,
timeout or cancellation removes this run's temporary files and leaves previous
results intact. If the OS prevents restoring a previous result, the error gives
the retained recovery-directory path and original filename mapping. Do not delete
that directory until recovery is complete. An output directory may remain empty
after a failed media job; schema/semantic validation errors create no output.

## Contract behavior

| Field | Implemented behavior |
|---|---|
| `version`, all properties | Validate against the checked-in JSON Schema first; reject unknown fields, non-finite numbers, duplicate IDs, invalid ranges and missing inputs. |
| `source.duration_seconds` | Enforce the declared ceiling and the actual decoded video end. Subtitle/container duration cannot extend the usable video. |
| `clips[].segments` | Preserve array order, repeats and overlaps. Each range is half-open `[start,end)` on the common source presentation timeline. |
| `output.dir`, `container` | Create the directory; output `<id>.mp4`. Reserved Windows filenames are rejected with a rename instruction. |
| `output.preset` | Defaults to `internal`; `shorts` defaults to 9:16, the others to 16:9. Warn using the contract's 15–120/90/60/60-second bounds, without rejecting. |
| `output.aspect`, `crop_focus` | 16:9 preserves the source frame, as the schema specifies. 9:16/1:1 crop left/center/right. `speaker` explicitly warns and falls back to center. |
| `output.max_height` | 1080 default, 720 override. Never enlarge either source dimension; normalize display aspect/rotation and round dimensions down to even codec dimensions. |
| `source.captions`, `output.captions` | Read the single embedded text subtitle track or a UTF-8 SRT, intersect/retime cues through the exact cuts, then burn into pixels or write `<id>.srt`. `none` disables captions; omitted source captions mean none. Missing/unsupported requested subtitles fail explicitly. |
| `trim_silence` | Both values retain exact selected ranges. `true` permits tightening but does not require it; this renderer deliberately shaves zero seconds because amplitude alone cannot prove absence of speech. |
| `takeaway`, `hook_offset_seconds` | Store the takeaway as MP4 title and informational hook offset as MP4 comment. No title-card effect or segment reordering is implied. |
| `summary.path` | Copy the existing companion document by basename, byte for byte; preserve it if already at its destination. No PDF conversion or summary generation. |
| `output.reel` (v1.1) | After every clip is verified, draw the cards, conform each segment in one `concat` filter graph, re-encode, verify the reel like a clip and publish `output.dir/<filename>` (default `reel.mp4`) in the same transaction: a reel failure publishes nothing. `chapter_cards`: `auto` shows a card only where a clip has `card`, `all` synthesizes one from `takeaway`, `none` drops chapter cards but keeps intro/outro. Warn above 10 minutes, never reject. A reel named after a clip is rejected as a filename collision. |
| `reel.intro`, `reel.outro`, `clips[].card` | Full-frame dark slide drawn with Pillow's bundled font at the reel's size: title (up to three rows), up to four lines (up to three rows each), chapter footer `k of N · h:mm:ss` naming the clip's place and its first segment's source time. Text wraps on word boundaries only and, when it does not fit, the font shrinks in 12 % steps (100 → 88 → 76 … → 40 %) instead of the text being cut: every contract-valid card (80-character title, 4 × 120-character lines) is shown in full on 16:9, 9:16 and 1:1, and the text block always ends above the footer band (see [Card text fit](#card-text-fit)). Shown for `seconds` (default 3) with silence at the source's sample rate and channel layout; a silent source gives a silent reel. |
| `reel.opening[]`, `reel.closing[]` (v1.2) | Cards after the intro and before the outro, drawn like the others but without a chapter footer (they are not chapters). Timeline: `[intro] + opening + Σ([card] + clip) + closing + [outro]`. |
| `reel.music` (v1.2) | A bed under every run of consecutive cards (`under: cards`, faded at the run edges, continuing through the reel from a running offset) or under the whole reel at `duck_db` beneath speech and `gain_db` beneath cards (`under: all`). Looped when `loop` and the file is shorter than needed. Missing file: rejected before any tool starts. Source without audio: rejected. Never clips: see [Reel v1.2](#reel-v12-openingclosing-cards-music-transitions-fades). |
| `reel.transition` (v1.2) | `cut` = the v1.1 join. `dip` = video fades to black and back at every join, length unchanged. `dissolve` = `xfade`/`acrossfade` between consecutive segments; the reel shortens by `seconds` per join. Shortened with a warning when a segment cannot hold it. |
| `reel.audio_fade_seconds` (v1.2) | `afade` in/out on every clip's audio at its edges (at most half the clip). Applies with the 0.15 s default whenever any v1.2 field is present; a plan with no v1.2 field renders exactly as v1.1. |

Ordinary SDR, progressive video with zero or one mono/stereo audio track is
supported. Silent input stays silent. Multiple video/audio tracks, surround,
interlacing, HDR/Dolby Vision and arbitrary non-right-angle rotation fail with
instructions to prepare a supported source. Multiple embedded subtitle tracks
require an explicitly selected SRT. These limits are explicit failures, not
silent stream removal or untested tone mapping.

## Timing and verification

FFprobe supplies decoded integer frame timestamps and their rational time base.
Only frames whose source timestamps lie inside a requested range are retained;
a range with no frame is rejected. Frame-index trims avoid FFmpeg's nearest-tick
rounding of fractional second boundaries. Source time zero is the container's
common start time, preserving relative audio/video offsets.

Each frame gets its exact requested output time (within five microseconds of the
microsecond encoder time base). `interleave` joins the disjoint output timelines
without inferring segment duration. Variable frame spacing and a positive first
frame offset are preserved. Reported seconds measure the output presentation end,
including any leading video gap; some demuxers report only the subsequent span as
`format.duration` for silent video. The last frame can display for a source-frame interval
past the requested end; container duration tolerance is that interval plus 25 ms.
Every expected frame and timestamp is checked separately, so this tolerance cannot
hide a missing segment. No unselected frame is added to fill a gap.

Audio timestamp gaps become silence on the common source timeline, then cuts are
made at sample boundaries and concatenated. Each segment's sample rounding error
is less than one sample; at most 20 segments are allowed by the schema. AAC output
can have up to one codec packet of trailing padding. This preserves delayed speech
instead of independently resetting the audio start to zero. Encoding is H.264
CRF 18/veryfast with B-frames disabled, yuv420p, AAC 192 kbit/s when audio exists,
with MP4 fast start. Explicit packet duration preserves the last decoded frame
on FFmpeg 7; microsecond movie/track timescales preserve fractional starts.
No byte-size target is promised.

Before publishing, the renderer checks stream counts, geometry, every video
frame timestamp, audio channel count/start/duration, total duration and a full
decode with FFmpeg `-xerror`. Source size/mtime is checked again before publishing.
Frame probing decodes the source and reordered split/interleave graphs can retain
many frames in memory; very long/high-resolution plans need adequate RAM and disk.
The timeout bounds process time, not memory consumption.

For MP4-family sources (`mov,mp4,m4a,3gp,3g2,mj2`) each clip's encoder reads only
the window it needs: an input `-ss` at the midpoint between the last unwanted and
the first wanted frame, never later than the earliest segment start, and `-t`
ending one second after the last selected sample. With `-copyts` every timestamp
is untouched; the demuxer lands on the last keyframe at or before the point and
FFmpeg's accurate-seek trim drops the decoded frames before it, so the frame-index
trims and the audio sample indices are simply re-based to the seek point and the
verification above is unchanged. Without this, a 79-minute 1080p session costs
about five minutes of decoding per clip on an 8-core ARM box. Other containers
(Matroska's millisecond timestamps, formats without a keyframe index) keep the
full decode.

## Reel assembly (v1.1)

The reel is `[intro] + for each clip ([card] + clip) + [outro]`, hard cuts only,
exactly as the contract's timeline says. Cards are drawn by `cliprender.cards`
(`Pillow`, bundled font, no font files) at the clips' output size, then encoded
into segments of exactly `seconds` at the reel's frame rate, paired with generated
silence. `cliprender.reel` feeds every segment through one `concat` filter graph
that scales/pads to the first clip's geometry, conforms to the source's nominal
frame rate (`fps=...:start_time=0`, so a clip whose first frame sits after its
audio start opens with a copy of that frame instead of a hole), resamples to the
source's sample rate and channel layout, and re-encodes with the clips' settings
(H.264 CRF 18 veryfast, yuv420p, AAC 192 kbit/s, fast start), constant frame rate.

Why not the concat demuxer with stream copy: it is faster, but it needs
bit-identical codec parameters and inherits each segment's AAC priming and
timescale quirks at every junction, and those behave differently on FFmpeg 4.4 and
7. Re-encoding gives one continuous timeline whose duration is the sum of its
parts; verification checks stream counts, geometry, that every stream's duration
is within 0.2 s per segment of that sum, audio start and channels, and a full
`-xerror` decode. The reel is staged and published with the clips; a failed reel
publishes nothing from the run. The reel title metadata is the intro title.

## Reel v1.2: opening/closing cards, music, transitions, fades

Contract v1.2 adds five optional fields under `output.reel`. A plan that uses none of
them renders exactly as a v1.1 reel: same command line, same filter graph, byte for
byte (`cliprender.reel.concat_graph` is unchanged and a test pins its text). As soon
as one v1.2 field is present, the schema defaults apply to the rest (`transition`
`cut`, `audio_fade_seconds` 0.15). The timeline becomes
`[intro] + opening + Σ([card] + clip) + closing + [outro]`; opening and closing cards
are drawn like the others but carry no `k of N` footer because they are not chapters.

Everything happens inside the one reel filter graph (`cliprender.reel.reel_graph`),
with filters that were measured to behave the same on FFmpeg 4.4.2 and 7.0.2:

- **Audio fades** (`audio_fade_seconds`): `afade` in and out on every clip's audio at
  its edges, never longer than half the clip. Cards are silent and get none.
- **Transitions** (`transition`): `cut` keeps the `concat` join. `dip` fades each
  segment's video to black over half the transition and the next one in from black
  over the other half (`fade`, inside the segments), so the reel length is unchanged
  and the audio is untouched. `dissolve` cross-fades consecutive segments with
  `xfade` (video) and `acrossfade` (audio, triangular curves); the reel shortens by
  the transition per join and the verification expects exactly that. A transition
  longer than the segments can hold (a 1 s card with a 1.5 s dissolve is a legal
  plan) is shortened for the whole reel with a warning: a dip needs every segment
  at least as long as the transition, a dissolve needs the first and last segment
  at least that long and every middle segment at least twice that.
- **Music bed** (`music`): the file is decoded once to a 16-bit WAV at the reel's
  sample rate and layout (`_reel/bed-once.wav`, bounded by `-t` to what the reel
  needs) and, when `loop` is on and the bed is shorter than what it must cover,
  looped sample-exactly with `-stream_loop` into `_reel/bed.wav`. Why a WAV: mp3 and
  ogg frames carry encoder delay and padding, so looping the compressed file seams
  with a gap and its probed duration is approximate; a WAV does neither. With
  `under: cards`, every maximal run of consecutive cards (intro + opening + the first
  chapter card is one run; closing + outro another) gets one stretch of the bed at
  `gain_db`, faded in and out over `fade_seconds` (at most half the run), cut with
  `atrim` from a running offset so the music continues through the reel instead of
  restarting at each card, placed with `adelay` in samples, and summed onto the
  speech with `amix normalize=0` (`amix` otherwise divides by the input count and
  would halve the speech). With `under: all`, one base stretch covers the whole reel
  at `duck_db` and each card run adds a coherent copy at `gain_db − duck_db` taken
  from the same bed position, so the sum is exactly `gain_db` under cards, `duck_db`
  under speech, ramping over `fade_seconds` at the boundaries: two levels, no
  side-chain, no dynamics processing. If `loop` is off and the bed runs out, the rest
  is silent and a warning says so. A reel with music but no cards warns and plays
  none; a source without an audio track is rejected, since there is no reel audio to
  mix the bed into.
- **Never clipping**: rather than a limiter (FFmpeg 4.4's `alimiter` delays the
  audio by its look-ahead and never flushes it; its `latency` option is 5.1+), the
  mix is kept from clipping by arithmetic (`cliprender.reel.headroom_gain`). The
  16-bit staging bounds the bed at full scale, so its peak is at most its level; the
  peak of every clip is measured with `astats`, which reads the decoder's floats
  (`volumedetect` histograms 16-bit samples, so an over is clipped to 0 dB before it
  is counted and can never be seen). Music and speech only coincide under
  `under: all` and across a dissolve; only then, if speech peak plus music level
  would pass −1 dBFS, is the whole mix lowered by the shortfall, with a warning.
  Speech that plays alone is never touched, however hot the recording is.
- **Verification**: a v1.2 reel is additionally decoded with `astats` before the
  usual full `-xerror` decode: the sample count must match the expected length
  (the duration math including the dissolve overlap, so the track is continuous
  with no missing piece) and, when music was mixed in, the true peak must stay under
  full scale (or under the speech's own peak when the recording is already hotter
  than that), plus 0.5 dB for AAC quantization.

FFmpeg 4.4 differences: `xfade` emits one frame more over a dissolve chain than 7.0.2
(83 frames for three 3 s segments at 10 fps versus 82), well inside the 0.2 s per
segment duration tolerance; `fade`, `afade`, `acrossfade`, `adelay`, `amix`, `astats`
and `-stream_loop` on WAV measured identical on both builds.

## Development and measured checks

```sh
uv sync --project render --extra dev --locked
uv run --project render --extra dev pytest render/tests
uv run --project render --extra dev ruff check render
uv run --project render --extra dev ruff format --check render
make check
```

The shared `veb-uv0` task owns `make check`, shared scripts and CI. Direct renderer
integration tests generate small fixtures and invoke the real CLI. They compare
retained frame pixels and audio markers, including between-keyframe/sub-tick cuts,
reordered and repeated ranges, adjacent boundaries, VFR, silent input, and delayed
audio on a nonzero timeline. Caption tests check retiming and visible burned pixels.

Sample verification on Windows with FFmpeg/ffprobe 7.0.2 and 9.0.2:

| Contract example | Output | Dimensions | Audio | Full decode |
|---|---|---|---|---|
| one-clip-trim | 28.000 s | 1920 × 1080 | stereo AAC | pass |
| two-clips-concat-vertical, first clip | 16.000 s | 606 × 1080 | stereo AAC | pass |
| two-clips-concat-vertical, second clip | 15.000 s | 606 × 1080 | stereo AAC | pass |

Sample runs redirect paths to `render/test-output/`; the second uses captions
extracted from the sample and a small companion-document fixture to verify copying.
These are renderer checks. They do not claim the separate full bot acceptance
task, subjective speech/lip-sync review, or successful runs on macOS/Pi/Linux.

## Changes on 2026-09-25 evening (Kyle's agent, under Kyle's authority)

- **FFmpeg 4.4 compatibility.** `-movie_timescale` and the `setts` bitstream filter's `duration`
  option are FFmpeg 5.0+; both now go through capability checks in `Tools` (`container_flags`,
  `tail_duration_flags`), matching the existing `timing_flags` / `graph_flag` pattern. What 4.4
  still cannot do, measured on Kyle's ARM Ubuntu 22.04 box against the 96 tests below:
  - Its `interleave` filter drops the last queued frame at EOF, so every **multi-segment** clip
    comes out one frame short and fails verification (four fixtures: adjacent half-open ranges,
    delayed audio origin, variable frame rate, reordered sidecar cues). Single-segment clips are
    unaffected. Not worked around; use 7.0.2 for plans with several segments per clip.
  - Without `-movie_timescale` the MP4 edit list that delays a clip's first frame is written in
    the default millisecond movie timescale, so the video track lands up to 1 ms early whenever
    a segment does not start exactly on a frame (most transcript-derived starts). Frame spacing
    is still exact, so verification accepts frame timestamps within 1 ms on such builds instead
    of 5 µs (`MILLISECOND_START` in `renderer.py`); audio start is still checked at 1 ms. This is
    what let an eight-clip reel from the 79-minute recording render on 4.4. The sub-tick fixture
    now renders on 4.4 as well, but its test still fails there because it asserts frame times to
    0.1 ms, which is the precision 7.0.2 delivers and 4.4 cannot.
  So 4.4.2 passes 91 of the 96 tests. The pinned 7.0.2 from `python scripts/install_ffmpeg.py`
  passes 96 of 96 and the shared gate uses it automatically. Recommendation for users: run the
  installer.
- `scripts/install_ffmpeg.py` works on Python 3.10 (sha256 fallback for `hashlib.file_digest`).

### Changes tonight (reel, same evening, Kyle's agent) — for Ramsey's review

- **Cards** (`cliprender/cards.py`, new dependency `pillow>=10.1,<13`): draws the v1.1 card
  slides and encodes them as silent segments. See the contract table above for the layout rules.
- **Reel** (`cliprender/reel.py`, `renderer.py`): `output.reel` assembles the summary video through
  one `concat` filter graph (why: see [Reel assembly](#reel-assembly-v11)), verifies it and
  publishes it in the same transaction as the clips. `Tools.cfr_flags()` gates `-fps_mode cfr`
  versus `-vsync cfr` like the other version checks. The CLI prints one extra `reel` row; plans
  without a reel are unchanged.
- **Decode window** (`renderer.decode_window`): MP4-family sources are read with input
  `-ss`/`-t` under `-copyts`, with frame and sample indices re-based to the seek point, because the
  full decode per clip made a 79-minute session cost about five minutes per clip here. All 92 tests
  pass on 7.0.2 with this active; the 4.4 status is the same 87 of 92. Other containers keep the
  full decode. If this ever looks wrong for a source, `SEEKABLE_FORMATS` is the switch.
- **Captions with blank lines inside a cue** (`captions.parse_srt`): the talk2 recording's
  embedded Zoom captions separate speakers with blank lines inside one cue, so FFmpeg's SRT
  extraction (and `video-editor-bot-data/talk2-embedded.srt`) contain index-less blocks that the
  strict parser rejected ("SRT block 4: expected a numeric index..."), which failed every
  `captions.kind: embedded` plan on that recording. A block with neither index nor timing line
  now continues the previous cue (blank line dropped); a first block that is not a cue is still
  an error, and every existing rejection test still passes. 1,053 cues parse from that file.
- **Reel inputs decode on one thread each** (`reel.render_reel`, `-threads 1` per input): `concat`
  consumes one segment at a time, so a single decoding thread per input outpaces the encoder,
  while frame-threaded decoders would each hold 1080p buffers for inputs that are only waiting.
  Measured on the 17-input, 264 s reel from the 79-minute recording (7.0.2): 81 fps versus 73 fps
  and a 1.7 GB instead of 2.6 GB peak. On this box the whole captioned 8-clip job took 8m49 on
  FFmpeg 4.4.2 (probe about 3 min, clips about 4 min, reel about 1.5 min); a run whose reel
  phase overlapped the full test suite and the gate took 32 min because the box swapped, so
  give a reel about 2 GB of free memory.
- **Threaded frame probe** (`Tools.frames`): ffprobe decodes on one thread by default, so the
  one full pass that lists every source frame took about seventeen minutes for the 79-minute
  recording; `-threads 0` brings it to about six. The frame list is byte-identical on 4.4.2 and
  7.0.2 (threading only pipelines decoding).
- **Tests**: `tests/test_cards.py` (drawing, wrapping, footers, timeline modes, frame rate choice)
  and `tests/test_reel.py` (card segment; a 17 s reel from the numbered fixture checked frame by
  frame and pulse by pulse; `chapter_cards` modes; silent source; no-reel regression; failed-reel
  transaction; filename collision). `tests/conftest.py` re-exports the integration fixtures.
  Acceptance: `uv run --project render cliprender contract/examples/reel-with-cards.json --root .`
  writes `out/demo-reel/reel.mp4`, 38.04 s, 1920 × 1080 at 24 fps, one H.264 video and one AAC
  audio stream, plus the two clips, on both FFmpeg 4.4.2 and 7.0.2.

### Changes on 2026-09-26 (Kyle's agent): contract v1.2 in the renderer — for Ramsey's review

- **What**: opening/closing cards, the music bed, `dip`/`dissolve` transitions and clip audio
  fades, designed as described in [Reel v1.2](#reel-v12-openingclosing-cards-music-transitions-fades).
  `cliprender/reel.py` gained `Style`/`Music`/`Piece`, `reel_graph`, `stage_bed`,
  `headroom_gain` and the extended `verify_reel`; `media.py` gained `warn` (moved from
  `renderer.py` so `reel.py` can warn without a circular import), `Tools.audio_stats` and
  `parse_audio_stats`; `renderer.py` resolves `music.path` before any tool starts and rejects
  a silent source with music. A plan without v1.2 fields still produces the v1.1 command line
  and graph byte for byte.
- **Tests**: `tests/test_reel_style.py` (8, no tools: the v1.1 graph text pinned, schema defaults,
  timeline footers, transition clamps, card runs and running offsets, styled graph text, the
  headroom rule, `astats` parsing) and `tests/test_reel_music.py` (8, real CLI on two generated
  5 s clips and a generated two-level 8 s bed, mp3 when `libmp3lame` exists: music under every
  card run at a running offset and none under speech, silent cards without music, `dip` frames,
  `dissolve` length and blend, `under: all` ducking, `loop: false`, silent source rejected,
  missing bed rejected before tools start). Counts: FFmpeg 7.0.2 98 → 114 passed; 4.4.2
  93 → 109 passed with the same five pre-existing multi-segment failures listed above.
- **Acceptance**: `contract/examples/reel-with-music.json` with `assets/music/bed.mp3` on both
  builds: 49.07 s (7.0.2) / 49.04 s (4.4.2), 1920 × 1080 at 24 fps, AAC 48 kHz stereo.
  `volumedetect` (input-side `-ss`/`-t`) over the card runs: mean −38.3 dB, max −27.2 dB (the
  bed at −18 dB); first 0.3 s max −60 dB and the last 0.4 s of a run max −34 dB (the fades);
  the clip ranges inside the reel measure identically to the clip files (mean −20.1 dB, max
  −0.4 dB), so no bed under speech; float peak −0.45 dBFS, the recording's own; the dip shows
  one black frame and five-frame ramps at 24 fps. Render time on this Pi: 59 s (7.0.2), 85 s
  (4.4.2). With `gain_db: -18` on the −16.5 LUFS bed the music sits about 18 dB under the
  speech: clearly there under the cards, but subtle; −12 to −14 dB would be bolder. The
  contract owns that default.

### Changes on 2026-09-26 (Kyle's agent): card text fit — for Ramsey's review

- **Why**: an independent check of the 2026-09-25 recording's reel found three chapter titles
  (75, 80 and 77 characters) cut with "…" on 1920 × 1080, so the lesson read as a broken
  sentence for three seconds ("Two agents, one repo: let them coordinate through the code,
  not…"). The contract allows 80-character titles and 4 × 120-character lines; a layout that
  gave the title two rows and ellipsized the rest could not honour that.
- **What** (`cliprender/cards.py`, see [Card text fit](#card-text-fit)): `layout` resolves
  fonts, rows and positions before `draw_card` paints. Titles get up to three rows and each
  line up to three; rows break on word boundaries only; when text still does not fit, the font
  shrinks in 12 % steps (`SHRINK_STEPS`, 100 → 40 %), each block on its own for width and
  then, while the block would run into the footer band, the lines first and the title once
  the lines have fallen two steps behind it. The footer band is reserved on every card. The
  ellipsis remains only as a last resort for text outside the contract or frames narrower
  than 1:1. A card that fitted before is drawn at the same size and position as before.
- **Tests** (`tests/test_cards.py`, 114 → 128 in the renderer suite): the three truncated
  titles with their real lines fit in three rows at full size on 1920 × 1080; an 80-character
  title of long words fits without an ellipsis, every word whole, on all three aspects
  (shrinking on 9:16 and 1:1); four 120-character long-word lines stay whole, three rows each
  at most, and end above the footer band on all three aspects; a tall card shrinks its lines
  before its title; a PNG check that the bright text pixels stay inside the margins and above
  the footer band; the fallback for text beyond the contract.
- **Acceptance**: every card of that recording's reel (`out/final/plan.json`, 15 cards) lays
  out without an ellipsis on 16:9, 9:16 and 1:1; on 16:9 all stay at full size, the three
  long titles now on three rows. `contract/examples/reel-with-music.json` re-rendered:
  49.07 s (7.0.2), 1920 × 1080 at 24 fps, AAC 48 kHz stereo, unchanged.

## Card text fit

A card is on screen for a few seconds and cannot be scrolled, so text that is cut reads as
a broken sentence. `cliprender.cards.layout` therefore fits the text before drawing it:

1. **Width.** The title is wrapped on word boundaries into at most three rows; if it needs
   more at the base size (88 px on a 1080-pixel short edge), the title font shrinks one step
   (`SHRINK_STEPS`: 100, 88, 76, 64, 52, 40 %) and wraps again. The lines (up to four, 46 px
   base) are one block: they share a font and shrink together until each fits three rows.
2. **Height.** The text block must end above the footer band (reserved on every card so
   intro, chapter and closing cards share one rhythm). While it would not, the lines shrink a
   step, being the bulk of the text, and the title follows once the lines have fallen two
   steps behind it, so the hierarchy survives without the title paying for text it did not
   cause.
3. **Last resort.** Only at the smallest step are words wider than the column split at
   characters and rows beyond the maximum ellipsized. Text within the contract never gets
   there on a contract aspect: the tests lay out the extremes (80-character long-word title,
   four 120-character long-word lines) on 16:9, 9:16 and 1:1.

The layout is proportional to the frame (margins to the width, fonts to the short edge), so
what fits at 1080p fits at every resolution of the same aspect. On 16:9 every card of the
first real reel keeps the base size; the narrow aspects shrink long titles to 88 or 76 %.
