"""Contract v1.2 reels through the real CLI: opening and closing cards, a music bed under the
card runs, dip and dissolve transitions, audio fades, and the clipping guard.

The fixture is a 10 s "talk" whose two 5 s halves become the two clips. Its speech is a tone
with one silent second inside each clip, so a test can hear whether the bed is playing under
speech. The bed is louder in its first four seconds than in its last four, so a test can tell
which part of the bed a card run is playing and prove that the music progresses through the
reel instead of restarting. Cut layout in reel seconds: intro 0-4, opening 4-9, chapter card
9-12, clip A 12-17, clip B 17-22, closing 22-28, outro 28-31. The intro, the opening and the
first chapter card are consecutive, so they form one card run (0-12 s); closing plus outro
form the other (22-31 s). After the mono downmix in `audio()` a tone's RMS equals its amplitude.
"""

import json
import shutil
import sys
from array import array
from pathlib import Path

import pytest
from test_integration import HEIGHT, RATE, ROOT, WIDTH, audio, cli, command, ffmpeg, rms
from test_reel import assert_reel, luma_planes, rows

needs_tools = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Reel tests require FFmpeg and ffprobe on PATH",
)


@pytest.fixture(scope="module")
def talk(tmp_path_factory):
    folder = tmp_path_factory.mktemp("reel music media")
    source = folder / "talk.mp4"
    # Luma 200 for the first half, 100 for the second, so a dissolve between the clips shows.
    video = f"color=black:s={WIDTH}x{HEIGHT}:r=10:d=10,geq=lum=200-100*floor(T/5):cb=128:cr=128"
    speech = (
        "aevalsrc=0.5*sin(2*PI*440*t)*(1-between(t\\,2\\,3)-between(t\\,7\\,8))"
        f":s={RATE}:d=10:c=stereo"
    )
    ffmpeg(
        *("-f", "lavfi", "-i", video, "-f", "lavfi", "-i", speech),
        *("-map", "0:v:0", "-map", "1:a:0"),
        *("-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-g", "100"),
        *("-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"),
        source,
    )
    tone = f"aevalsrc=(0.8-0.4*gte(t\\,4))*sin(2*PI*220*t):s={RATE}:d=8:c=stereo"
    encoders = command("ffmpeg", "-hide_banner", "-encoders").decode(errors="replace")
    if "libmp3lame" in encoders:
        bed = folder / "bed.mp3"
        ffmpeg("-f", "lavfi", "-i", tone, "-c:a", "libmp3lame", "-b:a", "192k", bed)
    else:
        bed = folder / "bed.wav"
        ffmpeg("-f", "lavfi", "-i", tone, "-c:a", "pcm_s16le", bed)
    return {"source": source, "bed": bed}


def music_plan(tmp_path, talk, *, music=True, transition=None, under="cards", loop=True, duck=-30):
    reel = {
        "intro": {"title": "Reel", "lines": ["intro"], "seconds": 4},
        "opening": [{"title": "What you'll learn", "lines": ["two things"], "seconds": 5}],
        "chapter_cards": "auto",
        "closing": [{"title": "Takeaways", "lines": ["one", "two"], "seconds": 6}],
        "outro": {"title": "Bye", "seconds": 3},
    }
    if music:
        reel["music"] = {
            "path": talk["bed"].as_posix(),
            "under": under,
            "gain_db": -18,
            "duck_db": duck,
            "fade_seconds": 1.0,
            "loop": loop,
        }
    if transition:
        reel["transition"] = {"kind": transition, "seconds": 0.4}
    plan = {
        "version": "1",
        "source": {"path": talk["source"].as_posix(), "captions": {"kind": "none"}},
        "output": {"dir": (tmp_path / "reel out").as_posix(), "captions": "none", "reel": reel},
        "clips": [
            {
                "id": "first",
                "takeaway": "The first half.",
                "segments": [{"start": 0, "end": 5}],
                "trim_silence": False,
                "card": {"title": "Chapter one", "lines": ["the first half"], "seconds": 3},
            },
            {
                "id": "second",
                "takeaway": "The second half.",
                "segments": [{"start": 5, "end": 10}],
                "trim_silence": False,
            },
        ],
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path, Path(plan["output"]["dir"])


def ramp_windows(samples, around, level=0.5):
    """Number of 50 ms windows within 0.3 s of `around` whose RMS lies between silence and the
    tone. A 0.15 s linear fade shows at least two, a hard cut at most one. Card segments run
    about 20 ms over their nominal length (their silent AAC track rounds up to whole frames and
    `concat` follows the longest stream), so windows pinned to nominal junctions would be brittle.
    """
    starts = [around - 0.3 + k * 0.05 for k in range(12)]
    return sum(0.12 * level < rms(samples, s, s + 0.05) < 0.88 * level for s in starts)


def peak(path):
    """True sample peak of the audio track, without the mono downmix `audio()` applies."""
    raw = ffmpeg("-i", path, "-map", "0:a:0", "-f", "f32le", "-")
    samples = array("f")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return max(abs(value) for value in samples)


def render(tmp_path, talk, **options):
    plan, output = music_plan(tmp_path, talk, **options)
    result = cli(plan)
    records = rows(result)
    assert [fields[0] for fields in records] == ["first", "second", "reel"]
    return result, output / "reel.mp4", float(records[-1][2])


@needs_tools
def test_music_plays_under_every_card_run_and_never_under_speech(tmp_path, talk):
    result, reel, seconds = render(tmp_path, talk)
    assert seconds == pytest.approx(31, abs=0.4)
    assert_reel(reel, 31)
    samples = audio(reel)
    # Bed at -18 dB: amplitude 0.8 * 0.126 = 0.10 in its loud half, half that in its quiet half.
    loud, quiet = rms(samples, 1.5, 3.5), rms(samples, 5.0, 7.0)
    assert 0.07 < loud < 0.14
    assert quiet == pytest.approx(loud / 2, rel=0.25)
    # Faded in over the first second of the intro; continuous across the opening card and the
    # chapter card (one run, no restart: the 8 s bed has looped back to its loud half by 9 s);
    # faded out over the last second of the chapter card.
    assert rms(samples, 0.0, 0.1) < 0.3 * loud
    assert rms(samples, 8.9, 9.0) == pytest.approx(loud, rel=0.25)
    assert rms(samples, 10.2, 10.8) == pytest.approx(loud, rel=0.25)
    assert rms(samples, 11.9, 12.0) < 0.3 * loud
    # Closing plus outro continue at bed offset 12 s: quiet until reel 26 s, then loud. Had the
    # bed followed reel time instead (22 s = 6 s into a loop), 24.5-25.5 s would be loud.
    assert rms(samples, 24.5, 25.5) == pytest.approx(quiet, rel=0.25)
    assert rms(samples, 26.5, 27.5) == pytest.approx(loud, rel=0.25)
    assert rms(samples, 30.9, 31.0) < 0.3 * quiet
    # No music under speech: the silent second inside each clip stays silent.
    assert rms(samples, 14.2, 14.8) < 0.003
    assert rms(samples, 19.2, 19.8) < 0.003
    # Clip audio fades in and out over 0.15 s at every cut (clip A in at 12 s, A out and B in
    # around 17 s, B out at 22 s) instead of cutting.
    assert rms(samples, 13.0, 14.0) > 0.45 and rms(samples, 18.0, 19.0) > 0.45
    assert ramp_windows(samples, 12.0) >= 2
    assert ramp_windows(samples, 17.0) >= 3
    assert ramp_windows(samples, 22.0) >= 2
    assert peak(reel) <= 1.0
    # No headroom reduction, no shortened transition, no exhausted bed (the clips are shorter
    # than the preset likes, and that warning is the renderer's business, not this test's).
    for unexpected in ("lowered", "shortened", "loop is off"):
        assert unexpected not in result.stderr


@needs_tools
def test_v12_reel_without_music_keeps_the_cards_silent_and_still_fades_speech(tmp_path, talk):
    _, reel, seconds = render(tmp_path, talk, music=False)
    assert seconds == pytest.approx(31, abs=0.4)
    samples = audio(reel)
    for start, end in ((1.5, 3.5), (10.2, 10.8), (24.5, 25.5), (14.2, 14.8)):
        assert rms(samples, start, end) < 0.003
    assert rms(samples, 13.0, 14.0) > 0.45 and ramp_windows(samples, 12.0) >= 2


@needs_tools
def test_dip_fades_the_video_to_black_at_every_join_and_keeps_the_length(
    tmp_path, talk, timing_flags
):
    _, reel, seconds = render(tmp_path, talk, transition="dip")
    assert seconds == pytest.approx(31, abs=0.4)
    assert_reel(reel, 31)
    luma = luma_planes(reel, timing_flags)
    assert len(luma) == pytest.approx(310, abs=1)
    # At 10 fps each 0.2 s half-fade spans two frames. Clip A (luma 200) is frames 120-169,
    # clip B (luma 100) 170-219; limited-range black is 16.
    assert luma[120] < 30 and luma[121] < 150 and luma[125] > 185
    assert luma[168] > 185 and luma[169] < 150
    assert luma[170] < 30 and luma[175] == pytest.approx(100, abs=6)
    assert luma[219] < 80
    # Dip is video only: the speech fades are the 0.15 s edge fades, nothing more.
    samples = audio(reel)
    middle = rms(samples, 13.0, 14.0)
    assert middle > 0.45 and ramp_windows(samples, 12.0) >= 2
    assert rms(samples, 12.3, 12.8) == pytest.approx(middle, rel=0.1)
    assert rms(samples, 1.5, 3.5) > 0.07


@needs_tools
def test_dissolve_overlaps_the_segments_and_shortens_the_reel(tmp_path, talk, timing_flags):
    _, reel, seconds = render(tmp_path, talk, transition="dissolve")
    expected = 31 - 0.4 * 6
    assert seconds == pytest.approx(expected, abs=0.4)
    assert_reel(reel, expected)
    luma = luma_planes(reel, timing_flags)
    # FFmpeg 4.4's xfade emits one frame more than 7.0.2 over the whole chain.
    assert len(luma) == pytest.approx(286, abs=1.5)
    # Segments start at 0, 3.6, 8.2, 10.8, 15.4, 20.0 and 25.6 s; clip A (luma 200) dissolves
    # into clip B (luma 100) over 15.4-15.8 s.
    assert luma[150] > 185 and luma[165] == pytest.approx(100, abs=6)
    assert any(115 < value < 185 for value in luma[153:159])
    samples = audio(reel)
    # Card runs: 0-11.2 s (bed 0-11.2) and 20.0-28.6 s (bed 11.2-19.8): the bed's quiet half
    # at 22-24 s (bed 13.2-15.2), its loud half at 25-27 s (bed 16.2-18.2).
    loud = rms(samples, 1.5, 3.5)
    assert 0.07 < loud < 0.14
    assert rms(samples, 22.0, 24.0) == pytest.approx(loud / 2, rel=0.25)
    assert rms(samples, 25.0, 27.0) == pytest.approx(loud, rel=0.25)
    # The silent second of clip A now sits at 12.8-13.8 s and is still silent under `cards`.
    assert rms(samples, 13.0, 13.6) < 0.003
    # Speech cross-fades in from the chapter card over 0.4 s (10.8-11.2 s) rather than cutting
    # or fading over 0.15 s: many more intermediate windows than a short fade shows.
    assert rms(samples, 11.5, 12.5) > 0.45 and ramp_windows(samples, 11.0) >= 4
    assert peak(reel) <= 1.0


@needs_tools
def test_music_under_all_ducks_below_speech_and_rises_under_cards(tmp_path, talk):
    result, reel, seconds = render(tmp_path, talk, under="all", duck=-20)
    assert seconds == pytest.approx(31, abs=0.4)
    samples = audio(reel)
    loud = rms(samples, 1.5, 3.5)
    assert 0.07 < loud < 0.14
    # The bed follows reel time: its quiet half at -20 dB inside clip A's silent second
    # (bed 6.2-6.8 s), its loud half inside clip B's (bed 19.2 s = 3.2 s after a loop).
    assert rms(samples, 14.2, 14.8) == pytest.approx(0.4 * 0.1, rel=0.3)
    assert rms(samples, 19.2, 19.8) == pytest.approx(0.8 * 0.1, rel=0.3)
    assert peak(reel) <= 1.0
    assert "lowered" not in result.stderr


@needs_tools
def test_music_that_may_not_loop_runs_out_with_a_warning(tmp_path, talk):
    result, reel, _ = render(tmp_path, talk, loop=False)
    assert "loop is off" in result.stderr
    samples = audio(reel)
    assert rms(samples, 1.5, 3.5) > 0.07
    # The bed is 8 s long: the first card run (0-12 s) loses its last four seconds and the
    # closing run gets nothing.
    assert rms(samples, 8.2, 8.9) < 0.003
    assert rms(samples, 10.2, 10.8) < 0.003
    assert rms(samples, 24.5, 25.5) < 0.003


@needs_tools
def test_music_needs_a_source_with_an_audio_track(tmp_path, media, talk):
    plan, output = music_plan(tmp_path, talk)
    data = json.loads(plan.read_text(encoding="utf-8"))
    data["source"]["path"] = media["vfr"].as_posix()
    data["clips"] = [data["clips"][0] | {"segments": [{"start": 0, "end": 2}]}]
    plan.write_text(json.dumps(data), encoding="utf-8")
    result = cli(plan)
    assert result.returncode == 1
    assert "audio track" in result.stderr
    assert not output.exists()


def test_missing_music_bed_is_rejected_before_tools_start(tmp_path):
    from cliprender.renderer import RenderError, render_plan

    source = tmp_path / "source.mp4"
    source.write_bytes(b"never opened")
    plan = {
        "version": "1",
        "source": {"path": source.as_posix()},
        "output": {
            "dir": (tmp_path / "outputs").as_posix(),
            "reel": {"music": {"path": (tmp_path / "nowhere/bed.mp3").as_posix()}},
        },
        "clips": [{"id": "only", "takeaway": "One", "segments": [{"start": 0, "end": 1}]}],
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(RenderError, match="music bed"):
        render_plan(path, root=ROOT, ffmpeg="must-not-start", ffprobe="must-not-start")
    assert not (tmp_path / "outputs").exists()
