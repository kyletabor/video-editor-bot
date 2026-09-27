"""Memory-bounded clip rendering: parts cut at frame boundaries, joined exactly, captions intact.

Why these tests exist: the single filter graph that rendered a clip (`split`, one `trim` per
segment, `interleave`) held every decoded frame of all but the last segment until the decoder
reached that segment, and a 37 s four-segment 1080p clip was killed at 2.2 GB. The renderer
now encodes one part at a time and joins the parts by stream copy. What must not change is
the output: the frames, their timestamps, the audio samples and the burned captions. The
legacy graph is kept here, verbatim, as the reference the new pipeline is compared against.
"""

import json
import math
import re
import shutil
from bisect import bisect_left
from fractions import Fraction
from itertools import pairwise
from pathlib import Path

import pytest
from test_integration import (
    HEIGHT,
    RATE,
    ROOT,
    WIDTH,
    assert_selected_video,
    assert_success,
    audio,
    cli,
    command,
    ffmpeg,
    frames,
    luminances,
    plan_file,
    probe,
    rms,
)

from cliprender import renderer
from cliprender.media import Tools, geometry, inspect_media
from cliprender.renderer import (
    Session,
    concat_list,
    decode_window,
    parts,
    render_plan,
    selections,
)

needs_tools = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Rendering tests require FFmpeg and ffprobe on PATH",
)


def ffmpeg_major():
    if not shutil.which("ffmpeg"):
        return 0
    first = command("ffmpeg", "-version").decode(errors="replace").splitlines()[0]
    match = re.search(r"version n?(\d+)\.", first)
    return int(match.group(1)) if match else 0


# ---------------------------------------------------------------------------------------
# Pure functions: where the cuts land and how the join places the parts.
# ---------------------------------------------------------------------------------------


def ten_fps(seconds):
    return [Fraction(i, 10) for i in range(seconds * 10)]


def test_parts_cut_a_45s_segment_into_three_at_frame_boundaries():
    times = ten_fps(60)
    selected = [(25, 475, Fraction(5, 2), Fraction(95, 2), Fraction(0))]
    result = parts(selected, times, Fraction(0))
    assert result == [
        (25, 175, Fraction(5, 2), Fraction(35, 2), Fraction(0)),
        (175, 325, Fraction(35, 2), Fraction(65, 2), Fraction(15)),
        (325, 475, Fraction(65, 2), Fraction(95, 2), Fraction(30)),
    ]
    for first, stop, start, end, _ in result:
        assert end - start <= renderer.MAX_PART_SECONDS
        # Every cut is a source frame time, so the frame index math of `selections` holds.
        assert bisect_left(times, start) == first
        assert bisect_left(times, end) == stop
    # Consecutive parts abut in frames, in source time and in output time.
    for earlier, later in pairwise(result):
        _, stop, start, end, offset = earlier
        assert later[0] == stop
        assert later[2] == end
        assert later[4] == offset + (end - start)


def test_parts_leave_short_segments_alone_and_keep_output_offsets():
    times = ten_fps(60)
    selected = [
        (100, 130, Fraction(10), Fraction(13), Fraction(0)),
        (0, 200, Fraction(0), Fraction(20), Fraction(3)),  # exactly the limit: one part
        (300, 501, Fraction(30), Fraction("50.1"), Fraction(23)),  # just over: two parts
    ]
    result = parts(selected, times, Fraction(0))
    assert result[:2] == selected[:2]
    assert [p[:2] for p in result[2:]] == [(300, 401), (401, 501)]
    assert result[2][2:] == (Fraction(30), Fraction("40.1"), Fraction(23))
    assert result[3][2:] == (Fraction("40.1"), Fraction("50.1"), Fraction(23) + Fraction("10.1"))


def test_parts_snap_to_real_frames_on_variable_frame_rate_and_nonzero_origin():
    origin = Fraction(5)
    # 0.1 s frames, then a stretch of 0.3 s frames, then 0.1 s again: cuts must be frame times.
    times = [origin + Fraction(i, 10) for i in range(200)]
    times += [times[-1] + Fraction(3 * i, 10) for i in range(1, 101)]
    times += [times[-1] + Fraction(i, 10) for i in range(1, 301)]
    start, end = Fraction(1), Fraction(75)
    first, stop = bisect_left(times, origin + start), bisect_left(times, origin + end)
    result = parts([(first, stop, start, end, Fraction(7))], times, origin, limit=Fraction(20))
    assert len(result) == 4
    assert result[0][0] == first and result[-1][1] == stop
    for a, b, part_start, part_end, offset in result:
        assert bisect_left(times, origin + part_start) == a
        assert bisect_left(times, origin + part_end) == b
        assert offset == Fraction(7) + part_start - start
        assert part_end - part_start <= Fraction(20) + Fraction(3, 10)
    assert sum(b - a for a, b, *_ in result) == stop - first


def test_concat_list_makes_the_demuxer_add_exactly_zero_to_every_part():
    offsets = [Fraction(0), Fraction("15.059"), Fraction("15.846")]
    text = concat_list(
        ["part-000.mp4", "part-001.mp4", "part-002.mp4"], offsets, Fraction("26.305")
    )
    assert text.splitlines() == [
        "ffconcat version 1.0",
        "file part-000.mp4",
        "inpoint 0.000000",
        "duration 15.059000",
        "file part-001.mp4",
        "inpoint 15.059000",
        "duration 0.787000",
        "file part-002.mp4",
        "inpoint 15.846000",
        "duration 10.459000",
    ]
    # Sub-microsecond offsets round once each, and each file's inpoint equals the sum of the
    # durations before it, so the demuxer's `start_time - inpoint` is zero for every part.
    thirds = concat_list(
        ["a", "b", "c"], [Fraction(0), Fraction(1, 3), Fraction(2, 3)], Fraction(1)
    )
    fields = [line.split() for line in thirds.splitlines()[1:]]
    inpoints = [Fraction(value) for key, value in fields if key == "inpoint"]
    durations = [Fraction(value) for key, value in fields if key == "duration"]
    assert [str(d) for d in durations] == ["333333/1000000", "166667/500000", "333333/1000000"]
    for index, inpoint in enumerate(inpoints):
        assert inpoint == sum(durations[:index], Fraction(0))


# ---------------------------------------------------------------------------------------
# Real renders.
# ---------------------------------------------------------------------------------------


def recording_encodes(monkeypatch):
    """Wrap `Tools.encode` to record every FFmpeg command the renderer runs, with its graph.

    The graph script is read when the command runs: the renderer deletes a clip's part
    workspace once the parts are joined, so it cannot be read afterwards.
    """
    seen = []
    original = Tools.encode

    def record(tools, args, *, cwd=None):
        words = [str(a) for a in args]
        graph = None
        for flag in ("-filter_complex_script", "-/filter_complex"):
            if flag in words:
                graph = Path(words[words.index(flag) + 1]).read_text(encoding="utf-8")
        seen.append({"args": words, "graph": graph})
        return original(tools, args, cwd=cwd)

    monkeypatch.setattr(Tools, "encode", record)
    return seen


def part_encodes(seen):
    return [run for run in seen if re.fullmatch(r"part-\d{3}\.mp4", run["args"][-1])]


def joins(seen):
    return [run for run in seen if "concat" in run["args"] and "-c:v" in run["args"]]


@pytest.fixture(scope="module")
def long_media(tmp_path_factory):
    """A 50 s numbered source: luminance steps every frame, pulses across the future cuts.

    A 45 s segment starting at 2.5 s is cut at 17.5 s and 32.5 s; the pulses straddle those
    times so a gap or a repeat at a join would move or duplicate them.
    """
    folder = tmp_path_factory.mktemp("long media")
    source = folder / "long-source.mp4"
    video = f"color=black:s={WIDTH}x{HEIGHT}:r=10:d=50,geq=lum=24+3*mod(N\\,64):cb=128:cr=128"
    pulses = (
        "aevalsrc=0.5*sin(2*PI*880*t)*"
        "(between(t\\,5.3\\,5.4)+between(t\\,17.45\\,17.55)+between(t\\,32.45\\,32.55))"
        f":s={RATE}:d=50"
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
    return source


@needs_tools
def test_a_45s_segment_renders_in_three_parts_and_joins_exactly(
    tmp_path, long_media, timing_flags, monkeypatch
):
    segments = [(2.5, 47.5)]
    plan, output = plan_file(tmp_path, long_media, segments)
    seen = recording_encodes(monkeypatch)
    records = render_plan(plan, root=ROOT)
    assert len(part_encodes(seen)) == 3
    assert len(joins(seen)) == 1
    assert [(row[0], Path(row[1])) for row in records] == [("chosen", output)]
    assert len(frames(output)) == 450
    assert_selected_video(long_media, output, segments, timing_flags)
    assert records[0][2] == pytest.approx(45.0, abs=0.1 + 0.025)
    samples = audio(output)
    assert len(samples) / RATE == pytest.approx(45.0, abs=1024 / RATE + 0.002)
    for centre in (2.85, 15.0, 30.0):
        assert rms(samples, centre - 0.04, centre + 0.04) > 0.15
        assert rms(samples, centre - 0.12, centre - 0.07) < 0.015
        assert rms(samples, centre + 0.07, centre + 0.12) < 0.015
    ffmpeg("-xerror", "-i", output, "-map", "0:v", "-map", "0:a", "-f", "null", "-")
    assert not list(output.parent.glob(".cliprender-*"))


def legacy_filter_graph(selected, origin, video_filter, audio, burn, base=0, audio_base=0):
    """The single graph the renderer used before parts: split, trim per segment, interleave.

    Kept as the reference for the comparison test; it is the code that ran out of memory.
    """

    def decimal(value):
        return f"{float(value):.12f}"

    count = len(selected)
    graph = []
    if count > 1:
        graph.append(f"[0:v:0]split={count}" + "".join(f"[v{i}]" for i in range(count)))
    for i, (first, stop, start, end, offset) in enumerate(selected):
        source = f"v{i}" if count > 1 else "0:v:0"
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


def legacy_render(source, segments, workdir):
    """Render `segments` of `source` the way the renderer did before parts: one graph, one encode."""
    tools = Tools()
    data = tools.probe(source)
    video, audio_info, raw_origin = inspect_media(data)
    origin = Fraction(str(raw_origin))
    times, durations = tools.frames(source, video["time_base"])
    video_end = times[-1] + Fraction(str(durations[-1])) - origin
    clip = {"id": "legacy", "segments": [{"start": a, "end": b} for a, b in segments]}
    selected, expected, _, tail = selections(clip, times, durations, origin, video_end)
    video_filter, _ = geometry(video, "16:9", "center", 1080)
    window, base, audio_base = decode_window(selected, times, origin, audio_info, True)
    graph, samples = legacy_filter_graph(
        selected, origin, video_filter, audio_info, False, base, audio_base
    )
    script = workdir / "legacy.txt"
    script.write_text(graph, encoding="utf-8")
    session = Session(
        tools,
        source,
        video,
        audio_info,
        times,
        durations,
        origin,
        True,
        tools.timing_flags(),
        tools.container_flags(),
        tools.graph_flag(),
    )
    rendered = workdir / "legacy.mp4"
    tools.encode(
        [
            "-y",
            "-copyts",
            *window,
            "-i",
            source,
            session.graph_flag,
            script,
            "-map",
            "[video]",
            "-map",
            "[audio]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            *session.encoder_flags(tail),
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            rendered,
        ],
        cwd=workdir,
    )
    return rendered, expected, samples


def pulse_edges(samples, threshold=0.2):
    """Sample indices where the signal first and last exceeds `threshold`."""
    above = [i for i, value in enumerate(samples) if abs(value) > threshold]
    assert above
    return above[0], above[-1]


@needs_tools
@pytest.mark.skipif(
    ffmpeg_major() < 5,
    reason="FFmpeg 4.4's interleave drops the last frame of the single-graph reference itself",
)
def test_parts_reproduce_the_single_graph_render_of_a_reordered_repeated_clip(
    tmp_path, media, timing_flags
):
    # Three segments, out of source order, one repeated: the case the single graph handled
    # by buffering. Every frame must sit at the same timestamp, and the audio within 1 ms.
    segments = [(3.25, 3.65), (1.25, 1.65), (3.25, 3.65)]
    plan, output = plan_file(tmp_path, media["source"], segments)
    assert_success(cli(plan), output)
    reference, expected, samples = legacy_render(media["source"], segments, tmp_path)
    new_times = [Fraction(f["best_effort_timestamp_time"]) for f in frames(output)]
    old_times = [Fraction(f["best_effort_timestamp_time"]) for f in frames(reference)]
    assert len(new_times) == len(old_times) == len(expected) == 12
    # The same offsets, and the join adds exactly zero. The legacy graph divided seconds by
    # the time base in floating point and FFmpeg truncated the result, so it could land a
    # microsecond short where the parts' integer constant does not; nothing else may differ.
    assert all(abs(a - b) <= Fraction(1, 1_000_000) for a, b in zip(new_times, old_times))
    assert all(abs(a - e) <= Fraction(1, 1_000_000) for a, e in zip(new_times, expected))
    # The last frame keeps its pinned source duration through the stream copy, so both files
    # end at the same instant (the plan's length plus that frame's remaining display time).
    assert float(probe(output)["format"]["duration"]) == pytest.approx(
        float(probe(reference)["format"]["duration"]), abs=0.0011
    )
    assert luminances(output, timing_flags) == pytest.approx(
        luminances(reference, timing_flags), abs=1.5
    )
    new_audio, old_audio = audio(output), audio(reference)
    assert abs(len(new_audio) - len(old_audio)) <= RATE // 1000
    assert abs(len(new_audio) - samples) <= 1024 + RATE // 1000
    new_first, new_last = pulse_edges(new_audio)
    old_first, old_last = pulse_edges(old_audio)
    assert abs(new_first - old_first) <= RATE // 1000
    assert abs(new_last - old_last) <= RATE // 1000
    # The one AAC encode reads the same samples the single graph fed its encoder.
    common = min(len(new_audio), len(old_audio))
    assert max(abs(a - b) for a, b in zip(new_audio[:common], old_audio[:common])) < 0.05


def visible_frames(path, timing_flags):
    raw = ffmpeg(
        "-i", path, "-map", "0:v:0", *timing_flags, "-pix_fmt", "gray", "-f", "rawvideo", "-"
    )
    size = WIDTH * HEIGHT
    assert len(raw) % size == 0
    return [max(raw[p : p + size]) - min(raw[p : p + size]) > 100 for p in range(0, len(raw), size)]


@needs_tools
def test_a_part_boundary_inside_a_cue_changes_neither_burned_nor_sidecar_captions(
    tmp_path, media, timing_flags, monkeypatch
):
    # The clip keeps source 1.25-3.25 s; the cue 1.8-2.6 s lands at output 0.55-1.35 s. With
    # one-second parts the encoder restarts at source 2.25 s (output 1.0 s), inside the cue.
    subtitles = tmp_path / "captions.srt"
    subtitles.write_text(
        "1\n00:00:01,800 --> 00:00:02,600\nALPHA BRAVO CHARLIE DELTA ECHO FOXTROT\n",
        encoding="utf-8",
    )
    captions = {"kind": "srt", "path": subtitles.as_posix()}
    outputs = {}
    for label, limit in (("whole", None), ("split", Fraction(1))):
        folder = tmp_path / label
        (folder / "sidecar").mkdir(parents=True)
        if limit is not None:
            monkeypatch.setattr(renderer, "MAX_PART_SECONDS", limit)
        seen = recording_encodes(monkeypatch)
        plan, burned = plan_file(
            folder, media["source"], [(1.25, 3.25)], captions=captions, mode="burn_in"
        )
        render_plan(plan, root=ROOT)
        expected_parts = 2 if limit else 1
        assert len(part_encodes(seen)) == expected_parts
        # Every part that shows the cue burned it: the SRT went with the part.
        assert sum("subtitles=" in run["graph"] for run in part_encodes(seen)) == expected_parts
        plan, sidecar = plan_file(
            folder / "sidecar",
            media["source"],
            [(1.25, 3.25)],
            captions=captions,
            mode="sidecar_srt",
        )
        render_plan(plan, root=ROOT)
        outputs[label] = (burned, sidecar.with_suffix(".srt"))
    whole, split = outputs["whole"], outputs["split"]
    whole_visible = visible_frames(whole[0], timing_flags)
    split_visible = visible_frames(split[0], timing_flags)
    assert len(whole_visible) == len(split_visible) == 20
    assert whole_visible == split_visible
    # Frames 6-12 (output 0.55-1.35 s) carry the caption; the boundary frame 10 is one of them.
    assert split_visible[6:13] == [True] * 7, split_visible
    assert not any(split_visible[:5]) and not any(split_visible[14:]), split_visible
    # Same text on both sides of the boundary: mean luminance per frame matches the whole render.
    assert luminances(split[0], timing_flags) == pytest.approx(
        luminances(whole[0], timing_flags), abs=1.5
    )
    whole_times = [f["best_effort_timestamp_time"] for f in frames(whole[0])]
    split_times = [f["best_effort_timestamp_time"] for f in frames(split[0])]
    assert [float(t) for t in split_times] == pytest.approx(
        [float(t) for t in whole_times], abs=5e-6
    )
    assert split[1].read_text(encoding="utf-8") == whole[1].read_text(encoding="utf-8")
    assert "ALPHA BRAVO CHARLIE DELTA ECHO FOXTROT" in split[1].read_text(encoding="utf-8")


@needs_tools
def test_a_failed_part_or_join_publishes_nothing(tmp_path, media, monkeypatch):
    plan, output = plan_file(tmp_path, media["source"], [(3.25, 3.65), (1.25, 1.65)])
    original = Tools.encode

    def fail_join(tools, args, *, cwd=None):
        if "concat" in [str(a) for a in args]:
            raise renderer.RenderError("injected join failure")
        return original(tools, args, cwd=cwd)

    monkeypatch.setattr(Tools, "encode", fail_join)
    with pytest.raises(renderer.RenderError, match="Clip chosen:.*injected join failure"):
        render_plan(plan, root=ROOT)
    assert not output.parent.exists() or not list(output.parent.iterdir())


def test_plan_file_helper_round_trips(tmp_path):
    # Guard for the helpers above: a plan written for one segment names one clip.
    plan, output = plan_file(tmp_path, Path("unused.mp4"), [(0, 1)])
    assert json.loads(plan.read_text(encoding="utf-8"))["clips"][0]["id"] == "chosen"
    assert output.name == "chosen.mp4"
