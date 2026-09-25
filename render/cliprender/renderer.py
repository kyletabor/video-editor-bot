"""Validate, render, verify and publish a v1 edit plan without modifying its inputs."""

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

BOUNDS = {"internal": (15, 120), "linkedin": (15, 90), "shorts": (15, 60), "email": (15, 60)}


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


def filter_graph(selected, origin, video_filter, audio, total_source_samples, burn):
    count = len(selected)
    graph = []
    if count > 1:
        graph.append(f"[0:v:0]split={count}" + "".join(f"[v{i}]" for i in range(count)))
    for i, (first, stop, start, end, offset) in enumerate(selected):
        source = f"v{i}" if count > 1 else "0:v:0"
        # Frame-index trims avoid FFmpeg rounding fractional seconds to input ticks.
        graph.append(
            f"[{source}]trim=start_frame={first}:end_frame={stop},settb=AVTB,"
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
        graph.append(
            f"[0:a:0]asetpts=PTS-({decimal(origin)})/TB,"
            "aresample=async=1:first_pts=0:min_hard_comp=0,"
            f"apad=whole_len={total_source_samples},atrim=end_sample={total_source_samples}[anorm]"
        )
        if count > 1:
            graph.append(f"[anorm]asplit={count}" + "".join(f"[a{i}]" for i in range(count)))
        for i, (_, _, start, end, _) in enumerate(selected):
            first, stop = math.ceil(start * rate), math.ceil(end * rate)
            sample_count += stop - first
            source = f"a{i}" if count > 1 else "anorm"
            graph.append(
                f"[{source}]atrim=start_sample={first}:end_sample={stop},asetpts=PTS-STARTPTS[{i}a]"
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
    timing_flags = tools.timing_flags()
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
        files, records = [], []
        for clip, (selected, expected, duration, tail), video_filter, dimensions in planned:
            clip_id = clip["id"]
            try:
                clip_cues = retime(cues, clip["segments"]) if caption_mode != "none" else []
                srt = job / "_captions.srt"
                srt.write_text(format_srt(clip_cues), encoding="utf-8")
                total_samples = math.ceil(video_end * int(audio["sample_rate"])) if audio else 0
                graph, samples = filter_graph(
                    selected,
                    origin,
                    video_filter,
                    audio,
                    total_samples,
                    bool(clip_cues) and caption_mode == "burn_in",
                )
                script = job / "filters.txt"
                script.write_text(graph, encoding="utf-8")
                rendered = job / f"{clip_id}.mp4"
                args = ["-y", "-copyts", "-i", source, graph_flag, script, "-map", "[video]"]
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
                    "-movie_timescale",
                    "1000000",
                    "-bsf:v",
                    f"setts=pts=PTS:dts=DTS:duration={max(1, round(tail * 1_000_000))}",
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
