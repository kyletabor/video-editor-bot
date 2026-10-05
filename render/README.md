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
| `source.captions`, `output.captions` | Read the single embedded text subtitle track or a UTF-8 SRT, intersect/retime cues through the exact cuts, then burn into pixels or write `<id>.srt`. A cue that a cut passes through keeps only the words spoken in its kept part and is dropped when a quarter or less of it survives (see [Caption text at cut edges](#caption-text-at-cut-edges)); cues wholly inside a segment keep their text unchanged. `none` disables captions; omitted source captions mean none. Missing/unsupported requested subtitles fail explicitly. |
| `trim_silence` | Both values retain exact selected ranges. `true` permits tightening but does not require it; this renderer deliberately shaves zero seconds because amplitude alone cannot prove absence of speech. |
| `takeaway`, `hook_offset_seconds` | Store the takeaway as MP4 title and informational hook offset as MP4 comment. No title-card effect or segment reordering is implied. |
| `summary.path` | Copy the existing companion document by basename, byte for byte; preserve it if already at its destination. No PDF conversion or summary generation. |
| `output.reel` (v1.1) | After every clip is verified, draw the cards, conform every card and clip into a piece of exact length in its own FFmpeg process, join the pieces (video stream-copied, audio joined sample-exactly and encoded once), verify the reel like a clip and publish `output.dir/<filename>` (default `reel.mp4`) in the same transaction: a reel failure publishes nothing. `chapter_cards`: `auto` shows a card only where a clip has `card`, `all` synthesizes one from `takeaway`, `none` drops chapter cards but keeps intro/outro. Warn above 10 minutes, never reject. A reel named after a clip is rejected as a filename collision. |
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
Frame probing decodes the whole source once (streaming, about 100 MB); each clip
is then rendered in parts of at most 20 s of source so that its peak memory does
not depend on the plan (see [Clip rendering in parts](#clip-rendering-in-parts)).
The timeout bounds process time, not memory consumption.

For MP4-family sources (`mov,mp4,m4a,3gp,3g2,mj2`) each encode reads only the
window it needs: an input `-ss` at the midpoint between the last unwanted and
the first wanted frame, never later than the earliest segment start, and `-t`
ending one second after the last selected sample. With `-copyts` every timestamp
is untouched; the demuxer lands on the last keyframe at or before the point and
FFmpeg's accurate-seek trim drops the decoded frames before it, so the frame-index
trims and the audio sample indices are simply re-based to the seek point and the
verification above is unchanged. Without this, a 79-minute 1080p session costs
about five minutes of decoding per clip on an 8-core ARM box. Other containers
(Matroska's millisecond timestamps, formats without a keyframe index) keep the
full decode, once per part.

## Clip rendering in parts

A clip is rendered one part at a time and the parts are joined without re-encoding.
Why: the single filter graph that rendered a clip before (`split`, one `trim` per
segment, `interleave`) looked like a streaming pipeline but was not. `interleave`
emits a frame only once every one of its inputs holds one, and the input for the last
segment holds nothing until the decoder reaches that segment, so every decoded frame
of every earlier segment waited in memory. A 37 s, four-segment 1080p24 clip from the
79-minute recording was killed by the kernel at 2.2 GB (`anon-rss:2228316kB`), twice,
on a box with 2.7 GB free, and the peak grew with the source window a clip covered,
not with the seconds it kept.

`cliprender.renderer.parts` cuts each segment into parts of at most `MAX_PART_SECONDS`
(20 s) of source, at the presentation time of a source frame, so `bisect_left` names a
part's frames exactly as it names a segment's. Each part is one FFmpeg process: the
input seek just before its first frame (`decode_window`, as before), `trim` by frame
index, `setpts` with the same constant the single graph used (`origin + start - offset`,
so the part file carries the clip's final timestamps; written as an exact microsecond
count because FFmpeg truncates the floating-point `seconds/TB` division, and on FFmpeg 4.4
a part's first frame that is one microsecond short lands a whole millisecond early through
the millisecond edit list that delays it), the geometry filters,
the clip's captions, and the same H.264 settings for every part (`Session.encoder_flags`:
CRF 18 veryfast, `-bf 0`, microsecond time bases, hence identical parameter sets and
closed GOPs). Nothing in that pipeline holds more than the decoder's and encoder's own
working set, whatever the plan.

Audio is rendered once per clip, without video (`render_audio`): the sample-exact graph
that always cut the segments at sample boundaries and joined them is unchanged, now
writing a float WAV whose sample count is checked against the plan before the join.
Why not AAC per part: every AAC file carries encoder priming and a padded last frame,
so stream-copying AAC pieces together opens gaps at the joins; the float WAV is
lossless and the clip's AAC is encoded once from it, from the same samples the single
graph fed its encoder.

The join is the concat demuxer with the video stream copied (`-c:v copy`,
`-auto_convert 0` so the parameter sets are not rewritten in-band) and the WAV encoded
to AAC. The demuxer adds `start_time - inpoint` to every packet of a file, where
`start_time` is the sum of the previous files' `duration`s; by default `inpoint` is the
file's own first timestamp, which would move each part to the end of the previous one
and discard its lead-in. `concat_list` therefore declares every file's `duration` (the
distance to the next part's output offset, in microseconds) and an `inpoint` equal to
its own offset, so the two sums agree and the demuxer adds exactly zero. The declared
durations also keep the demuxer from trusting the containers' lengths (millisecond
rounding on FFmpeg 4.4, a guessed last-frame duration). The last frame's duration is
pinned again at the join (`setts`, FFmpeg 5.0+ as for the encodes): an MP4 demuxer derives
a file's last packet duration from the stream duration, which excludes the edit list that
delays the first frame, so for a part that starts late in the clip it comes out zero and
the muxer would otherwise guess it from the average frame rate (0.3 s instead of 0.1 s on
the variable-rate fixture). Interior frames are unaffected, their durations being re-derived
from the next frame's timestamp. Verification is unchanged and runs on the joined file:
every frame timestamp against the plan, audio start and length, a full decode. The part
workspace is deleted after each clip's join.

Captions do not change at a part boundary: the cues are retimed and word-trimmed once
for the whole clip against the plan's real cuts, and every part burns that one SRT
against the clip's own timestamps. This matters more than it looks: the `subtitles`
filter converts a frame's timestamp to milliseconds in floating point and truncates, so
a cue edge that falls exactly on a frame time can flip between shown and hidden when
the absolute timestamps differ by a single microsecond. Part-relative timestamps
would have made that flip possible at every boundary; identical timestamps make the
edge decisions identical too, and the test suite compares a split render with an
unsplit one frame by frame.

Measured on the 8-core ARM box (FFmpeg 7.0.2) with clip-01 of the Talk #2 plan (1080p24,
four segments, 34.98 s kept over a 37.4 s window, burned captions), polling the RSS of
every process every 0.2 s: before, the clip encode reached 1759 MB within two seconds
of starting and was stopped by the measurement's 1.7 GB guard (the kernel had killed
the same render at 2.2 GB); after, the largest process peaked at 444 MB (a part
encode; the audio pass 30 MB, the join 30 MB, verification 104 MB) and the clip took
about 14 s after the frame probe (audio 0.2 s, parts 3.0 + 0.2 + 2.5 + 2.4 s, join
1.6 s, verification 2.2 s). One side effect on FFmpeg 4.4.2: `interleave` is no longer
used, so the four multi-segment fixtures that its last-frame drop failed now pass
there; 4.4's one remaining known failure is the sub-tick test's 0.1 ms precision, a
muxer limit unrelated to this change.

## Reel assembly (v1.1)

The reel is `[intro] + for each clip ([card] + clip) + [outro]`, hard cuts only,
exactly as the contract's timeline says. Cards are drawn by `cliprender.cards`
(`Pillow`, bundled font, no font files) at the clips' output size. `cliprender.reel`
then builds the reel in bounded steps, one FFmpeg process per step, none of which
opens more than three media inputs:

1. **Pieces.** Every timeline item (a card PNG looped at the reel's frame rate over
   generated silence, or a clip file) is conformed on its own into a *piece*: video
   scaled/padded to the first clip's geometry, at the source's nominal frame rate
   (`fps=...:start_time=0`, so a clip whose first frame sits after its audio start
   opens with a copy of that frame instead of a hole), yuv420p; audio at the source's
   sample rate and channel layout. Both are pinned to an exact length on the frame
   grid: `round(seconds × fps)` frames (`tpad` repeats the last frame if the video
   runs short, `trim` drops any surplus) and the matching sample count (`apad`,
   `atrim`), so video and audio cannot drift apart at a join. The video is encoded
   with the clips' settings (H.264 CRF 18 veryfast, yuv420p, `-bf 0`, explicit
   BT.709/limited-range tags so a card drawn in RGB and a clip decoded from the
   recording get the same parameter sets); the audio is written as a float WAV.
2. **Join.** The video pieces are joined by the concat demuxer with the stream copied
   (`-c:v copy`, `-auto_convert 0`, every file's `duration` declared as its frames
   over the rate so the containers' own lengths are never consulted); the WAVs are
   concatenated byte for byte by the renderer (`join_wavs`), which also checks that
   the total is exactly the sum of the pieces before a single AAC frame is encoded.
   One process muxes the copied video with the AAC encoded once from that WAV
   (192 kbit/s, fast start, title metadata from the intro).

Why not the single `concat` filter graph that did this before: FFmpeg opens a decoder
for every input of a graph and, from 7.0, reads and decodes ahead on all of them, so
the 25-input graph of a 4.9-minute 1080p reel held about 2.2 GB of frames and was
killed by the kernel on a box with 2.7 GB free (the same plan fitted at 720p). Memory
grew with the number of pieces; now it is that of one piece (see the measurements
below). Why not stream-copy the clips themselves: their AAC priming and the cards'
would open gaps at every junction, and their parameter sets would have to match by
luck; conforming each piece once, with one encoder configuration, makes the copy join
exact by construction. Verification is unchanged: stream counts, geometry, every
stream's duration within 0.2 s per segment of the sum of the pieces, audio start and
channels, and a full `-xerror` decode. The reel is staged and published with the
clips; a failed reel publishes nothing from the run.

## Reel v1.2: opening/closing cards, music, transitions, fades

Contract v1.2 adds five optional fields under `output.reel`. A plan that uses none of
them conforms its pieces with no v1.2 filter at all (no fade, no transition, no
music; `tests/test_reel_style.py` pins that graph text), so the v1.1 reel is the plain
join of its pieces. As soon as one v1.2 field is present, the schema defaults apply
to the rest (`transition` `cut`, `audio_fade_seconds` 0.15). The timeline becomes
`[intro] + opening + Σ([card] + clip) + closing + [outro]`; opening and closing cards
are drawn like the others but carry no `k of N` footer because they are not chapters.

Every v1.2 effect is applied per piece or per join, in the bounded steps above, with
filters that were measured to behave the same on FFmpeg 4.4.2 and 7.0.2:

- **Audio fades** (`audio_fade_seconds`): `afade` in and out on every clip's audio at
  its edges, inside the clip's piece, never longer than half the clip. Cards are
  silent and get none.
- **Transitions** (`transition`): `cut` is the plain join. `dip` fades each piece's
  video to black over half the transition and the next one in from black over the
  other half (`fade`, inside the pieces), so the reel length is unchanged and the
  audio is untouched. `dissolve` is quantized to whole frames (`transition_frames`)
  and rendered as a *bridge* per join: while a piece is conformed, its first and last
  transition's worth of frames and samples are split off into raw files (`head`,
  `tail`) in the same pass, its body is what the reel plays, and a second small
  process cross-fades the outgoing tail into the incoming head (`xfade` for the
  video, pinned to exactly the transition's frames; two triangular `afade`s summed
  for the audio, the same curve as `acrossfade`). The reel shortens by the
  transition per join and the verification expects exactly that. A transition
  longer than the segments can hold (a 1 s card with a 1.5 s dissolve is a legal
  plan) is shortened for the whole reel with a warning: a dip needs every segment
  at least as long as the transition, a dissolve needs the first and last segment
  at least that long and every middle segment at least twice that, and on the frame
  grid every piece keeps at least one frame of its own between its bridges.
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
  restarting at each card, written once as its own WAV with its level and edge fades
  (`bed_piece_graph`), and mixed into every reel segment it overlaps (`mix_music`: the
  segment's WAV plus the overlapping part of the stretch, cut and placed by sample
  count, summed with `amix normalize=0`, since `amix` otherwise divides by the input
  count and would halve the speech). With `under: all`, one base stretch covers the whole reel
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

FFmpeg 4.4 differences: `xfade` emits one frame more than 7.0.2 over a chain of
segments; a bridge is pinned to exactly the transition's frames (`tpad`, `trim`), so
both builds now give the same frame count. `fade`, `afade`, `adelay`, `amix`, `apad`,
`tpad`, `astats` and `-stream_loop` on WAV measured identical on both builds.

## Reel v1.4: pictures on cards, card lists, pip framing, overlays

Contract v1.4 fields are all optional; a plan without them renders exactly as before
(the per-part graph text is pinned by `tests/test_v14.py`, and a clip gets the new code
path only when it has `layout: pip` or `overlays`).

- **`card.image`** (`cliprender.cards`): a PNG or JPEG, resolved like every plan path
  (repo-root-relative or absolute). On 16:9 the text keeps the left 55 % of the frame
  and the picture is fitted into the right part; on 9:16 and 1:1 the text may use at most
  60 % of the height above the footer band and the picture fills what is left below it.
  Fitted, centred, never cropped, inside a 2 px border; a transparent PNG is flattened onto
  the card background. The text layout runs unchanged on the narrower or shorter area, so
  the shrink steps absorb the difference: the extremes tests (80-character title of long
  words, four 120-character lines) also run with an image and a QR code on all three
  aspects, without an ellipsis. Every card image of the reel is opened before the first
  tool runs; a missing file, an unreadable one or a GIF fails the plan with its path.
- **`card.qr`**: drawn with [segno](https://pypi.org/project/segno/) (pure Python, error
  correction M, smallest version), placed like an image: black modules of whole pixels on a
  white square with the four-module quiet zone the QR specification asks for, so a phone
  can scan it off a screen, and the URL printed under it (wrapped by characters, up to four
  rows) so it can also be typed. The tests compare every module's centre pixel with segno's
  matrix for the exact URL, which is what a scanner reads.
- **`clips[].cards`**: shown in order right before the clip, ahead of its `card`.
  Decision: they show under every `chapter_cards` mode, `none` included, because they are
  explicit section and link slides the author placed, not chapter markers the renderer may
  synthesize or drop. The chapter footer (`k of N · h:mm:ss`) goes on the last card before
  the clip only, so a run of slides is counted once; under `none` no card has a footer.
  Consecutive cards are one card run, so a music bed plays through them without restarting.
- **`clips[].layout: pip`** (`cliprender.framing`, 16:9 only; 9:16 and 1:1 warn and render
  the full frame): the `screen` region is cropped and scaled to fit the clip's usual 16:9
  frame keeping its shape. If the frame has a band left beside or below it and the speaker
  tile fits that band at least 18 % of the frame high, the tile goes in the band at `corner`
  and the screen moves away from that corner (top-centred for a wide share). Otherwise the
  screen is centred and the tile laid over it at 24 % of the frame width, with a margin of
  2.5 % of the height and a 2 px light border. The band is the card background colour.
  Without `speaker` the screen alone is fitted and letterboxed. Regions must lie inside
  the source frame (as the graph receives it, after autorotation) and be at least 2 x 2,
  or the plan fails before any encode. The geometry is one chain (`split`, `crop`, `scale`,
  `pad`, `overlay`) in place of the usual geometry filters of every part's graph, so parts,
  joins and verification are unchanged. When the tile sits in a bottom corner, burned
  captions get symmetric libass margins (`force_style`, script units of width / 384, which
  is how libass scales an SRT's margins; measured) that keep them centred and clear of it.
  The output frame is never larger than the source; the screen region is enlarged to fill
  it, which is the point of the layout.
- **`clips[].overlays`** (`cliprender.overlays`): burned with libass, not `drawtext`
  (Homebrew's plain FFmpeg has no FreeType; libass is already needed for captions). Each
  kept frame is tested on its own: a label shows while the frame's *source* time is in
  `[start, end)`, consecutive frames that pass become one ASS event, and the event edges
  are put between frames (centisecond ASS times, 2 ms of slack for FFmpeg's millisecond
  rounding, exact up to 60 fps), so reordered or non-contiguous segments never put a label
  on the wrong picture. One `.ass` script per clip, at the frame's pixel size: white text
  on a semi-opaque dark box, about 3.2 % of the frame height, top-left (`top`) or
  bottom-left (`bottom`), switched to the right side when a pip tile occupies that corner.
  It is burned after captions, whatever `output.captions` is (`none` and `sidecar_srt`
  included). Braces and backslashes in a label are shown as look-alikes rather than read
  as ASS tags.

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
    *(Superseded on 2026-09-26: clips are rendered in parts and joined without `interleave`,
    and those four fixtures pass on 4.4.2; see [Clip rendering in parts](#clip-rendering-in-parts).)*
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
- **Tests** (`tests/test_cards.py`; renderer suite 114 → 128 passed on FFmpeg 7.0.2, 109 → 123
  on 4.4.2 with the same five pre-existing multi-segment failures): the three truncated
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

### Changes on 2026-09-26 (Kyle's agent): caption text at cut edges — for Ramsey's review

- **Why**: the independent check of the Talk #2 reel found the first burned caption showing
  words that had been cut. The clip opened on "Normally what you would do", but the caption read
  "I don't know. I don't know. I so, okay, normally what you would do": `retime` split cues by
  time and kept the whole text, and Meet/Zoom cues run about four seconds, so every clip that
  starts or ends inside a cue showed up to four seconds of speech the viewer never hears.
- **What** (`cliprender/captions.py`, see [Caption text at cut edges](#caption-text-at-cut-edges)):
  a cue that a segment edge passes through keeps only the words spoken in its kept part,
  trimmed at word boundaries from the cut side; a cue that keeps a quarter or less of its
  duration, or no whole word, is dropped. Cues wholly inside a segment keep their text byte
  for byte, so sidecar SRT output is unchanged wherever no cut passes through a cue. Speaker
  tags and cut markers follow the speech they belong to, so a trimmed sidecar cue still names
  its speaker; the burn-in path strips them as before.
- **Tests** (`tests/test_captions.py`; renderer suite 128 → 140 passed on FFmpeg 7.0.2): the
  8-word, 4 s cue cut at 2 s keeps the last four words, cut at 3 s the first six, cut at 1 s is
  dropped, wholly inside is unchanged; both edges trimmed; the threshold from both sides; a
  one-word cue; a Zoom multi-speaker cue keeping only the surviving speaker's tag and marker;
  the demo clip's opening cue and the Talk #2 finding. Three existing edge-cut assertions were
  updated to the trimmed text.
- **Acceptance**: `contract/examples/one-clip-trim.json` (clip starts 2 s into the 4 s cue
  "Okay, this is a") renders 28.000 s as before; the frame at 0.5 s now burns "is a" instead of
  "Okay, this is a", and the frame at 2.5 s shows the untouched second cue.

### Changes on 2026-09-26 (Kyle's agent): clips rendered in parts, memory bounded — for Ramsey's review

- **Why**: rendering one clip (37 s of source, four segments, 1080p24, burned captions) from
  the Talk #2 recording was killed by the kernel twice at 2.2 GB anonymous RSS on an 8-core
  ARM box with 2.7 GB free; the same plan had rendered earlier with 6 GB free. The clip graph
  (`split` → `trim` per segment → `interleave`) buffered every decoded frame of all but the last
  segment until the decoder reached that segment, so peak memory was proportional to the source
  window a clip covered. A 4-minute reel from a 79-minute recording must render with 2 GB free.
- **What** (`cliprender/renderer.py`, see [Clip rendering in parts](#clip-rendering-in-parts)):
  `parts` cuts segments into parts of at most 20 s at exact frame boundaries; `video_graph`
  encodes each part on its own with the clip's output timestamps and captions; `audio_graph`
  (the previous audio half of the graph, unchanged) renders the clip's audio once to a float
  WAV whose sample count is checked; `concat_list` and the concat demuxer join the parts by
  stream copy with the demuxer adding exactly zero to every timestamp (`inpoint` equal to each
  file's offset, explicit `duration`s), and the AAC is encoded once at the join. `Session`
  bundles what every clip shares; `verify` and the transactional publication are untouched and
  run on the joined file. `filter_graph` is gone; its text survives in `tests/test_parts.py` as
  the reference the new pipeline is compared against.
- **Measured** (FFmpeg 7.0.2, RSS polled every 0.2 s while rendering clip-01 of the Talk #2 plan):
  before 1759 MB and climbing when the measurement's 1.7 GB guard stopped it (the kernel had
  killed the same render at 2.2 GB earlier that evening); after 444 MB peak for the largest
  process, 517 MB for the whole
  process tree, about 14 s for the clip after the frame probe. Output identical to the plan:
  842 frames, 35.008 s of video (34.98 s kept plus the last frame's display time), 34.980 s
  of audio.
- **Tests** (`tests/test_parts.py`, renderer suite 140 → 149 on FFmpeg 7.0.2; 4.4.2 goes from
  135 passed / 5 failed to 147 passed / 1 skipped / 1 failed, the four `interleave` failures
  gone): cuts at frame boundaries (45 s → 3 parts, exactly 20 s untouched, 20.1 s → 2 parts,
  variable frame rate and a nonzero origin); the concat list's inpoint/duration arithmetic;
  a real 45 s segment rendered in three parts with every frame and three audio pulses across
  the cuts checked; a reordered, repeated three-segment clip compared with the legacy
  single-graph render (frame timestamps within a microsecond, the same luminance, audio within
  1 ms, the same end; skipped on 4.4 where the legacy graph itself drops a frame); a part
  boundary inside a cue leaves burned and sidecar captions identical; a failed join publishes
  nothing.

### Changes on 2026-09-27 (Kyle's agent): reel assembled in bounded steps — for Ramsey's review

- **Why**: with every clip of the Talk #2 plan rendered (peak 0.44 GB each after the
  parts change), the reel step was killed by the kernel at 2.2 GB anonymous RSS
  (`Out of memory: Killed process (ffmpeg) total-vm:6462856kB, anon-rss:2226680kB`) on the
  8-core ARM box with 2.7 GB free. The single filter graph over 15 card segments, 10 clips
  and the bed decoded ahead on every input; the same plan fitted at 720p and died at 1080p,
  so the memory scaled with the number and size of the inputs, not with any one step.
- **What** (`cliprender/reel.py`, see [Reel assembly](#reel-assembly-v11) and
  [Reel v1.2](#reel-v12-openingclosing-cards-music-transitions-fades)): every item is
  conformed on its own into a piece of exactly so many frames and samples with one encoder
  configuration (`piece_graph`, `encode_piece`; cards straight from their PNG, so a card is
  encoded once and `encode_card_segment` is gone); dips and audio fades are applied inside
  the piece; a dissolve becomes a bridge per join, cross-faded from the head and tail split
  off during the neighbours' conform (`encode_bridge`); each stretch of the music bed is
  written once (`render_beds`) and mixed into the segments it overlaps (`mix_music`); the
  WAVs are joined by the renderer with the sample count checked (`join_wavs`), the video by
  the concat demuxer with `-c:v copy`, and the AAC is encoded once at the mux. Verification,
  the headroom rule, the transactional publish and `renderer.py`'s call are unchanged. No
  FFmpeg process of the reel step opens more than three media inputs. The old
  `concat_graph`/`reel_graph`/`music_graph` and card encoder survive verbatim in
  `tests/legacy_reel.py` as the reference the pieces are compared against.
- **Measured** (FFmpeg 7.0.2, RSS of every process polled every 0.2 s while assembling the
  Talk #2 reel, 294.8 s of 1080p24 in 25 pieces with dips and music under 12 card runs, from
  the ten rendered clips): before, the single-graph `ffmpeg` reached 1670 MB 34 s in and was
  still climbing when the measurement's 1.5 GB guard stopped it (the kernel had killed the
  real run at 2.2 GB); after, 512 MB peak for the largest process (a 1080p piece conform),
  572 MB for the whole process tree, 110 s wall for the whole reel step, 7076 frames,
  294.833 s of video and audio, verification passed.
- **Tests** (renderer suite 149 → 158 on FFmpeg 7.0.2; on 4.4.2 156 passed, 1 skipped and the
  one pre-existing sub-tick muxer failure, every reel test green):
  `tests/test_reel_style.py` pins the piece, bridge, bed and mix graphs, the frame grid and
  the WAV join; `tests/test_reel_bounded.py` renders the music fixture reel with cut, dip and
  dissolve and asserts that no reel-step command has more than three `-i` (and that the widest
  mix, `under: all`, has exactly three), then assembles the same reel with the legacy graph
  and compares: the pieces give exactly the timeline's frames and samples on both builds,
  where the single graph's length depended on the build (one frame and 64 ms long on 7.0.2,
  its card silences outlasting the cards; 107 ms short on 4.4.2), every frame's luminance
  matches within 1.5 except a dip's fade ramp meeting that drift, and every piece's interior
  has the same music and speech levels. The existing music, transition, style and reel tests
  pass unchanged.

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

## Caption text at cut edges

Embedded meeting captions (Google Meet, Zoom) arrive as cues of about four seconds with no
timing inside the cue, while the bot cuts clips at word boundaries anywhere in the speech. A
cut therefore usually lands inside a cue, and a caption that keeps the whole cue's text shows
the viewer words that were cut out, most visibly on the first frames of a clip.
`cliprender.captions.retime` handles a cue that a segment edge passes through like this:

1. **Drop, if little is left.** The cue must keep strictly more than `MIN_KEPT_FRACTION`
   (a quarter) of its duration; a shorter remainder is dropped rather than shown as a
   fragment. A cue the segment only touches at a boundary was already excluded.
2. **Trim by time, at word boundaries.** Without per-word timing the words are taken to be
   evenly spaced over the cue: word `i` of `n` is kept when its centre `(i + 0.5) / n` falls
   inside the kept fraction of the cue. That keeps one contiguous run of words, from the side
   the cut did not touch, with the original spacing; a segment starting inside the cue drops
   leading words, one ending inside it drops trailing words, one lying inside it both. A cue
   whose words are all cut is dropped.
3. **Annotation lines follow their speech.** A speaker tag `(Name)` or `()` stays with the
   content line after it, a lone `-` with the line before it, so a trimmed sidecar cue still
   names the speaker who is heard. `tidy_for_burn` strips those lines from burned captions as
   before.

A cue wholly inside a segment keeps its text byte for byte, so a sidecar SRT only changes for
the cues a cut passes through. The trimmed cue keeps the intersected timing, so it is shown
for exactly the kept part of the cue. The heuristic is proportional, not a transcript
alignment: on the demo clip the 4 s cue "Okay, this is a" cut at 2 s burns "is a", which is
close to what is heard and never shows words that are not.
