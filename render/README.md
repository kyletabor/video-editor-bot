# Local edit-plan renderer

`cliprender` consumes the repository's [v1 contract](../contract/README.md) and
writes one H.264/AAC MP4 per clip; a v1.1 plan with `output.reel` also gets one
summary video assembled from intro/chapter/outro cards and the clips (see
[Reel assembly](#reel-assembly-v11)). Install Python 3.11+, uv, and FFmpeg/ffprobe
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
| `reel.intro`, `reel.outro`, `clips[].card` | Full-frame dark slide drawn with Pillow's bundled font at the reel's size: title (at most two rows), up to four lines (two rows each, ellipsized), chapter footer `k of N · h:mm:ss` naming the clip's place and its first segment's source time. Shown for `seconds` (default 3) with silence at the source's sample rate and channel layout; a silent source gives a silent reel. |

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
  `tail_duration_flags`), matching the existing `timing_flags` / `graph_flag` pattern. Result on
  Kyle's ARM Ubuntu 22.04 box: ffmpeg 4.4.2 passes 87 of the 92 tests below; the five that still
  fail are sub-frame edge fixtures (adjacent half-open ranges, sub-tick boundaries, delayed audio
  origin, variable frame rate, reordered sidecar cues) whose output verification is stricter than
  4.4 can deliver. The pinned 7.0.2 from `python scripts/install_ffmpeg.py` passes 92 of 92 and the
  shared gate uses it automatically. Recommendation for users: run the installer.
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
