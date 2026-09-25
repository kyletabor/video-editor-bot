# FFmpeg for local video analysis and rendering

Research date: 2026-09-25. Scope: technical research and executable examples;
this document does not implement the bot or define the shared edit-plan schema.

## Recommendation

Use **FFmpeg and ffprobe as the media-processing foundation**. Ramsey selected
FFmpeg for this project. Probe the recording, collect timestamped evidence,
select source ranges, and render those ranges into clips. Keep semantic highlight
selection as a separate step: a scene boundary or a quiet interval is evidence,
not a judgment that a moment is useful.

The local experiments support accurate trimming and joining of ordinary H.264/AAC
recordings, including inputs without audio. They also demonstrate audio extraction
and scene/silence signals. See [reproduction and measured results](ffmpeg-experiments.md).
No paid media API is required for those operations.

This fits the existing division of work: `bot/` selects content, `contract/`
defines the shared plan, and `render/` executes it. Runtime selection remains in
`veb-de2`; these CLI recipes do not introduce a language or package manager.
The interaction remains a skill/chat or CLI workflow, usable across operating
systems, rather than a new desktop or browser application.

## What FFmpeg contributes

| Need | Local approach | Boundary |
|---|---|---|
| Understand file structure | ffprobe JSON for streams, timing, dimensions and codecs | Metadata is not a description of the content. |
| Inspect the recording | Extract preview frames or a short preview video | Review still needs a viewer or a model. |
| Find possible boundaries | Scene and silence detection | Signals can miss a strong spoken answer or flag an uninteresting transition. |
| Produce speech input | Extract a mono PCM WAV for a transcription adapter | Keep original audio for the final video. |
| Transcribe | Optional FFmpeg Whisper filter, or a separate local speech model | Model weights and build capabilities are additional requirements. |
| Cut and join | Decode, trim, reset segment timestamps and concatenate | Requested times resolve to actual frames/samples. |
| Deliver clips | Encode and mux a validated output | Encoding settings affect quality and size; shorter does not guarantee fewer bytes. |

Probe with an explicit machine-readable format. Treat missing metadata as unknown
and inspect the stream list before constructing an audio filter graph.
[ffprobe reference](https://ffmpeg.org/ffprobe.html#Main-options)

```text
ffprobe -v error -show_format -show_streams -of json "input.mp4"
```

## Proposed processing flow

1. **Probe once.** Preserve the source. Record video/audio/subtitle streams,
   duration, stream start times, time bases, frame-rate information, rotation and
   display aspect ratio. Choose the intended streams explicitly.
2. **Collect evidence.** Reuse a suitable embedded text subtitle stream when
   present; otherwise use a local transcription adapter. Extract preview frames
   where visual context matters. Detect scene and silence boundaries as optional
   supporting signals. An image subtitle stream needs OCR, not text extraction.
3. **Select moments.** Let the user, bot agent, or an explicitly configured local
   model propose ranges and explain why each matches the request. A transcript
   can establish what was said; visual claims need visual evidence. A hosted
   model would be a separate data transfer, not part of the local FFmpeg path.
4. **Validate the shared plan.** Use finite source-relative times and agreed
   half-open ranges `[start, end)`. Require `0 <= start < end <= duration` when
   duration is known. Resolve overlap/removal semantics before rendering. Keep
   all decisions in the future shared contract instead of inventing a second
   schema in `render/`.
5. **Render.** Generate a video-only graph when the input has no audio. For
   ordinary matching A/V streams, trim both on the same source timeline, reset
   each segment's timestamps, concatenate in source order, and encode once.
6. **Verify and publish the artifact.** Check successful process exit, expected
   streams, dimensions and duration, and decode the complete output. Keep the
   result temporary until validation succeeds. Return the ranges and actual
   output properties alongside the downloadable file.

This is a proposed integration design. Selection and transcript summaries remain
bot responsibilities; the renderer consumes their agreed outputs.

## Cutting and joining correctly

Default to re-encoding for arbitrary user-selected cut points. Input seeking with
`-ss` can start at an earlier seek point; accurate transcoding discards the extra
decoded material. Stream copy does not provide that same arbitrary-boundary
guarantee. It is useful when speed and preserving compressed data matter and its
boundary behavior is acceptable. [FFmpeg seeking and output options](https://ffmpeg.org/ffmpeg.html#Main-options)

The [measured recipes](ffmpeg-experiments.md) use `trim`/`atrim` and
`setpts`/`asetpts` before `concat`. Source ranges refer to the original recording;
removing an early section must not shift the interpretation of later ranges.
The test range `[1.25, 3.75)` retained frames from 1.266667 through 3.733333 at
30 fps. This is frame-grid behavior, not arbitrary sub-frame precision.

The recipes assume a zero-based, ordinary SDR source with aligned stream starts.
Independently resetting video and audio would erase an intentional initial A/V
offset. An implementation must map both streams to one presentation timeline,
preserve that offset, and define gap handling before generalizing these examples.
Variable frame rate, rotated phone recordings and HDR need separate fixtures.

Concat filtering requires segments to start at timestamp zero. Inputs must have
compatible dimensions and stream parameters; different frame rates can yield
variable-rate output. [Concat filter](https://ffmpeg.org/ffmpeg-filters.html#concat)
Our first target is multiple ranges from one source, so multi-file normalization
is a later extension. Preserve source orientation, aspect and frame timing unless
the requested export explicitly changes them.

The concat **demuxer** is a different path for compatible streams. Its in/out
points may include extra packets or decoded frames with inter-frame codecs; it
does not replace accurate filtered trimming.
[Concat demuxer and limitations](https://ffmpeg.org/ffmpeg-formats.html#concat)

For the experiment, H.264 (`libx264`), AAC, `yuv420p`, CRF 18 and `veryfast` make
a practical test profile. These are experimental settings, not a project-wide
quality or file-size promise. Browser-oriented MP4 can use `+faststart` to move
container metadata to the beginning.
[MP4 muxer](https://ffmpeg.org/ffmpeg-formats.html#mov_002c-mp4_002c-ismv)

## Local transcription choices

**FFmpeg can transcribe when built with Whisper support.** Its `whisper` audio
filter requires whisper.cpp integration, `--enable-whisper` and downloaded model
weights, and can write text, SRT or JSON. Presence is build-dependent.
[Whisper filter](https://ffmpeg.org/ffmpeg-filters.html#whisper)

```text
ffmpeg -hide_banner -filters
ffmpeg -hide_banner -h filter=whisper
```

The Windows build inspected for this research includes the filter. No model was
downloaded and no speech inference was run, so transcription accuracy, throughput
and timestamp quality are **unvalidated**. Do not make optional Whisper support a
requirement for basic rendering on another machine.

A separate [faster-whisper adapter](https://github.com/SYSTRAN/faster-whisper)
offers CPU INT8 inference, NVIDIA GPU support and optional word timestamps. It
uses CTranslate2 and PyAV; it does not itself require a system FFmpeg executable
to decode audio. Its GPU dependencies and target-platform package availability
must be checked before choosing it as the portable default.

Recommendation: keep the transcription boundary replaceable. Compare usable
embedded captions, the installed Whisper filter, and a separate local adapter on
the same real speech sample before selecting a default. Download models once and
use local paths for offline processing. Transcription does not replace semantic
highlight ranking or visual inspection.

## Portability and integration constraints

The portability target is Windows, macOS and Linux, including ARM Linux/Pi.
Use the native FFmpeg/ffprobe dependency for each platform; do not require a
Windows-only application. The research validated only the Windows environment
listed in the experiment record. No Pi or macOS compatibility or speed is claimed.
[Official download options](https://ffmpeg.org/download.html)

Record version **and build capabilities**, not just a version number:

```text
ffmpeg -version
ffprobe -version
ffmpeg -buildconf
ffmpeg -encoders
ffmpeg -filters
```

The installed 9.0.2 build is an observation, not the cross-platform version pin.
Choose a tested baseline in the shared check-gate work. Core rendering should
work without a GPU or Whisper; discover optional encoders and filters and return
a clear error when a requested capability is absent. FFmpeg is open source; the
applicable license depends on the enabled components/build.
[FFmpeg licensing](https://ffmpeg.org/legal.html)

Invoke the process with separate arguments. Generate filter graphs from validated
numeric ranges and selected stream indexes; never execute model-produced shell
text. Filename quoting and filter-expression escaping are distinct concerns,
especially for Windows drive letters. Prefer controlled relative paths inside
filter expressions. [FFmpeg quoting and escaping](https://ffmpeg.org/ffmpeg-utils.html#Quoting-and-escaping)

Use a unique job directory, preserve stderr diagnostics, support cancellation and
clean only that job's temporary outputs. A nonzero process exit or failed output
validation must not produce a success/download result. These are proposed
renderer behavior requirements rather than features implemented by this PR.

## Validation boundary

The synthetic experiments establish command execution, output stream structure,
duration, frame counts and basic boundary detection. They do not establish
editorial highlight quality, speech recognition accuracy, visual quality,
real-world lip sync, or production performance. Deterministic test fixtures
should become part of the future shared gate; representative recordings should
cover nonzero A/V offsets, VFR, portrait/rotation, HDR, multitrack audio,
unsupported/corrupt inputs and interrupted jobs before release.

Research validation here consists of the executable experiments, source checks
and `git diff --check`. The repository's `make check` gate (`veb-uv0`) has not
landed at the research base; this document does not claim that gate passed.
