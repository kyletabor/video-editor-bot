"""Validate, render, verify and publish a v1/v1.1 edit plan without modifying its inputs."""

import json
import math
import os
import shutil
import sys
import tempfile
from bisect import bisect_left
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path

from jsonschema import Draft202012Validator

from .captions import Cue, format_srt, parse_srt, retime
from .media import RenderError, Tools, geometry, inspect_media
from .reel import MAX_RECOMMENDED_SECONDS, planned_seconds, reel_fps, render_reel, timeline

BOUNDS = {"internal": (15, 120), "linkedin": (15, 90), "shorts": (15, 60), "email": (15, 60)}
# Containers whose keyframe index makes an input seek land exactly and whose audio timestamps
# are sample-exact, so a seeked decode yields the same frames and samples as a full decode.
SEEKABLE_FORMATS = {"mov,mp4,m4a,3gp,3g2,mj2"}


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


def warn(message):
    print(f"cliprender: warning: {message}", file=sys.stderr)


def number(value):
    return Fraction(str(value))


def decimal(value):
    return f"{float(value):.12f}"


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
    last_index = selected[-1][1] - 1
    tail = durations[last_index]
    if tail <= 0:
        tail = float(times[last_index] - times[last_index - 1]) if last_index else 1 / 24
    return selected, expected, offset, max(tail, 0.001)


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


def filter_graph(selected, origin, video_filter, audio, burn, base=0, audio_base=0):
    """Build the graph; `base`/`audio_base` re-base indices when the input was seeked."""
    count = len(selected)
    graph = []
    if count > 1:
        graph.append(f"[0:v:0]split={count}" + "".join(f"[v{i}]" for i in range(count)))
    for i, (first, stop, start, end, offset) in enumerate(selected):
        source = f"v{i}" if count > 1 else "0:v:0"
        # Frame-index trims avoid FFmpeg rounding fractional seconds to input ticks.
        graph.append(
            f"[{source}]trim=start_frame={first - base}:end_frame={stop - base},settb=AVTB,"
            f"setpts=PTS-({decimal(origin + start - offset)})/TB[{i}v]"
        )
    if count > 1:
        graph.append(
            "".join(f"[{i}v]" for i in range(count))
            + f"interleave=nb_inputs={count}:duration=longest[vjoined]"
        )
        joined = "vjoined"
    else:
        joined = "0v"
    if burn:
        video_filter += ",subtitles=filename=_captions.srt"
    graph.append(f"[{joined}]{video_filter}[video]")
    sample_count = 0
    if audio:
        rate = int(audio["sample_rate"])
        ranges = [
            (math.ceil(start * rate), math.ceil(end * rate)) for _, _, start, end, _ in selected
        ]
        # Silence covers any audio gap up to the last selected sample; nothing later is needed.
        needed = max(stop for _, stop in ranges) - audio_base
        graph.append(
            f"[0:a:0]asetpts=PTS-({decimal(origin + Fraction(audio_base, rate))})/TB,"
            "aresample=async=1:first_pts=0:min_hard_comp=0,"
            f"apad=whole_len={needed},atrim=end_sample={needed}[anorm]"
        )
        if count > 1:
            graph.append(f"[anorm]asplit={count}" + "".join(f"[a{i}]" for i in range(count)))
        for i, (first, stop) in enumerate(ranges):
            sample_count += stop - first
            source = f"a{i}" if count > 1 else "anorm"
            graph.append(
                f"[{source}]atrim=start_sample={first - audio_base}:end_sample={stop - audio_base},"
                f"asetpts=PTS-STARTPTS[{i}a]"
            )
        graph.append("".join(f"[{i}a]" for i in range(count)) + f"concat=n={count}:v=0:a=1[audio]")
    return ";\n".join(graph), sample_count


def verify(tools, path, dimensions, audio, expected, requested_duration, tail, samples):
    data = tools.probe(path)
    videos = [s for s in data["streams"] if s["codec_type"] == "video"]
    audios = [s for s in data["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1 or len(audios) != bool(audio):
        raise RenderError("Verification failed: output video/audio stream counts differ")
    video = videos[0]
    if (video["width"], video["height"]) != dimensions:
        raise RenderError("Verification failed: output dimensions differ")
    actual, _ = tools.frames(path, video["time_base"])
    if len(actual) != len(expected) or any(abs(a - e) > 0.000005 for a, e in zip(actual, expected)):
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
    summary_dest = output / summary.name if summary else None
    if summary and summary != summary_dest:
        destinations.append(summary_dest)
    elif summary_dest and summary_dest in destinations:
        raise RenderError("Summary path collides with a clip output")
    protected = {source, plan_path, caption_source, summary}
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
    timing_flags = tools.timing_flags()
    container_flags = tools.container_flags()
    graph_flag = tools.graph_flag()
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
        for clip, (selected, expected, duration, tail), video_filter, dimensions in planned:
            clip_id = clip["id"]
            try:
                clip_cues = retime(cues, clip["segments"]) if caption_mode != "none" else []
                srt = job / "_captions.srt"
                srt.write_text(format_srt(clip_cues), encoding="utf-8")
                window, base, audio_base = decode_window(selected, times, origin, audio, seekable)
                graph, samples = filter_graph(
                    selected,
                    origin,
                    video_filter,
                    audio,
                    bool(clip_cues) and caption_mode == "burn_in",
                    base,
                    audio_base,
                )
                script = job / "filters.txt"
                script.write_text(graph, encoding="utf-8")
                rendered = job / f"{clip_id}.mp4"
                args = ["-y", "-copyts", *window, "-i", source, graph_flag, script]
                args += ["-map", "[video]"]
                args += ["-map", "[audio]", "-c:a", "aac", "-b:a", "192k"] if audio else ["-an"]
                args += [
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
                    f"fps={video.get('r_frame_rate', '24/1')}:force-cfr=0",
                    *timing_flags,
                    "-enc_time_base:v",
                    "1:1000000",
                    "-video_track_timescale",
                    "1000000",
                    *container_flags,
                    *tools.tail_duration_flags(tail),
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
                tools.encode(args, cwd=job)
                actual_duration = verify(
                    tools, rendered, dimensions, audio, expected, duration, tail, samples
                )
                target = output / rendered.name
                files.append((rendered, target))
                records.append((clip_id, target, actual_duration))
                rendered_clips[clip_id] = (rendered, actual_duration)
                if caption_mode == "sidecar_srt":
                    staged_srt = job / f"{clip_id}.srt"
                    shutil.copyfile(srt, staged_srt)
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
                    graph_flag,
                    tools.cfr_flags(),
                    reel_dest.name,
                    reel.get("intro", {}).get("title", reel_dest.stem),
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
