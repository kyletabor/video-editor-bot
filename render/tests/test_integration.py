"""Blackbox CLI tests proving retained content and timing, beyond output duration."""

import json
import math
import os
import shutil
import subprocess
import sys
from array import array
from itertools import pairwise
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WIDTH, HEIGHT = 160, 90
RATE = 48000
pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Integration tests require FFmpeg and ffprobe on PATH",
)


def command(*args):
    result = subprocess.run(
        [str(arg) for arg in args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


@pytest.fixture(scope="module")
def timing_flags():
    help_text = command("ffmpeg", "-hide_banner", "-h", "full").decode(errors="replace")
    return ["-fps_mode", "passthrough"] if "-fps_mode" in help_text else ["-vsync", "0"]


def ffmpeg(*args):
    return command("ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", *args)


def probe(path):
    return json.loads(
        command(
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            path,
        )
    )


def frames(path):
    result = json.loads(
        command(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time,key_frame",
            "-of",
            "json",
            path,
        )
    )
    return result["frames"]


def luminances(path, timing_flags):
    raw = ffmpeg(
        "-i", path, "-map", "0:v:0", "-an", *timing_flags, "-pix_fmt", "gray", "-f", "rawvideo", "-"
    )
    info = next(s for s in probe(path)["streams"] if s["codec_type"] == "video")
    size = info["width"] * info["height"]
    assert len(raw) % size == 0
    return [sum(raw[pos : pos + size]) / size for pos in range(0, len(raw), size)]


def audio(path):
    raw = ffmpeg("-i", path, "-map", "0:a:0", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-")
    samples = array("f")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def rms(samples, start, end):
    window = samples[round(start * RATE) : round(end * RATE)]
    assert window
    return math.sqrt(sum(value * value for value in window) / len(window))


@pytest.fixture(scope="module")
def media(tmp_path_factory, timing_flags):
    folder = tmp_path_factory.mktemp("timing media with spaces")
    source = folder / "numbered-source.mp4"
    # Flat luminance advances every frame, making one-frame mistakes measurable.
    video = f"color=black:s={WIDTH}x{HEIGHT}:r=10:d=6,geq=lum=24+3*N:cb=128:cr=128"
    pulses = (
        "aevalsrc=0.5*sin(2*PI*880*t)*"
        "(between(t\\,1.3\\,1.4)+between(t\\,3.3\\,3.4)+between(t\\,4.6\\,4.7))"
        f":s={RATE}:d=6"
    )
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        video,
        "-f",
        "lavfi",
        "-i",
        pulses,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "12",
        "-g",
        "120",
        "-keyint_min",
        "120",
        "-sc_threshold",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        source,
    )

    delayed = folder / "nonzero-delayed.mkv"
    delayed_pulse = f"aevalsrc=0.5*sin(2*PI*880*t)*between(t\\,0.2\\,0.3):s={RATE}:d=2"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        video,
        "-f",
        "lavfi",
        "-i",
        delayed_pulse,
        "-filter_complex",
        "[0:v]setpts=PTS+5/TB[v];[1:a]asetpts=PTS+5.4/TB[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "12",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "pcm_s16le",
        *timing_flags,
        delayed,
    )

    silent_vfr = folder / "silent-vfr.mkv"
    ffmpeg(
        "-i",
        source,
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        "select='lt(t,2)+gte(t,2)*not(mod(n,3))'",
        *timing_flags,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "12",
        silent_vfr,
    )
    return {"source": source, "delayed": delayed, "vfr": silent_vfr}


def plan_file(tmp_path, source, segments, *, captions=None, mode="none"):
    plan = {
        "version": "1",
        "source": {"path": source.as_posix(), "captions": captions or {"kind": "none"}},
        "output": {"dir": (tmp_path / "results with spaces").as_posix(), "captions": mode},
        "clips": [
            {
                "id": "chosen",
                "takeaway": "Keep the explicitly selected content.",
                "segments": [{"start": start, "end": end} for start, end in segments],
                "trim_silence": False,
            }
        ],
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path, Path(plan["output"]["dir"]) / "chosen.mp4"


def cli(plan, *extra):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "render") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "cliprender.cli", str(plan), "--root", str(ROOT), *extra],
        cwd=plan.parent,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )


def assert_success(result, output, clip_id="chosen"):
    assert result.returncode == 0, result.stderr
    rows = [line.split("\t") for line in result.stdout.strip().splitlines()]
    assert rows
    for fields in rows:
        assert len(fields) == 3, result.stdout
        assert fields[0] in {clip_id, "summary"}
        assert Path(fields[1]).is_file()
        if fields[0] == "summary":
            assert float(fields[2]) == 0
        else:
            assert float(fields[2]) > 0
    assert sum(Path(fields[1]).resolve() == output.resolve() for fields in rows) == 1
    assert output.stat().st_size > 0
    ffmpeg("-xerror", "-i", output, "-map", "0:v", "-map", "0:a?", "-f", "null", "-")


def assert_selected_video(source, output, segments, timing_flags):
    origin = float(probe(source)["format"].get("start_time", 0))
    input_frames = frames(source)
    input_luma = luminances(source, timing_flags)
    expected_times, expected_luma = [], []
    offset = 0
    for start, end in segments:
        for frame, luma in zip(input_frames, input_luma, strict=True):
            time = float(frame["best_effort_timestamp_time"]) - origin
            if start - 1e-7 <= time < end - 1e-7:
                expected_times.append(time - start + offset)
                expected_luma.append(luma)
        offset += end - start
    actual_times = [float(f["best_effort_timestamp_time"]) for f in frames(output)]
    actual_luma = luminances(output, timing_flags)
    assert actual_times == pytest.approx(expected_times, abs=0.0001)
    assert actual_luma == pytest.approx(expected_luma, abs=1.5)
    assert all(b > a for a, b in pairwise(actual_times))


def test_reorders_between_keyframes_and_repeats_content_without_join_drift(
    tmp_path,
    media,
    timing_flags,
):
    source = media["source"]
    assert sum(f["key_frame"] for f in frames(source)) == 1
    segments = [(3.25, 3.65), (1.25, 1.65), (3.25, 3.65)]
    plan, output = plan_file(tmp_path, source, segments)
    assert_success(cli(plan), output)
    assert_selected_video(source, output, segments, timing_flags)
    samples = audio(output)
    for offset in (0, 0.4, 0.8):
        assert rms(samples, offset + 0.07, offset + 0.13) > 0.15
        assert rms(samples, offset + 0.23, offset + 0.33) < 0.015
    assert float(probe(output)["format"]["duration"]) == pytest.approx(1.2, abs=0.06)


def test_adjacent_half_open_ranges_do_not_duplicate_the_boundary(tmp_path, media, timing_flags):
    segments = [(1.2, 1.5), (1.5, 1.8)]
    plan, output = plan_file(tmp_path, media["source"], segments)
    assert_success(cli(plan), output)
    assert_selected_video(media["source"], output, segments, timing_flags)
    assert len(frames(output)) == 6


def test_subtick_boundaries_do_not_round_in_an_excluded_frame(tmp_path, media, timing_flags):
    # Matroska uses 1ms ticks; both boundaries lie immediately after source frames.
    # Rounding seconds to the nearest input tick would wrongly keep 1.2, omit 1.6.
    source = media["vfr"]
    segments = [(1.2001, 1.6001)]
    plan, output = plan_file(tmp_path, source, segments)
    assert_success(cli(plan), output)
    assert_selected_video(source, output, segments, timing_flags)
    assert len(frames(output)) == 4


def test_delayed_short_audio_preserves_common_nonzero_origin(tmp_path, media, timing_flags):
    source = media["delayed"]
    info = probe(source)
    assert float(info["format"]["start_time"]) == pytest.approx(5)
    stream = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert float(stream["start_time"]) == pytest.approx(5.4, abs=0.002)
    segments = [(0.1, 1.1), (3.0, 3.6)]
    plan, output = plan_file(tmp_path, source, segments)
    assert_success(cli(plan), output)
    assert_selected_video(source, output, segments, timing_flags)
    samples = audio(output)
    # Original marker is at source .6-.7, so it must occur at output .5-.6.
    assert rms(samples, 0.51, 0.59) > 0.15
    assert rms(samples, 0.11, 0.25) < 0.01
    assert rms(samples, 1.1, 1.5) < 0.01
    assert len(samples) / RATE == pytest.approx(1.6, abs=1024 / RATE + 0.002)


def test_silent_variable_rate_video_keeps_all_selected_timestamps(tmp_path, media, timing_flags):
    source = media["vfr"]
    source_times = [float(f["best_effort_timestamp_time"]) for f in frames(source)]
    assert len({round(b - a, 3) for a, b in pairwise(source_times)}) > 1
    segments = [(2.15, 3.65), (0.25, 0.85)]
    plan, output = plan_file(tmp_path, source, segments)
    assert_success(cli(plan), output)
    assert_selected_video(source, output, segments, timing_flags)
    assert [s["codec_type"] for s in probe(output)["streams"]] == ["video"]


def test_sidecar_subtitles_follow_reordered_intersections(tmp_path, media):
    subtitles = tmp_path / "captions.srt"
    subtitles.write_text(
        "1\n00:00:01,000 --> 00:00:01,600\nFIRST\n\n2\n00:00:03,000 --> 00:00:03,600\nPAYOFF\n",
        encoding="utf-8",
    )
    plan, output = plan_file(
        tmp_path,
        media["source"],
        [(3.25, 3.65), (1.25, 1.65)],
        captions={"kind": "srt", "path": subtitles.as_posix()},
        mode="sidecar_srt",
    )
    assert_success(cli(plan), output)
    sidecar = output.with_suffix(".srt").read_text(encoding="utf-8-sig")
    assert "00:00:00,000 --> 00:00:00,350\nPAYOFF" in sidecar
    assert "00:00:00,400 --> 00:00:00,750\nFIRST" in sidecar


def test_burned_subtitles_change_pixels_without_adding_a_subtitle_stream(
    tmp_path,
    media,
    timing_flags,
):
    subtitles = tmp_path / "captions.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nVISIBLE CAPTION\n", encoding="utf-8")
    plan, output = plan_file(
        tmp_path,
        media["source"],
        [(0, 1)],
        captions={"kind": "srt", "path": subtitles.as_posix()},
        mode="burn_in",
    )
    assert_success(cli(plan), output)
    assert not any(s["codec_type"] == "subtitle" for s in probe(output)["streams"])
    raw = ffmpeg(
        "-i", output, "-frames:v", "1", *timing_flags, "-pix_fmt", "gray", "-f", "rawvideo", "-"
    )
    # The fixture frame is flat gray; text must add bright glyphs inside the frame.
    assert max(raw) - min(raw) > 100
    assert not output.with_suffix(".srt").exists()


def test_burned_captions_accept_blank_lines_inside_a_cue(tmp_path, media, timing_flags):
    # The renderer must burn a speaker-separated cue (blank lines inside its text) rather
    # than reject the file; the written cue must also be accepted by FFmpeg's SRT reader.
    subtitles = tmp_path / "captions.srt"
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n(Kyle Tabor)\nFIRST\n-\n\n(Ramsey Jamoul)\nSECOND\n",
        encoding="utf-8",
    )
    plan, output = plan_file(
        tmp_path,
        media["source"],
        [(0, 1)],
        captions={"kind": "srt", "path": subtitles.as_posix()},
        mode="burn_in",
    )
    assert_success(cli(plan), output)
    raw = ffmpeg(
        "-i", output, "-frames:v", "1", *timing_flags, "-pix_fmt", "gray", "-f", "rawvideo", "-"
    )
    assert max(raw) - min(raw) > 100


def test_burned_captions_stay_aligned_when_the_source_is_seeked(tmp_path, media, timing_flags):
    # An MP4 segment starting after frame 0 is decoded through an input seek; the cue must
    # still land on the frames it belongs to, not shift by the seek offset.
    subtitles = tmp_path / "captions.srt"
    subtitles.write_text("1\n00:00:01,500 --> 00:00:02,000\nSEEKED CUE\n", encoding="utf-8")
    plan, output = plan_file(
        tmp_path,
        media["source"],
        [(1.25, 2.25)],
        captions={"kind": "srt", "path": subtitles.as_posix()},
        mode="burn_in",
    )
    assert_success(cli(plan), output)
    raw = ffmpeg(
        "-i", output, "-map", "0:v:0", *timing_flags, "-pix_fmt", "gray", "-f", "rawvideo", "-"
    )
    size = WIDTH * HEIGHT
    assert len(raw) == 10 * size
    spans = [
        max(frame) - min(frame) for frame in (raw[p : p + size] for p in range(0, len(raw), size))
    ]
    # Output frames sit at 0.05 s steps of 0.1 s; the cue covers output 0.25-0.75 s.
    # A seek-offset bug would shift the cue by ~12 frames, so the interior frames decide the
    # test; the two boundary frames (2 and 7) may land either way depending on how the
    # platform's subtitle renderer rounds the cue edges (Windows CI differs from Linux).
    visible = [span > 100 for span in spans]
    assert visible[:2] == [False, False], visible
    assert visible[3:7] == [True, True, True, True], visible
    assert visible[8:] == [False, False], visible


@pytest.mark.parametrize("segments", [[(1.201, 1.209)], [(5.9, 8.0)]])
def test_empty_frame_or_out_of_bounds_selection_writes_nothing(tmp_path, media, segments):
    plan, output = plan_file(tmp_path, media["source"], segments)
    result = cli(plan)
    assert result.returncode != 0
    assert result.stderr.strip()
    assert result.stdout == ""
    assert not output.exists()


def test_existing_result_is_preserved_until_explicit_overwrite(tmp_path, media):
    plan, output = plan_file(tmp_path, media["source"], [(0, 1)])
    output.parent.mkdir()
    sentinel = b"previous user result"
    output.write_bytes(sentinel)
    result = cli(plan)
    assert result.returncode != 0
    assert output.read_bytes() == sentinel
    assert_success(cli(plan, "--overwrite"), output)


def test_clip_named_captions_does_not_collide_with_sidecar_scratch_file(tmp_path, media):
    subtitles = tmp_path / "source-captions.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nKEEP THIS\n", encoding="utf-8")
    plan, output = plan_file(
        tmp_path,
        media["source"],
        [(0, 1)],
        captions={"kind": "srt", "path": subtitles.as_posix()},
        mode="sidecar_srt",
    )
    data = json.loads(plan.read_text(encoding="utf-8"))
    data["clips"][0]["id"] = "captions"
    plan.write_text(json.dumps(data), encoding="utf-8")
    output = output.with_name("captions.mp4")
    assert_success(cli(plan), output, clip_id="captions")
    assert "KEEP THIS" in output.with_suffix(".srt").read_text(encoding="utf-8")
    assert not list(output.parent.glob(".cliprender-*"))


def test_later_clip_render_failure_publishes_nothing_and_preserves_existing_files(
    tmp_path,
    media,
    monkeypatch,
):
    from cliprender.renderer import RenderError, Tools, render_plan

    plan, output = plan_file(tmp_path, media["source"], [(0, 1)])
    data = json.loads(plan.read_text(encoding="utf-8"))
    data["clips"].append(
        {
            "id": "later",
            "takeaway": "This clip will fail during encoding.",
            "segments": [{"start": 2, "end": 3}],
            "trim_silence": False,
        }
    )
    plan.write_text(json.dumps(data), encoding="utf-8")
    output.parent.mkdir()
    original = output.parent / "existing.mp4"
    original.write_bytes(b"keep this unrelated completed result")
    original_encode = Tools.encode
    saw_completed_first = []

    def fail_second(tools, args, *, cwd=None):
        if Path(str(args[-1])).name == "later.mp4":
            saw_completed_first.append((Path(cwd) / "chosen.mp4").is_file())
            raise RenderError("injected second-clip encoder failure")
        return original_encode(tools, args, cwd=cwd)

    monkeypatch.setattr(Tools, "encode", fail_second)
    with pytest.raises(RenderError, match="Clip later:.*second-clip encoder failure"):
        render_plan(plan, root=ROOT)
    assert saw_completed_first == [True]
    assert sorted(path.name for path in output.parent.iterdir()) == ["existing.mp4"]
    assert original.read_bytes() == b"keep this unrelated completed result"


@pytest.mark.parametrize("already_in_output", [False, True])
def test_summary_is_copied_or_preserved_in_place(tmp_path, media, already_in_output):
    plan, output = plan_file(tmp_path, media["source"], [(0, 1)])
    summary_dir = output.parent if already_in_output else tmp_path / "documents"
    summary_dir.mkdir(parents=True)
    summary = summary_dir / "summary.md"
    contents = b"# Summary\n\nAn exact byte-for-byte companion document.\n"
    summary.write_bytes(contents)
    before = summary.stat()
    data = json.loads(plan.read_text(encoding="utf-8"))
    data["summary"] = {"path": summary.as_posix()}
    plan.write_text(json.dumps(data), encoding="utf-8")
    result = cli(plan)
    assert_success(result, output)
    assert (output.parent / summary.name).read_bytes() == contents
    assert summary.read_bytes() == contents
    assert summary.stat().st_mtime_ns == before.st_mtime_ns
    records = [line.split("\t") for line in result.stdout.splitlines()]
    summary_records = [row for row in records if row[0] == "summary"]
    assert len(summary_records) == (0 if already_in_output else 1)
    assert not list(output.parent.glob(".cliprender-*"))


@pytest.fixture(scope="module")
def geometric_media(tmp_path_factory):
    folder = tmp_path_factory.mktemp("geometry media")
    wide = folder / "red-left-blue-right.mp4"
    rotated = folder / "rotated.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=red:s=320x180:r=10:d=1,drawbox=x=160:y=0:w=160:h=180:color=blue:t=fill",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "12",
        wide,
    )
    help_text = command("ffmpeg", "-hide_banner", "-h", "full").decode(errors="replace")
    if "-display_rotation" in help_text:
        ffmpeg("-display_rotation:v:0", "90", "-i", wide, "-c", "copy", rotated)
    else:
        ffmpeg("-i", wide, "-c", "copy", "-metadata:s:v:0", "rotate=90", rotated)
    return wide, rotated


def first_rgb(path):
    return ffmpeg("-i", path, "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-")


def test_rotation_is_applied_once_without_upscaling(tmp_path, geometric_media):
    _, rotated = geometric_media
    source_video = next(s for s in probe(rotated)["streams"] if s["codec_type"] == "video")
    rotation = source_video.get("tags", {}).get("rotate", 0)
    for side in source_video.get("side_data_list", []):
        rotation = side.get("rotation", rotation)
    assert abs(float(rotation)) == 90
    plan, output = plan_file(tmp_path, rotated, [(0, 1)])
    assert_success(cli(plan), output)
    video = next(s for s in probe(output)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (180, 320)
    raw = first_rgb(output)
    colors = []
    for row in (40, 280):
        offset = (row * 180 + 90) * 3
        rgb = raw[offset : offset + 3]
        colors.append(max(range(3), key=rgb.__getitem__))
    assert set(colors) == {0, 2}, "Rotation must move red/blue halves to top/bottom"


@pytest.mark.parametrize(("focus", "dominant"), [("left", 0), ("right", 2)])
def test_vertical_crop_honors_left_and_right_focus(tmp_path, geometric_media, focus, dominant):
    wide, _ = geometric_media
    plan, output = plan_file(tmp_path, wide, [(0, 1)])
    data = json.loads(plan.read_text(encoding="utf-8"))
    data["output"].update({"aspect": "9:16", "max_height": 720})
    data["clips"][0]["crop_focus"] = focus
    plan.write_text(json.dumps(data), encoding="utf-8")
    assert_success(cli(plan), output)
    video = next(s for s in probe(output)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (100, 180)
    raw = first_rgb(output)
    channels = [sum(raw[channel::3]) / (len(raw) / 3) for channel in range(3)]
    assert channels[dominant] > 200
    assert channels[2 - dominant] < 20


def test_process_timeout_is_actionable_and_leaves_source_and_outputs_untouched(tmp_path, media):
    source = media["source"]
    before = source.stat()
    plan, output = plan_file(tmp_path, source, [(0, 1)])
    result = cli(plan, "--timeout", "0.000001")
    assert result.returncode != 0
    assert "timed out" in result.stderr
    assert "--timeout" in result.stderr
    assert result.stdout == ""
    assert not output.exists()
    assert not list(output.parent.glob(".cliprender-*"))
    after = source.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
