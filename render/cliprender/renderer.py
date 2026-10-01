"""Validate, render, verify and publish a v1/v1.1 edit plan without modifying its inputs."""

import json
import math
import os
import shutil
import tempfile
from bisect import bisect_left
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from pathlib import Path

from jsonschema import Draft202012Validator

from .captions import Cue, format_srt, parse_srt, retime, tidy_for_burn
from .media import RenderError, Tools, geometry, inspect_media, warn
from .reel import (
    MAX_RECOMMENDED_SECONDS,
    planned_seconds,
    reel_fps,
    reel_style,
    render_reel,
    timeline,
)
from .transitions import load_transition

BOUNDS = {"internal": (15, 120), "linkedin": (15, 90), "shorts": (15, 60), "email": (15, 60)}
# Containers whose keyframe index makes an input seek land exactly and whose audio timestamps
# are sample-exact, so a seeked decode yields the same frames and samples as a full decode.
SEEKABLE_FORMATS = {"mov,mp4,m4a,3gp,3g2,mj2"}
# Longest stretch of source a single video encode may cover. A clip is rendered one part at a
# time (see `parts`), so this, not the plan, bounds how many decoded frames can be alive at
# once: 20 s of 1080p24 is 480 frames, about 1.5 GB if every one of them were held, and in
# practice a streaming trim holds only the decoder's and encoder's own working set.
MAX_PART_SECONDS = Fraction(20)


class RecoveryError(RenderError):
    """Keep the job directory when the OS prevents restoring an existing result."""


@contextmanager
def staged_job(output):
    job = Path(tempfile.mkdtemp(prefix=".cliprender-", dir=output))
    preserve = False
    try:
        yield job
    except RecoveryError:
        preserve = True
        raise
    finally:
        if not preserve:
            shutil.rmtree(job)


def normalize_embedded(cues, origin):
    return [
        Cue(max(0, c.start - float(origin)), c.end - float(origin), c.text)
        for c in cues
        if c.end > origin
    ]


def number(value):
    return Fraction(str(value))


def decimal(value):
    return f"{float(value):.12f}"


def micro(value):
    """Whole microseconds of a time in seconds, the unit of the output timelines."""
    return round(value * 1_000_000)


def seconds_text(microseconds):
    """`microseconds` as an exact decimal seconds string for FFmpeg's time parser."""
    return f"{microseconds // 1_000_000}.{microseconds % 1_000_000:06d}"


def reject_constant(value):
    raise RenderError(f"Invalid JSON number: {value}; all numbers must be finite")


def load_plan(path, root):
    try:
        plan = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject_constant)
        schema = json.loads((root / "contract/edit-plan.schema.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError(f"Cannot read plan or contract schema: {exc}") from exc
    for error in Draft202012Validator(schema).iter_errors(plan):
        location = ".".join(str(p) for p in error.absolute_path) or "plan"
        raise RenderError(f"Schema error at {location}: {error.message}")

    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise RenderError("All plan numbers must be finite")
        if isinstance(value, dict):
            for child in value.values():
                finite(child)
        elif isinstance(value, list):
            for child in value:
                finite(child)

    finite(plan)
    ids = set()
    for clip in plan["clips"]:
        clip_id = clip["id"]
        if clip_id in ids:
            raise RenderError(f"Duplicate clip id: {clip_id}")
        ids.add(clip_id)
        # Contract stems must also be usable on Windows, including on other hosts.
        if clip_id.upper() in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *[f"COM{i}" for i in range(1, 10)],
            *[f"LPT{i}" for i in range(1, 10)],
        }:
            raise RenderError(f"Clip id {clip_id!r} is a reserved filename; use another id")
        for segment in clip["segments"]:
            if segment["end"] <= segment["start"]:
                raise RenderError(f"Clip {clip_id}: segment end must be greater than start")
            if segment["end"] > plan["source"].get("duration_seconds", math.inf):
                raise RenderError(f"Clip {clip_id}: segment exceeds source.duration_seconds")
    return plan


def resolve(root, value):
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def require_file(path, label):
    if not path.is_file():
        raise RenderError(f"Missing {label}: {path}; provide a readable local file")


def frame_tail(times, durations, index):
    """How long frame `index` is shown: its probed duration, else the previous frame spacing."""
    tail = durations[index]
    if tail <= 0:
        tail = float(times[index] - times[index - 1]) if index else 1 / 24
    return max(tail, 0.001)


def selections(clip, times, durations, origin, video_end):
    selected, expected = [], []
    offset = Fraction(0)
    for segment in clip["segments"]:
        start, end = number(segment["start"]), number(segment["end"])
        if end > video_end + Fraction(1, 1_000_000):
            raise RenderError(
                f"Clip {clip['id']}: segment end {float(end):g}s exceeds actual video end {float(video_end):g}s"
            )
        first, stop = bisect_left(times, origin + start), bisect_left(times, origin + end)
        if first == stop:
            raise RenderError(
                f"Clip {clip['id']}: segment [{float(start):g},{float(end):g}) contains no video frame"
            )
        selected.append((first, stop, start, end, offset))
        expected.extend(t - origin - start + offset for t in times[first:stop])
        offset += end - start
    return selected, expected, offset, frame_tail(times, durations, selected[-1][1] - 1)


def decode_window(selected, times, origin, audio, seekable):
    """Input `-ss`/`-t` flags so FFmpeg reads only the part of the source a clip needs.

    The graph trims by frame index, which is exact but on its own decodes the whole recording
    for every clip: about five minutes per clip for a 79-minute 1080p session on an 8-core ARM
    box. With `-copyts`, an input `-ss` keeps every timestamp, the demuxer seeks to the last
    keyframe at or before the point, and FFmpeg's accurate-seek trim discards decoded frames
    before it, so the frames reaching the graph start at index `base`. The point is the
    midpoint between the last unwanted frame and the first wanted one, so tick rounding
    cannot move a frame across it, and never later than the earliest segment start, so no
    selected audio is lost. `-t` stops reading a second after the last selected sample.
    Only MP4-family sources are seeked (see SEEKABLE_FORMATS); other containers keep the
    full decode. Verification still checks every output frame timestamp either way.

    Returns (flags, base, audio_base): the frame index and the audio sample index, both
    counted from the common origin, of the first data that reaches the graph.
    """
    if not seekable:
        return [], 0, 0
    earliest = origin + min(start for _, _, start, _, _ in selected)
    stop = origin + max(end for _, _, _, end, _ in selected) + 1
    base = min(first for first, *_ in selected)
    seek = None
    while base:
        midpoint = (times[base - 1] + times[base]) / 2
        # Keep at least one millisecond from both frames; the graph trims any extra frame.
        if midpoint <= earliest and times[base] - times[base - 1] >= Fraction(1, 500):
            seek = midpoint
            break
        base -= 1
    if seek is None:
        return ["-t", f"{float(stop - origin):.6f}"], 0, 0
    microseconds = round((seek - origin) * 1_000_000)
    audio_base = 0
    if audio:
        # av_rescale_q(microseconds, 1/1e6, 1/rate) with FFmpeg's round-half-away-from-zero:
        # the first sample its accurate-seek trim keeps.
        rate = int(audio["sample_rate"])
        audio_base = (2 * microseconds * rate + 1_000_000) // 2_000_000
    flags = [
        "-ss",
        f"{microseconds // 1_000_000}.{microseconds % 1_000_000:06d}",
        "-t",
        f"{float(stop - origin) - microseconds / 1_000_000:.6f}",
    ]
    return flags, base, audio_base


def parts(selected, times, origin, limit=None):
    """Cut every selected segment into decode parts of at most `limit` seconds of source.

    Why: a clip used to be one filter graph, `split` feeding one `trim` per segment into
    `interleave`. `interleave` emits a frame only once every input holds one, and the input
    for the last segment holds nothing until the decoder reaches that segment, so every
    decoded frame of every earlier segment waited in memory: one 37 s, four-segment 1080p
    clip was killed by the kernel at 2.2 GB. Rendering one part per process instead keeps
    a streaming pipeline (decode, trim, scale, encode) whose working set does not grow with
    the plan, and the parts are joined afterwards (`render_clip`). Segments longer than
    `limit` (MAX_PART_SECONDS by default) are cut further, at the presentation time of a
    source frame, so a part's length never depends on the plan either.

    Returns (first, stop, start, end, offset) tuples like the entries of `selected`, in
    output order: frame indices [first, stop), source seconds [start, end) and the output
    time at which the part begins. A cut lands exactly on `times[index] - origin`, so
    `bisect_left(times, origin + boundary)` names the first frame of the later part just as
    `selections` names a segment's; a part may run one frame past `limit` where the ideal
    cut falls between frames.
    """
    limit = MAX_PART_SECONDS if limit is None else limit
    result = []
    for first, stop, start, end, offset in selected:
        length = end - start
        pieces = max(1, math.ceil(length / limit))
        cuts = [first]
        for k in range(1, pieces):
            index = bisect_left(times, origin + start + length * k / pieces)
            if cuts[-1] < index < stop:
                cuts.append(index)
        cuts.append(stop)
        for a, b in pairwise(cuts):
            part_start = start if a == first else times[a] - origin
            part_end = end if b == stop else times[b] - origin
            result.append((a, b, part_start, part_end, offset + part_start - start))
    return result


def video_graph(part, origin, video_filter, base=0, captions=None):
    """One part's video: a frame-index trim, output-timeline timestamps, then geometry.

    `base` re-bases the frame indices when the input was seeked (see `decode_window`). The
    frame-index trim avoids FFmpeg rounding fractional seconds to input ticks. After
    `settb=AVTB` a timestamp is a whole number of microseconds, and the `setpts` constant is
    `origin + start - offset` as an exact microsecond count, the same value for every part of
    a segment: a part file carries the clip's final timestamps (`concat_list` then adds
    exactly zero). Why an integer rather than the single graph's `seconds/TB`: FFmpeg
    evaluates that division in floating point and truncates the result, so 3.3 s minus 2.45 s
    came out as 849999 µs; a microsecond is inside every tolerance here, but a part's first
    frame also sets the MP4 edit list that delays it, and FFmpeg 4.4 writes that list in
    milliseconds, turning the lost microsecond into a whole millisecond. Identical timestamps
    in every part also keep burned captions identical: the `subtitles` filter turns a frame's
    timestamp into milliseconds in floating point and truncates, so a cue edge that coincides
    with a frame time can flip between shown and hidden if the absolute timestamps differ at
    all; with the clip's one SRT and the clip's own timestamps in every part, nothing differs.
    Captions, when given, are that SRT's file name, burned after the geometry filters as before.
    """
    first, stop, start, _, offset = part
    graph = (
        f"[0:v:0]trim=start_frame={first - base}:end_frame={stop - base},settb=AVTB,"
        f"setpts=PTS-({micro(origin + start - offset)}),{video_filter}"
    )
    if captions:
        graph += f",subtitles=filename={captions}"
    return graph + "[video]"


def audio_graph(selected, origin, audio, audio_base=0):
    """The clip's audio in one pass: sample-exact cuts on the common timeline, joined in order.

    Timestamp gaps become silence up to the last selected sample; each segment is then cut at
    sample boundaries and the pieces are concatenated. `audio_base` re-bases the sample
    indices when the input was seeked. Returns the graph and the exact output sample count.
    """
    count = len(selected)
    rate = int(audio["sample_rate"])
    ranges = [(math.ceil(start * rate), math.ceil(end * rate)) for _, _, start, end, _ in selected]
    # Silence covers any audio gap up to the last selected sample; nothing later is needed.
    needed = max(stop for _, stop in ranges) - audio_base
    graph = [
        (
            f"[0:a:0]asetpts=PTS-({decimal(origin + Fraction(audio_base, rate))})/TB,"
            "aresample=async=1:first_pts=0:min_hard_comp=0,"
            f"apad=whole_len={needed},atrim=end_sample={needed}[anorm]"
        )
    ]
    if count > 1:
        graph.append(f"[anorm]asplit={count}" + "".join(f"[a{i}]" for i in range(count)))
    sample_count = 0
    for i, (first, stop) in enumerate(ranges):
        sample_count += stop - first
        source = f"a{i}" if count > 1 else "anorm"
        graph.append(
            f"[{source}]atrim=start_sample={first - audio_base}:end_sample={stop - audio_base},"
            f"asetpts=PTS-STARTPTS[{i}a]"
        )
    graph.append("".join(f"[{i}a]" for i in range(count)) + f"concat=n={count}:v=0:a=1[audio]")
    return ";\n".join(graph), sample_count


def concat_list(names, offsets, total):
    """A concat-demuxer script that passes every part's timestamps through unchanged.

    The part files already carry the clip's output timeline (`video_graph`). The demuxer adds
    `start_time - inpoint` to every packet of a file, where `start_time` is the sum of the
    previous files' `duration`s; by default `inpoint` is the file's own first timestamp, which
    would move each part to the end of the previous one and discard its lead-in. So every file
    gets a `duration` equal to the distance to the next part's output offset and an `inpoint`
    equal to its own offset: the two sums agree to the microsecond and the demuxer adds exactly
    zero. The declared durations also stop the demuxer from trusting the containers, whose
    lengths carry millisecond rounding on FFmpeg 4.4 and the last frame's guessed duration.
    An `inpoint` at or before a file's first frame only makes the demuxer seek to that first
    frame; no packet is dropped.
    """
    lines = ["ffconcat version 1.0"]
    ends = [*offsets[1:], total]
    for name, offset, end in zip(names, offsets, ends):
        lines += [
            f"file {name}",
            f"inpoint {seconds_text(micro(offset))}",
            f"duration {seconds_text(micro(end) - micro(offset))}",
        ]
    return "\n".join(lines) + "\n"


EXACT_TIMESTAMP = 0.000005
# Without `-movie_timescale` (FFmpeg 4.4) the MP4 edit list that delays a clip's first frame is
# expressed in the default millisecond movie timescale, so the whole video track lands up to
# one millisecond early. Frame spacing stays exact; only the presentation start is quantized.
MILLISECOND_START = 0.001 + EXACT_TIMESTAMP


@dataclass
class Session:
    """What every clip of a plan shares: the source, its timeline, the tools and their flags."""

    tools: Tools
    source: Path
    video: dict
    audio: dict | None
    times: list
    durations: list
    origin: Fraction
    seekable: bool
    timing_flags: list
    container_flags: list
    graph_flag: str

    def encoder_flags(self, tail):
        """The H.264 settings every part shares, so the parts can be joined without re-encoding.

        Identical settings give identical sequence/picture parameter sets, and `-bf 0` keeps
        decode order equal to presentation order, so the concat demuxer's stream copy is a
        valid, closed-GOP H.264 stream. The microsecond encoder and track time bases carry
        the exact frame timestamps; `tail` pins the last frame's duration where FFmpeg lets us.
        """
        return [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-x264-params",
            f"fps={self.video.get('r_frame_rate', '24/1')}:force-cfr=0",
            *self.timing_flags,
            "-enc_time_base:v",
            "1:1000000",
            "-video_track_timescale",
            "1000000",
            *self.container_flags,
            *self.tools.tail_duration_flags(tail),
        ]


def wav_sample_count(data):
    """Exact sample count of a probed WAV, whose stream duration is counted in samples."""
    stream = next(s for s in data["streams"] if s["codec_type"] == "audio")
    return int(stream["duration_ts"]) * Fraction(stream["time_base"]) * int(stream["sample_rate"])


def render_audio(session, workspace, selected):
    """Decode the clip's audio once, without video, into a float WAV; return (path, samples).

    Why a separate pass: audio is cheap to decode and small to hold, so the one graph that
    cuts every segment at sample boundaries and joins them stays exactly as it was, while the
    video, which is what fills memory, is rendered in parts. Why WAV rather than AAC per part:
    every AAC file carries encoder priming and a padded last frame, so stream-copying AAC
    pieces together opens gaps at the joins; the float WAV is lossless, and the clip's AAC is
    encoded once from it when the parts are joined, from the same samples as before. The
    sample count is checked against the plan before the join.
    """
    window, _, audio_base = decode_window(
        selected, session.times, session.origin, session.audio, session.seekable
    )
    graph, samples = audio_graph(selected, session.origin, session.audio, audio_base)
    script = workspace / "audio.txt"
    script.write_text(graph, encoding="utf-8")
    wav = workspace / "audio.wav"
    session.tools.encode(
        [
            "-y",
            "-copyts",
            *window,
            "-i",
            session.source,
            session.graph_flag,
            script,
            "-map",
            "[audio]",
            "-c:a",
            "pcm_f32le",
            wav,
        ],
        cwd=workspace,
    )
    count = wav_sample_count(session.tools.probe(wav))
    if count != samples:
        raise RenderError(
            f"audio pass produced {float(count):g} samples for the plan's {samples}; "
            "the source's audio timestamps cannot be cut sample-exactly"
        )
    return wav, samples


def render_clip(session, job, clip, selection, video_filter, burn_cues):
    """Render one clip in parts and join them; return (staged file, audio sample count).

    Each part (see `parts`) is decoded from an input seek just before it, trimmed by frame
    index and encoded on its own, so the memory a clip needs is that of one part, whatever
    the plan. The audio is rendered once (`render_audio`). The parts are then joined by the
    concat demuxer with the video stream copied, bit for bit, at the offsets `concat_list`
    fixes, and the audio encoded to AAC once. The result is verified by the caller exactly
    as a single-pass render was: every frame timestamp, the audio start and length, and a
    full decode. The part workspace is removed after the join, so disk use is bounded by
    one clip's parts.
    """
    selected, _, total, tail = selection
    workspace = job / "_parts"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir()
    audio_path = None
    samples = 0
    if session.audio:
        audio_path, samples = render_audio(session, workspace, selected)
    captions = None
    if burn_cues:
        # One SRT on the clip's timeline, burned by every part against the same timestamps.
        captions = "_captions.srt"
        (workspace / captions).write_text(format_srt(burn_cues), encoding="utf-8")
    names, offsets = [], []
    for index, part in enumerate(parts(selected, session.times, session.origin)):
        _, stop, _, _, offset = part
        name = f"part-{index:03d}.mp4"
        window, base, _ = decode_window(
            [part], session.times, session.origin, None, session.seekable
        )
        script = workspace / f"part-{index:03d}.txt"
        script.write_text(
            video_graph(part, session.origin, video_filter, base, captions), encoding="utf-8"
        )
        part_tail = frame_tail(session.times, session.durations, stop - 1)
        session.tools.encode(
            [
                "-y",
                "-copyts",
                *window,
                "-i",
                session.source,
                session.graph_flag,
                script,
                "-map",
                "[video]",
                "-an",
                *session.encoder_flags(part_tail),
                name,
            ],
            cwd=workspace,
        )
        names.append(name)
        offsets.append(offset)
    script = workspace / "parts.txt"
    script.write_text(concat_list(names, offsets, total), encoding="utf-8")
    rendered = job / f"{clip['id']}.mp4"
    # The join runs from the job directory, like every clip encode before it; the demuxer
    # resolves the list's `file` entries against the list's own directory.
    args = ["-y", "-copyts", "-f", "concat", "-safe", "1", "-auto_convert", "0"]
    args += ["-i", script.relative_to(job).as_posix()]
    if audio_path:
        args += ["-i", audio_path.relative_to(job).as_posix()]
    # The last frame's duration is pinned again here. An MP4 demuxer derives the duration of a
    # file's last packet from the stream duration, which excludes the edit list that delays the
    # first frame, so for a part that starts late in the clip that duration comes out zero and
    # the muxer would guess one from the average frame rate (0.3 s instead of 0.1 s on a
    # variable-rate fixture). Interior frames are unaffected: their durations are re-derived
    # from the following frame's timestamp, so this flag only decides where the clip ends.
    args += ["-map", "0:v:0", "-c:v", "copy", *session.tools.tail_duration_flags(tail)]
    args += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"] if audio_path else ["-an"]
    args += [
        "-video_track_timescale",
        "1000000",
        *session.container_flags,
        "-map_metadata",
        "-1",
        "-metadata",
        f"title={clip['takeaway']}",
        "-metadata",
        f"comment=hook_offset_seconds={clip.get('hook_offset_seconds', 0)}",
        "-movflags",
        "+faststart",
        rendered,
    ]
    session.tools.encode(args, cwd=job)
    shutil.rmtree(workspace)
    return rendered, samples


def verify(
    tools,
    path,
    dimensions,
    audio,
    expected,
    requested_duration,
    tail,
    samples,
    timestamp_tolerance=EXACT_TIMESTAMP,
):
    data = tools.probe(path)
    videos = [s for s in data["streams"] if s["codec_type"] == "video"]
    audios = [s for s in data["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1 or len(audios) != bool(audio):
        raise RenderError("Verification failed: output video/audio stream counts differ")
    video = videos[0]
    if (video["width"], video["height"]) != dimensions:
        raise RenderError("Verification failed: output dimensions differ")
    actual, _ = tools.frames(path, video["time_base"])
    if len(actual) != len(expected) or any(
        abs(a - e) > timestamp_tolerance for a, e in zip(actual, expected)
    ):
        raise RenderError(
            "Verification failed: retained video frame count/timestamps differ from the plan"
        )
    # Some MP4 demuxers report format.duration as the span AFTER the first
    # video timestamp on silent/VFR files. Preserve and measure the leading
    # presentation gap instead of subtracting it from the requested timeline.
    duration = max(float(s.get("start_time", 0)) + float(s["duration"]) for s in videos + audios)
    # Frame identity/count is checked separately; a duration tolerance cannot hide a missing segment.
    if abs(duration - float(requested_duration)) > tail + 0.025:
        raise RenderError(
            f"Verification failed: duration {duration:g}s differs from requested {float(requested_duration):g}s"
        )
    if audio:
        output_audio = audios[0]
        if int(output_audio["channels"]) != int(audio["channels"]):
            raise RenderError("Verification failed: audio channel count changed")
        if abs(float(output_audio.get("start_time", 0))) > 0.001:
            raise RenderError("Verification failed: unexpected output audio offset")
        expected_audio = samples / int(audio["sample_rate"])
        if abs(float(output_audio["duration"]) - expected_audio) > 0.025:
            raise RenderError("Verification failed: audio duration differs from selected samples")
    tools.encode(["-i", path, "-map", "0:v", "-map", "0:a?", "-f", "null", "-"])
    return duration


def publish(files, overwrite, backup):
    """Stage first; roll back a failed multi-file publication, preserving old results."""
    installed, saved = [], []
    try:
        for index, (source, dest) in enumerate(files):
            if overwrite and dest.exists():
                old = backup / f"previous-{index}"
                os.replace(dest, old)
                saved.append((old, dest))
            if overwrite:
                os.replace(source, dest)
            else:
                # Same-filesystem hard link atomically refuses a destination created by another job.
                os.link(source, dest)
            installed.append(dest)
    except BaseException as failure:
        recovery_errors = []
        for dest in reversed(installed):
            try:
                dest.unlink()
            except OSError as exc:
                recovery_errors.append(str(exc))
        for old, dest in reversed(saved):
            try:
                os.replace(old, dest)
            except OSError as exc:
                recovery_errors.append(str(exc))
        if recovery_errors:
            mapping = "; ".join(f"{old.name} -> {dest.name}" for old, dest in saved)
            raise RecoveryError(
                f"Publication failed and rollback was incomplete. Recovery files retained at {backup}. "
                f"Original-file mapping: {mapping}. Close applications using these files before recovery. "
                + "; ".join(recovery_errors)
            ) from failure
        raise


def render_plan(
    plan_path, *, root=None, ffmpeg="ffmpeg", ffprobe="ffprobe", timeout=1800, overwrite=False
):
    root = Path(root).resolve() if root else Path(__file__).resolve().parents[2]
    plan_path = Path(plan_path).resolve()
    plan = load_plan(plan_path, root)
    if not math.isfinite(timeout) or timeout <= 0:
        raise RenderError("--timeout must be finite and greater than zero")
    source = resolve(root, plan["source"]["path"])
    output = resolve(root, plan["output"]["dir"])
    require_file(source, "source video")
    source_snapshot = source.stat()
    spec = plan["output"]
    preset = spec.get("preset", "internal")
    aspect = spec.get("aspect", "9:16" if preset == "shorts" else "16:9")
    caption_kind = plan["source"].get("captions", {}).get("kind", "none")
    caption_mode = spec.get("captions", "burn_in") if caption_kind != "none" else "none"
    caption_source = None
    if caption_kind == "srt" and caption_mode != "none":
        caption_source = resolve(root, plan["source"]["captions"]["path"])
        require_file(caption_source, "SRT captions")
    summary = resolve(root, plan["summary"]["path"]) if "summary" in plan else None
    if summary:
        require_file(summary, "summary document")
    destinations = []
    for clip in plan["clips"]:
        destinations.append(output / f"{clip['id']}.mp4")
        if caption_mode == "sidecar_srt":
            destinations.append(output / f"{clip['id']}.srt")
    reel = spec.get("reel")
    reel_dest = output / reel.get("filename", "reel.mp4") if reel is not None else None
    if reel_dest:
        # The same collision rules as clips: a reel named after a clip is rejected below.
        destinations.append(reel_dest)
    # v1.2 options resolve now so a missing music file fails before any tool starts.
    style = reel_style(reel, lambda value: resolve(root, value)) if reel is not None else None
    music = style.music.path if style is not None and style.music is not None else None
    if music:
        require_file(music, "music bed")
    # A module transition is loaded now for the same reason: a missing or broken module, or a
    # renderer installed without numpy, must not cost the clips' encodes before it shows.
    module = style.module if style is not None else None
    draw = None
    if module:
        require_file(module, "transition module")
        draw = load_transition(module)
    summary_dest = output / summary.name if summary else None
    if summary and summary != summary_dest:
        destinations.append(summary_dest)
    elif summary_dest and summary_dest in destinations:
        raise RenderError("Summary path collides with a clip output")
    protected = {source, plan_path, caption_source, summary, music, module}
    if len(set(destinations)) != len(destinations):
        raise RenderError("Output filenames collide with one another")
    for dest in destinations:
        if dest.resolve() in protected or (
            dest.exists() and any(p and os.path.samefile(dest, p) for p in protected)
        ):
            raise RenderError(f"Output would overwrite an input: {dest}")
        if dest.exists() and (not overwrite or not dest.is_file()):
            raise RenderError(
                f"Output already exists: {dest}; use a different directory or --overwrite"
            )

    tools = Tools(ffmpeg, ffprobe, timeout)
    data = tools.probe(source)
    video, audio, raw_origin = inspect_media(data)
    if music and audio is None:
        raise RenderError(
            "Reel music needs a source with an audio track; a silent source gives a reel with "
            "no audio to mix the bed into"
        )
    origin = number(raw_origin)
    seekable = data.get("format", {}).get("format_name") in SEEKABLE_FORMATS and Fraction(
        video["time_base"]
    ) <= Fraction(1, 1000)
    times, durations = tools.frames(source, video["time_base"])
    last_duration = durations[-1] or (float(times[-1] - times[-2]) if len(times) > 1 else 1 / 24)
    video_end = times[-1] + number(last_duration) - origin
    planned = []
    for clip in plan["clips"]:
        selection = selections(clip, times, durations, origin, video_end)
        graph, dimensions = geometry(
            video, aspect, clip.get("crop_focus", "center"), spec.get("max_height", 1080)
        )
        planned.append((clip, selection, graph, dimensions))
        low, high = BOUNDS[preset]
        if not low <= selection[2] <= high:
            warn(
                f"clip {clip['id']} keeps {float(selection[2]):g}s; {preset} recommends {low}-{high}s"
            )
        if aspect != "16:9" and clip.get("crop_focus") == "speaker":
            warn(
                f"clip {clip['id']}: speaker tracking is unavailable; using the contract's center fallback"
            )
    reel_items = timeline(plan, reel) if reel is not None else []
    if reel is not None:
        planned_length = planned_seconds(reel_items, plan)
        if planned_length > MAX_RECOMMENDED_SECONDS:
            warn(
                f"reel keeps {float(planned_length):g}s; the contract recommends at most "
                f"{MAX_RECOMMENDED_SECONDS // 60} minutes"
            )
    session = Session(
        tools,
        source,
        video,
        audio,
        times,
        durations,
        origin,
        seekable,
        tools.timing_flags(),
        tools.container_flags(),
        tools.graph_flag(),
    )
    output.mkdir(parents=True, exist_ok=True)
    with staged_job(output) as job:
        cues = []
        if caption_mode != "none":
            if caption_source:
                cues = parse_srt(caption_source.read_text(encoding="utf-8-sig"))
            else:
                subtitles = [s for s in data["streams"] if s["codec_type"] == "subtitle"]
                if len(subtitles) != 1:
                    raise RenderError(
                        "Embedded captions need exactly one subtitle track; provide an SRT to select captions"
                    )
                # Preserve source PTS, then subtract the same media origin as A/V.
                text = tools.encode(
                    [
                        "-copyts",
                        "-i",
                        source,
                        "-map",
                        f"0:{subtitles[0]['index']}",
                        "-c:s",
                        "srt",
                        "-f",
                        "srt",
                        "-",
                    ]
                )
                cues = normalize_embedded(parse_srt(text), origin)
        files, records, rendered_clips = [], [], {}
        for clip, selection, video_filter, dimensions in planned:
            clip_id = clip["id"]
            _, expected, duration, tail = selection
            try:
                clip_cues = retime(cues, clip["segments"]) if caption_mode != "none" else []
                burn_cues = tidy_for_burn(clip_cues) if caption_mode == "burn_in" else []
                rendered, samples = render_clip(
                    session, job, clip, selection, video_filter, burn_cues
                )
                actual_duration = verify(
                    tools,
                    rendered,
                    dimensions,
                    audio,
                    expected,
                    duration,
                    tail,
                    samples,
                    EXACT_TIMESTAMP if session.container_flags else MILLISECOND_START,
                )
                target = output / rendered.name
                files.append((rendered, target))
                records.append((clip_id, target, actual_duration))
                rendered_clips[clip_id] = (rendered, actual_duration)
                if caption_mode == "sidecar_srt":
                    staged_srt = job / f"{clip_id}.srt"
                    staged_srt.write_text(format_srt(clip_cues), encoding="utf-8")
                    target = output / staged_srt.name
                    files.append((staged_srt, target))
                    records.append((clip_id, target, actual_duration))
            except (RenderError, ValueError, OSError) as exc:
                raise RenderError(
                    f"Clip {clip_id}: {exc}; no outputs from this run published"
                ) from exc
        if reel is not None:
            # One more artifact in the same transaction: cards are drawn at the clips' size,
            # every segment is conformed to the source's nominal rate, and the reel is verified
            # before anything, clips included, is published.
            try:
                staged_reel, reel_seconds = render_reel(
                    tools,
                    job,
                    reel_items,
                    rendered_clips,
                    planned[0][3],
                    reel_fps(video),
                    audio,
                    session.graph_flag,
                    tools.cfr_flags(),
                    reel_dest.name,
                    reel.get("intro", {}).get("title", reel_dest.stem),
                    style=style,
                    draw=draw,
                )
            except (RenderError, ValueError, OSError) as exc:
                raise RenderError(f"Reel: {exc}; no outputs from this run published") from exc
            files.append((staged_reel, reel_dest))
            records.append(("reel", reel_dest, reel_seconds))
        if summary and summary != summary_dest:
            staged_summary = job / "summary-copy"
            shutil.copyfile(summary, staged_summary)
            files.append((staged_summary, summary_dest))
            records.append(("summary", summary_dest, 0.0))
        final_snapshot = source.stat()
        if (source_snapshot.st_size, source_snapshot.st_mtime_ns) != (
            final_snapshot.st_size,
            final_snapshot.st_mtime_ns,
        ):
            raise RenderError("Source changed during rendering; retry with an unchanged source")
        publish(files, overwrite, job)
    return records
