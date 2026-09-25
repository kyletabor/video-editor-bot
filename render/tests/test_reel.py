"""Reel assembly through the real CLI: cards, order, continuity and the publish transaction."""

import json
import shutil
from fractions import Fraction
from pathlib import Path

import pytest
from test_integration import HEIGHT, RATE, ROOT, WIDTH, audio, cli, ffmpeg, plan_file, probe, rms

needs_tools = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Reel tests require FFmpeg and ffprobe on PATH",
)


def luma_planes(path, timing_flags):
    """Mean of each frame's raw Y plane, read without any pixel-format conversion.

    The reel carries an explicit limited-range tag (its cards were drawn in RGB), while the
    fixture and the clips leave the range unspecified; converting to `gray` would rescale one
    of them and hide the fact that the pixel values are identical.
    """
    info = next(s for s in probe(path)["streams"] if s["codec_type"] == "video")
    width, height = info["width"], info["height"]
    raw = ffmpeg(
        "-i",
        path,
        "-map",
        "0:v:0",
        "-an",
        *timing_flags,
        "-pix_fmt",
        "yuv420p",
        "-f",
        "rawvideo",
        "-",
    )
    frame, plane = width * height * 3 // 2, width * height
    assert len(raw) % frame == 0
    return [sum(raw[pos : pos + plane]) / plane for pos in range(0, len(raw), frame)]


def reel_plan(tmp_path, source, *, chapter_cards="auto", intro=True):
    """Two 5 s clips of the numbered fixture; the first carries a 3 s chapter card."""
    plan = {
        "version": "1",
        "source": {"path": source.as_posix(), "captions": {"kind": "none"}},
        "output": {
            "dir": (tmp_path / "reel out").as_posix(),
            "captions": "none",
            "reel": {"chapter_cards": chapter_cards},
        },
        "clips": [
            {
                "id": "first",
                "takeaway": "The first five seconds.",
                "segments": [{"start": 0, "end": 5}],
                "trim_silence": False,
                "card": {"title": "Chapter one", "lines": ["Frames 0 to 49"], "seconds": 3},
            },
            {
                "id": "second",
                "takeaway": "The last five seconds.",
                "segments": [{"start": 1, "end": 6}],
                "trim_silence": False,
            },
        ],
    }
    if intro:
        plan["output"]["reel"]["intro"] = {"title": "Intro", "lines": ["Two clips"], "seconds": 4}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path, Path(plan["output"]["dir"])


def rows(result):
    assert result.returncode == 0, result.stderr
    return [line.split("\t") for line in result.stdout.strip().splitlines()]


def assert_reel(path, seconds, *, with_audio=True):
    info = probe(path)
    assert [s["codec_type"] for s in info["streams"]] == (
        ["video", "audio"] if with_audio else ["video"]
    )
    video = info["streams"][0]
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", WIDTH, HEIGHT)
    assert float(info["format"]["duration"]) == pytest.approx(seconds, abs=0.4)
    for stream in info["streams"]:
        assert float(stream["duration"]) == pytest.approx(seconds, abs=0.4)
    ffmpeg("-xerror", "-i", path, "-map", "0:v", "-map", "0:a?", "-f", "null", "-")


@needs_tools
def test_reel_concatenates_intro_card_and_clips_in_order(tmp_path, media, timing_flags):
    plan, output = reel_plan(tmp_path, media["source"])
    result = cli(plan)
    records = rows(result)
    assert [fields[0] for fields in records] == ["first", "second", "reel"]
    reel = Path(records[-1][1])
    assert reel == output / "reel.mp4"
    assert float(records[-1][2]) == pytest.approx(17, abs=0.4)
    assert (output / "first.mp4").is_file() and (output / "second.mp4").is_file()
    assert_reel(reel, 17)
    assert not list(output.glob(".cliprender-*"))

    # Timeline: intro 0-4 s, card 4-7 s, first clip 7-12 s, second clip 12-17 s at 10 fps.
    # The fixture's flat luminance is 24 + 3 * source frame, so every reel frame identifies
    # the source frame it shows; cards are dark with a little text.
    luma = luma_planes(reel, timing_flags)
    assert len(luma) == pytest.approx(170, abs=1)
    assert all(value < 60 for value in luma[:70])
    for index in range(70, 120):
        assert luma[index] == pytest.approx(24 + 3 * (index - 70), abs=3.5)
    for index in range(120, 170):
        assert luma[index] == pytest.approx(24 + 3 * (index - 120 + 10), abs=3.5)

    # Audio is one continuous track: silence under the cards, the fixture's 880 Hz pulses
    # exactly where each clip places them (source 1.3 s, 3.3 s, 4.6 s).
    samples = audio(reel)
    assert len(samples) / RATE == pytest.approx(17, abs=0.1)
    assert rms(samples, 0.1, 6.9) < 0.01
    for start in (8.3, 10.3, 11.6, 12.3, 14.3, 15.6):
        assert rms(samples, start + 0.03, start + 0.08) > 0.15
    assert rms(samples, 9.0, 10.2) < 0.015
    assert rms(samples, 16.0, 16.9) < 0.015


@needs_tools
@pytest.mark.parametrize(("mode", "seconds"), [("all", 20), ("none", 14)])
def test_chapter_cards_all_synthesizes_and_none_drops(tmp_path, media, mode, seconds):
    plan, output = reel_plan(tmp_path, media["source"], chapter_cards=mode)
    records = rows(cli(plan))
    assert records[-1][0] == "reel"
    assert float(records[-1][2]) == pytest.approx(seconds, abs=0.4)
    assert_reel(output / "reel.mp4", seconds)


@needs_tools
def test_silent_source_gives_a_silent_reel_with_a_custom_filename(tmp_path, media):
    plan, output = reel_plan(tmp_path, media["vfr"], chapter_cards="all", intro=False)
    data = json.loads(plan.read_text(encoding="utf-8"))
    data["clips"] = [data["clips"][0] | {"segments": [{"start": 0, "end": 2}]}]
    data["output"]["reel"]["filename"] = "summary-video.mp4"
    data["output"]["reel"]["outro"] = {"title": "Done", "seconds": 2}
    plan.write_text(json.dumps(data), encoding="utf-8")
    records = rows(cli(plan))
    assert records[-1][:2] == ["reel", str(output / "summary-video.mp4")]
    assert_reel(output / "summary-video.mp4", 3 + 2 + 2, with_audio=False)


@needs_tools
def test_plan_without_reel_is_unchanged(tmp_path, media):
    plan, output = plan_file(tmp_path, media["source"], [(0, 1)])
    records = rows(cli(plan))
    assert [fields[0] for fields in records] == ["chosen"]
    assert sorted(path.name for path in output.parent.iterdir()) == ["chosen.mp4"]


@needs_tools
def test_reel_failure_publishes_no_clips(tmp_path, media, monkeypatch):
    import cliprender.reel
    from cliprender.renderer import RenderError, render_plan

    plan, output = reel_plan(tmp_path, media["source"])

    def fail(*args, **kwargs):
        raise RenderError("injected reel verification failure")

    monkeypatch.setattr(cliprender.reel, "verify_reel", fail)
    with pytest.raises(RenderError, match="Reel:.*injected reel verification failure"):
        render_plan(plan, root=ROOT)
    assert list(output.iterdir()) == []


def test_reel_named_after_a_clip_is_rejected_before_tools_start(tmp_path):
    from cliprender.renderer import RenderError, render_plan

    source = tmp_path / "source.mp4"
    source.write_bytes(b"never opened")
    plan = {
        "version": "1",
        "source": {"path": source.as_posix()},
        "output": {"dir": (tmp_path / "outputs").as_posix(), "reel": {"filename": "reel.mp4"}},
        "clips": [{"id": "reel", "takeaway": "Collides", "segments": [{"start": 0, "end": 1}]}],
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(RenderError, match="collide"):
        render_plan(path, root=ROOT, ffmpeg="must-not-start", ffprobe="must-not-start")
    assert not (tmp_path / "outputs").exists()


@needs_tools
def test_card_segment_matches_the_reel_format(tmp_path, timing_flags):
    from cliprender.cards import Card, encode_card_segment, write_card_png
    from cliprender.media import Tools

    tools = Tools(timeout=90)
    card = Card("Segment", ("one line",), Fraction(3), "1 of 1 · 0:00:00")
    png = write_card_png(card, (320, 180), tmp_path / "card.png")
    segment = encode_card_segment(
        tools,
        png,
        tmp_path / "card.mp4",
        card.seconds,
        "10/1",
        {"sample_rate": 48000, "channels": 2},
    )
    info = probe(segment)
    video, sound = info["streams"]
    assert (video["codec_type"], video["codec_name"], video["width"], video["height"]) == (
        "video",
        "h264",
        320,
        180,
    )
    assert (sound["codec_type"], sound["codec_name"], sound["sample_rate"], sound["channels"]) == (
        "audio",
        "aac",
        "48000",
        2,
    )
    for value in (info["format"]["duration"], video["duration"], sound["duration"]):
        assert float(value) == pytest.approx(3, abs=0.05)
    assert float(sound.get("start_time", 0)) == pytest.approx(0, abs=0.001)
    assert len(luma_planes(segment, timing_flags)) == 30
    ffmpeg("-xerror", "-i", segment, "-map", "0:v", "-map", "0:a", "-f", "null", "-")
    silent = encode_card_segment(tools, png, tmp_path / "silent.mp4", card.seconds, "10/1", None)
    assert [s["codec_type"] for s in probe(silent)["streams"]] == ["video"]
