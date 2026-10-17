"""The clipping guard of a reel with an overlapping transition: what each bridge holds.

Speech and music only sound together during the bridges of a dissolve or a module transition
(and a sample or few of a clip body at 29.97 fps), so the bound reads what each holds: the
speech after the cross-fade plus the bed heard there. The unit tests pin the arithmetic
(`wav_peak`, `music_bounds`, `headroom_gain`); the reels are rendered with the layout the
bot scores with `music_at: ends` (music under the first and last card runs, silence under
the chapter card between the clips) and with a bed under every card.

The fixture reel is the music tests' one with a chapter card before clip B as well: intro 4,
opening 5, card A 3, clip A 5, card B 3, clip B 5, closing 6 and outro 3 seconds at 10 fps, a
0.4 s transition at each of the seven joins. Card runs: 0-11.2 s (bed 0-11.2), card B alone
15.4-18.4 s (bed 11.2-14.2) and 22.6-31.2 s (bed 14.2-22.8). The speech is a hot tone (0.98,
about -0.2 dBFS) through clip A's middle and clip B's first second, and quiet (0.2) elsewhere:
so card B's bridge into clip B carries the loudest speech of the reel, and the others little.
"""

import json
import math
import struct
from fractions import Fraction
from pathlib import Path

import pytest
from test_integration import RATE, ROOT, audio, cli, ffmpeg, rms
from test_reel import rows
from test_reel_module import needs_numpy
from test_reel_music import music_plan, needs_tools, peak

import cliprender.reel
from cliprender.media import RenderError, Tools
from cliprender.reel import (
    HEADROOM_DB,
    PEAK_SLACK_DB,
    Music,
    Segment,
    Style,
    frame_count,
    headroom_gain,
    music_bounds,
    music_pieces,
    sample_count,
    transition_frames,
    wav_peak,
)
from cliprender.renderer import render_plan

CEILING = 10 ** (-HEADROOM_DB / 20)


def float_wav(path, frames, *, extensible=False):
    """A 32-bit float WAV of `frames` (one tuple of channel values per sample frame)."""
    channels = len(frames[0])
    data = struct.pack(f"<{channels * len(frames)}f", *(v for frame in frames for v in frame))
    block = 4 * channels
    fmt = struct.pack(
        "<HHIIHH", 0xFFFE if extensible else 3, channels, 8000, 8000 * block, block, 32
    )
    if extensible:
        # cbSize, valid bits, channel mask, then KSDATAFORMAT_SUBTYPE_IEEE_FLOAT.
        guid = bytes.fromhex("0300000000001000800000aa00389b71")
        fmt += struct.pack("<HHI", 22, 32, 3) + guid
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def test_wav_peak_reads_the_largest_sample_of_a_range_on_every_channel(tmp_path):
    frames = [(0.1, -0.2), (0.5, 0.0), (-0.25, -0.9), (0.0, 0.0)]
    for extensible in (False, True):
        path = float_wav(tmp_path / f"x{extensible}.wav", frames, extensible=extensible)
        assert wav_peak(path) == pytest.approx(0.9)
        assert wav_peak(path, 0, 2) == pytest.approx(0.5)
        assert wav_peak(path, 3, 4) == 0.0
        assert wav_peak(path, 2, 99) == pytest.approx(0.9)
        assert wav_peak(path, 5, 9) == 0.0
    # The renderer's own files: FFmpeg's float WAV header, whatever chunks it adds.
    tone = "aevalsrc=0.25*sin(2*PI*100*t):s=8000:d=0.1:c=stereo"
    ffmpeg("-f", "lavfi", "-i", tone, "-c:a", "pcm_f32le", tmp_path / "made.wav")
    assert wav_peak(tmp_path / "made.wav") == pytest.approx(0.25, abs=0.001)
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "0.1", tmp_path / "int.wav")
    with pytest.raises(RenderError, match="not 32-bit float"):
        wav_peak(tmp_path / "int.wav")


def test_music_bounds_add_the_bed_heard_over_each_bridge_and_skip_silence(tmp_path):
    def write(name, values):
        float_wav(tmp_path / name, [(v, -v) for v in values])

    write("bridge-00.wav", [0.1, 0.6, 0.2, 0.0])
    write("bridge-01.wav", [0.0, 0.95, 0.5, 0.1])
    write("piece-02.wav", [0.4] * 6)
    # Run one plays at reel samples 0-20, the first bridge at 10-14; run two at 26-40, silent
    # where the second bridge (30-34) is. Both are loud one sample outside each window, so a
    # window one sample early or wide would show.
    bed = [0.0] * 20
    bed[5], bed[9], bed[10:14], bed[14], bed[19] = 0.9, 0.8, [0.3, 0.2, 0.1, 0.05], 0.7, 0.6
    write("bed-00.wav", bed)
    write("bed-01.wav", [0.5, 0.0, 0.0, 0.45, 0.0, 0.0, 0.0, 0.0, 0.4] + [0.3] * 5)
    bridges = [Segment("bridge-00", 4, 4, start=10), Segment("bridge-01", 4, 4, start=30)]
    beds = [("bed-00.wav", 0, 20), ("bed-01.wav", 26, 40)]
    assert music_bounds(tmp_path, bridges, beds) == [pytest.approx(0.6 + 0.3)]
    # A segment between the runs (20-26) touches both and hears neither.
    between = Segment("piece-02", 6, 6, start=20)
    assert music_bounds(tmp_path, [between], beds) == []
    # `under: all`: the base stretch under the whole reel adds to all three, so the second
    # bridge counts, at its own speech peak plus that base.
    write("bed-base.wav", [0.05] * 40)
    beds.insert(0, ("bed-base.wav", 0, 40))
    assert music_bounds(tmp_path, [*bridges, between], beds) == [
        pytest.approx(0.6 + 0.05 + 0.3),
        pytest.approx(0.95 + 0.05),
        pytest.approx(0.4 + 0.05),
    ]


def test_at_29_97_fps_a_card_runs_music_starts_a_sample_inside_the_clip_before_it():
    """Why `music_bounds` reads the clip bodies as well as the bridges (veb-47p): a run's
    stretch is placed by rounding its start in seconds, the segments by whole-sample lengths.
    The layout is Talk #4's opening: intro, opening card, card A, clip A, card B, clip B."""
    durations = [Fraction(4), Fraction("7.5"), Fraction(3), Fraction("40.45")]
    durations += [Fraction(3), Fraction("52.45")]
    cards = [True, True, True, False, True, False]
    for fps, early in ((Fraction(24), 0), (Fraction(30), 0), (Fraction(30000, 1001), 1)):
        frames = [frame_count(seconds, fps) for seconds in durations]
        samples = [sample_count(count, fps, RATE) for count in frames]
        bridge = transition_frames(Fraction("0.4"), fps, frames)
        lengths = [Fraction(count) / fps for count in frames]
        run = music_pieces(Music(Path("bed")), lengths, cards, Fraction(bridge) / fps)[1]
        # Clip A's body ends, and its bridge into card B starts, after the first four pieces
        # less one bridge each.
        joint = sum(samples[:4]) - 4 * sample_count(bridge, fps, RATE)
        assert joint - round(run.start * RATE) == early


def test_music_bounds_count_the_samples_a_run_reaches_into_a_clip_body(tmp_path):
    # Clip A's body (reel samples 100-110) is hot at its third sample; card B's run starts a
    # sample early, at 109, with an unfaded cue onset (cue styles set `fade_seconds: 0`).
    float_wav(tmp_path / "piece-03.wav", [(v, v) for v in [0.2, 0.2, 0.95] + [0.2] * 6 + [0.3]])
    float_wav(tmp_path / "bed-01.wav", [(0.6, 0.6)] * 11)
    body = Segment("piece-03", 10, 10, start=100)
    assert music_bounds(tmp_path, [body], [("bed-01.wav", 109, 120)]) == [pytest.approx(0.9)]
    # `under: all`: the base stretch plays under the whole body too. The windows split at the
    # run's edge, so the onset adds to the leaked sample's speech, not to the loud one.
    float_wav(tmp_path / "bed-base.wav", [(0.05, 0.05)] * 200)
    beds = [("bed-base.wav", 0, 200), ("bed-01.wav", 109, 120)]
    assert music_bounds(tmp_path, [body], beds) == [
        pytest.approx(0.95 + 0.05),
        pytest.approx(0.3 + 0.05 + 0.6),
    ]


class Peaks:
    def __init__(self, db):
        self.db = db

    def audio_stats(self, path):
        return self.db, 48000


def test_headroom_gain_counts_only_the_bridges_that_carry_music(capsys):
    # Talk #4: Meet speech peaking at -0.1 dBFS, the cue at -4.5 dB, a module wipe.
    hot, bed = Peaks(-0.1), Music(Path("cues.wav"), gain_db=-4.5)
    style = Style("module", music=bed, module=Path("wipe.py"))
    # Unmeasured, every join is taken at its worst: the whole reel lowered by about 5 dB.
    worst = headroom_gain(hot, style, ["a"])
    assert worst.gain == pytest.approx(CEILING / (10 ** (-0.1 / 20) + 10 ** (-4.5 / 20)))
    assert "lowered by 5.0 dB" in capsys.readouterr().err
    # Measured: music only at the open and close, and those two bridges hold 0.63 and 0.65
    # (quiet speech fading in or out under the cue); the chapter cards between are silent.
    for bridges in ([], [0.63, 0.65]):
        mix = headroom_gain(hot, style, ["a"], bridges)
        assert mix.gain == 1.0
        assert mix.ceiling_db == pytest.approx(PEAK_SLACK_DB)
    assert capsys.readouterr().err == ""
    # A bridge whose speech and music do add up past the target lowers the reel by exactly it.
    mix = headroom_gain(hot, style, ["a"], [0.5, 1.2])
    assert mix.gain == pytest.approx(CEILING / 1.2)
    assert "lowered by 2.6 dB" in capsys.readouterr().err
    # `under: all` still counts the base bed under the whole of every clip.
    ducked = Style("dissolve", music=Music(Path("bed"), under="all", gain_db=-4.5, duck_db=-20))
    assert headroom_gain(hot, ducked, ["a"], []).gain == pytest.approx(
        CEILING / (10 ** (-0.1 / 20) + 10 ** (-20 / 20))
    )


# --- Through the CLI -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hot_talk(tmp_path_factory):
    folder = tmp_path_factory.mktemp("headroom media")
    video = "color=black:s=160x90:r=10:d=10,geq=lum=200-100*floor(T/5):cb=128:cr=128"
    hot = "(gte(t\\,1)*lt(t\\,4)+gte(t\\,5)*lt(t\\,6))"
    speech = f"aevalsrc=(0.2+0.78*{hot})*sin(2*PI*440*t):s={RATE}:d=10:c=stereo"
    source = folder / "talk.mp4"
    ffmpeg(
        *("-f", "lavfi", "-i", video, "-f", "lavfi", "-i", speech),
        *("-map", "0:v:0", "-map", "1:a:0"),
        *("-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-g", "100"),
        *("-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"),
        source,
    )
    beds = {}
    # As the bot writes them: a 16-bit WAV laid out run after run, the middle one silent
    # under `ends` (with a margin, so not one sample of the cues leaks into it).
    for name, gate in (("ends", "*(lt(t\\,11.1)+gte(t\\,14.3))"), ("cards", "")):
        beds[name] = folder / f"{name}.wav"
        tone = f"aevalsrc=0.8*sin(2*PI*220*t){gate}:s={RATE}:d=23:c=stereo"
        ffmpeg("-f", "lavfi", "-i", tone, "-c:a", "pcm_s16le", beds[name])
    module = folder / "wipe.py"
    module.write_text(
        "def render(a, b, t, state):\n"
        "    edge = round(t * a.shape[1])\n"
        "    out = a.copy()\n"
        "    out[:, :edge] = b[:, :edge]\n"
        "    return out\n",
        encoding="utf-8",
    )
    return {"source": source, "beds": beds, "module": module}


def headroom_plan(tmp_path, hot_talk, bed, kind):
    """The fixture reel's plan with `bed` under its cards at -4.5 dB and a 0.4 s `kind`
    transition, and its output folder."""
    talk = {"source": hot_talk["source"], "bed": hot_talk["beds"][bed]}
    plan, output = music_plan(tmp_path, talk, transition="dissolve")
    data = json.loads(plan.read_text(encoding="utf-8"))
    reel = data["output"]["reel"]
    reel["music"]["gain_db"] = -4.5
    if kind == "module":
        reel["transition"] = {
            "kind": "module",
            "module": hot_talk["module"].as_posix(),
            "seconds": 0.4,
        }
    data["clips"][1]["card"] = {"title": "Chapter two", "lines": ["the second half"], "seconds": 3}
    plan.write_text(json.dumps(data), encoding="utf-8")
    return plan, output


def headroom_reel(tmp_path, hot_talk, bed, kind):
    """Render that plan through the CLI; return the result, the reel and the published clips."""
    plan, output = headroom_plan(tmp_path, hot_talk, bed, kind)
    result = cli(plan)
    assert [fields[0] for fields in rows(result)] == ["first", "second", "reel"]
    return result, output / "reel.mp4", [output / "first.mp4", output / "second.mp4"]


def worst_case(clips, kind):
    """What the guard did before it measured the bridges, for the same clips and bed level."""
    style = Style(kind, music=Music(Path("bed"), gain_db=-4.5), module=Path("wipe.py"))
    return headroom_gain(Tools(timeout=60), style, clips).gain


@needs_tools
@pytest.mark.parametrize("kind", [pytest.param("module", marks=needs_numpy), "dissolve"])
def test_music_at_the_ends_leaves_a_reel_with_overlapping_joins_at_full_level(
    tmp_path, hot_talk, kind
):
    result, reel, clips = headroom_reel(tmp_path, hot_talk, "ends", kind)
    # The hot word at clip B's head sits in card B's bridge, but the score left card B quiet;
    # the joins that do carry music hold quiet speech. Nothing is lowered.
    assert "lowered" not in result.stderr
    samples = audio(reel)
    # Clip A plays 10.8-15.8 s (its hot middle at 11.8-14.8 s), clip B 18.0-23.0 s.
    assert rms(samples, 12.0, 14.5) == pytest.approx(0.98, rel=0.05)
    assert rms(samples, 19.5, 22.0) == pytest.approx(0.2, rel=0.05)
    assert rms(samples, 16.0, 18.0) < 0.003  # card B: no music, no speech
    assert rms(samples, 2.0, 3.0) == pytest.approx(0.8 * 10 ** (-4.5 / 20), rel=0.1)
    assert 0.9 < peak(reel) <= 10 ** (PEAK_SLACK_DB / 20)
    # Before, the loudest clip's peak was added to the bed at every join: about 5 dB down.
    assert worst_case(clips, kind) < 10 ** (-4 / 20)


@needs_tools
@needs_numpy
def test_a_bed_under_every_card_lowers_the_reel_only_as_far_as_its_loudest_bridge(
    tmp_path, hot_talk, monkeypatch, capsys
):
    # In process, so the test can see what the renderer hands the bound.
    plan, output = headroom_plan(tmp_path, hot_talk, "cards", "module")
    measured, original = [], cliprender.reel.music_bounds

    def record(workspace, segments, beds):
        measured.extend(segment.name for segment in segments)
        return original(workspace, segments, beds)

    monkeypatch.setattr(cliprender.reel, "music_bounds", record)
    records = render_plan(plan, root=ROOT)
    assert [record[0] for record in records] == ["first", "second", "reel"]
    reel, clips = output / "reel.mp4", [output / "first.mp4", output / "second.mp4"]
    # Every segment that carries speech is measured: the seven bridges and both clip bodies
    # (a run's music can reach into one, veb-47p), never the silent card bodies.
    bridges = [f"bridge-{k:02d}" for k in range(7)]
    assert measured == [*bridges[:3], "piece-03", *bridges[3:5], "piece-05", *bridges[5:]]
    # Card B's bridge into clip B now holds the hot word (0.98) under the last 0.4 s of card
    # B's music fading out over its second (at most 0.4 x 0.48): about 1.17, so 2.4 dB.
    stderr = capsys.readouterr().err
    line = next(line for line in stderr.splitlines() if "lowered by" in line)
    lowered = float(line.split("lowered by ")[1].split(" dB")[0])
    assert 1.9 < lowered < 2.9
    assert lowered < -20 * math.log10(worst_case(clips, "module")) - 2
    gain = 10 ** (-lowered / 20)
    samples = audio(reel)
    assert rms(samples, 12.0, 14.5) == pytest.approx(0.98 * gain, rel=0.05)
    assert rms(samples, 16.4, 17.4) == pytest.approx(0.8 * 10 ** (-4.5 / 20) * gain, rel=0.1)
    # The verification held the true peak under full scale plus the AAC slack, and the
    # loudest bridge sits at the -1 dBFS target or under it.
    assert peak(reel) <= CEILING * 10 ** (PEAK_SLACK_DB / 20)
