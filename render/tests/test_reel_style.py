"""Contract v1.2 reel rules that need no media tools: style defaults, timeline, transition and
music arithmetic, the graph text, and the headroom rule."""

from fractions import Fraction
from pathlib import Path

import pytest

from cliprender.media import RenderError, parse_audio_stats
from cliprender.reel import (
    HEADROOM_DB,
    PEAK_SLACK_DB,
    Mix,
    Music,
    Piece,
    Style,
    card_runs,
    concat_graph,
    effective_transition,
    headroom_gain,
    music_pieces,
    reel_graph,
    reel_seconds,
    reel_style,
    segment_starts,
    timeline,
)

AUDIO = {"sample_rate": 48000, "channels": 2}
# The reel of the music tests: intro, opening, chapter card, two clips, closing, outro.
DURATIONS = [Fraction(n) for n in (4, 5, 3, 5, 5, 6, 3)]
CARDS = [True, True, True, False, False, True, True]


def test_v11_plan_has_no_style_and_keeps_the_v11_graph_byte_for_byte():
    assert reel_style({"intro": {"title": "Intro"}, "chapter_cards": "all"}, Path) is None
    video = (
        "[{i}:v:0]scale=160:90:force_original_aspect_ratio=decrease,"
        "pad=160:90:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=10/1:start_time=0,format=yuv420p[v{i}]"
    )
    sound = (
        "[{i}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        "asetpts=PTS-STARTPTS[a{i}]"
    )
    # The graph the v1.1 renderer wrote for a two-input stereo reel, verbatim.
    golden = ";\n".join(
        [
            video.format(i=0),
            sound.format(i=0),
            video.format(i=1),
            sound.format(i=1),
            "[v0][a0][v1][a1]concat=n=2:v=1:a=1[video][audio]",
        ]
    )
    assert concat_graph(2, (160, 90), Fraction(10), AUDIO) == golden
    assert reel_graph(2, (160, 90), Fraction(10), AUDIO) == golden
    silent = reel_graph(1, (160, 90), Fraction(24), None)
    assert silent.endswith("format=yuv420p[v0];\n[v0]concat=n=1:v=1:a=0[video]")
    assert "[a0]" not in silent


def test_style_applies_schema_defaults_and_resolves_the_bed(tmp_path):
    assert reel_style({"opening": []}, lambda value: tmp_path / value) == Style()
    reel = {
        "music": {"path": "assets/music/bed.mp3", "under": "all", "loop": False},
        "transition": {"kind": "dip"},
        "audio_fade_seconds": 0,
    }
    style = reel_style(reel, lambda value: tmp_path / value)
    assert style.transition == "dip" and style.transition_seconds == Fraction("0.4")
    assert style.audio_fade == 0
    assert style.music == Music(tmp_path / "assets/music/bed.mp3", "all", -18.0, -30.0, 1, False)
    assert style.music.gain == pytest.approx(0.1259, abs=0.0005)
    assert style.music.duck == pytest.approx(0.0316, abs=0.0005)


def test_timeline_places_opening_and_closing_cards_without_chapter_footers():
    plan = {
        "clips": [
            {"id": "a", "takeaway": "A", "segments": [{"start": 10, "end": 15}]},
            {"id": "b", "takeaway": "B", "segments": [{"start": 60, "end": 66}]},
        ]
    }
    reel = {
        "intro": {"title": "Intro"},
        "opening": [{"title": "What you'll learn", "seconds": 5}],
        "chapter_cards": "all",
        "closing": [{"title": "Takeaways", "seconds": 6}, {"title": "Next steps"}],
        "outro": {"title": "Bye"},
    }
    items = timeline(plan, reel)
    titles = [item if isinstance(item, str) else item.title for item in items]
    expected = ["Intro", "What you'll learn", "A", "a", "B", "b", "Takeaways", "Next steps", "Bye"]
    assert titles == expected
    footers = [item.footer for item in items if not isinstance(item, str)]
    assert footers == ["", "", "1 of 2 · 0:00:10", "2 of 2 · 0:01:00", "", "", ""]
    assert items[1].seconds == 5 and items[6].seconds == 6


def test_transition_length_is_clamped_to_what_the_segments_can_hold():
    durations = [Fraction(4), Fraction(1), Fraction(5)]
    assert effective_transition(None, durations) == 0
    assert effective_transition(Style("cut", Fraction(1)), durations) == 0
    assert effective_transition(Style("dip", Fraction("1.5")), durations) == 1
    assert effective_transition(Style("dip", Fraction("0.4")), durations) == Fraction("0.4")
    assert effective_transition(Style("dissolve", Fraction("1.5")), durations) == Fraction(1, 2)
    assert effective_transition(Style("dissolve", Fraction("0.4")), durations) == Fraction("0.4")
    assert effective_transition(Style("dissolve", Fraction(1)), [Fraction(5)]) == 0
    assert segment_starts(durations, Fraction("0.5")) == [0, Fraction("3.5"), 4]
    assert reel_seconds(durations, Fraction("0.5")) == 9
    assert reel_seconds(durations, Fraction(0)) == 10


def test_music_pieces_follow_the_card_runs_with_a_running_offset():
    assert card_runs(DURATIONS, CARDS, Fraction(0)) == [(0, 12), (22, 31)]
    bed = Music(Path("bed.wav"))
    assert music_pieces(bed, DURATIONS, CARDS, Fraction(0)) == [
        Piece(Fraction(0), Fraction(0), Fraction(12), bed.gain),
        Piece(Fraction(12), Fraction(22), Fraction(9), bed.gain),
    ]
    everything = Music(Path("bed.wav"), under="all")
    assert music_pieces(everything, DURATIONS, CARDS, Fraction(0)) == [
        Piece(Fraction(0), Fraction(0), Fraction(31), everything.duck),
        Piece(Fraction(0), Fraction(0), Fraction(12), everything.gain - everything.duck),
        Piece(Fraction(22), Fraction(22), Fraction(9), everything.gain - everything.duck),
    ]
    # With a 0.4 s dissolve the segments start at 0, 3.6, 8.2, 10.8, 15.4, 20.0 and 25.6 s.
    overlap = Fraction("0.4")
    assert card_runs(DURATIONS, CARDS, overlap) == [(0, Fraction("11.2")), (20, Fraction("28.6"))]
    assert reel_seconds(DURATIONS, overlap) == Fraction("28.6")
    assert card_runs([Fraction(5)], [False], Fraction(0)) == []
    assert music_pieces(bed, [Fraction(5)], [False], Fraction(0)) == []


def test_styled_graph_adds_fades_transitions_and_the_music_mix():
    fades = Style()
    graph = reel_graph(7, (160, 90), Fraction(10), AUDIO, fades, DURATIONS, CARDS)
    assert graph.count("afade=t=in:st=0:d=0.150000,afade=t=out:st=4.850000:d=0.150000") == 2
    assert graph.count("afade") == 4  # clips only; cards are silent
    assert graph.endswith("concat=n=7:v=1:a=1[video][audio]")
    assert "fade=t=in" not in graph.replace("afade", "")

    dip = Style("dip", Fraction("0.4"))
    graph = reel_graph(7, (160, 90), Fraction(10), AUDIO, dip, DURATIONS, CARDS, Fraction("0.4"))
    assert "format=yuv420p,fade=t=out:st=3.800000:d=0.200000[v0]" in graph
    assert "format=yuv420p,fade=t=in:st=0:d=0.200000,fade=t=out:st=4.800000:d=0.200000[v1]" in graph
    assert "format=yuv420p,fade=t=in:st=0:d=0.200000[v6]" in graph
    assert "xfade" not in graph and "concat=n=7" in graph

    dissolve = Style("dissolve", Fraction("0.4"))
    graph = reel_graph(
        7, (160, 90), Fraction(10), AUDIO, dissolve, DURATIONS, CARDS, Fraction("0.4")
    )
    assert "[v0][v1]xfade=transition=fade:duration=0.400000:offset=3.600000[x1]" in graph
    assert "[x5][v6]xfade=transition=fade:duration=0.400000:offset=25.600000[video]" in graph
    assert "[a0][a1]acrossfade=d=0.400000:c1=tri:c2=tri[y1]" in graph
    assert "[y5][a6]acrossfade=d=0.400000:c1=tri:c2=tri[audio]" in graph
    assert "concat" not in graph

    music = Style(music=Music(Path("bed.wav")))
    pieces = music_pieces(music.music, DURATIONS, CARDS, Fraction(0))
    graph = reel_graph(
        7, (160, 90), Fraction(10), AUDIO, music, DURATIONS, CARDS, Fraction(0), pieces, 0.9
    )
    assert "concat=n=7:v=1:a=1[video][speech]" in graph
    assert "[7:a:0]aformat=sample_fmts=fltp:channel_layouts=stereo,asplit=2[b0][b1]" in graph
    assert (
        "[b0]atrim=start_sample=0:end_sample=576000,asetpts=PTS-STARTPTS,volume=0.125893,"
        "afade=t=in:st=0:d=1.000000,afade=t=out:st=11.000000:d=1.000000[m0]"
    ) in graph
    assert (
        "[b1]atrim=start_sample=576000:end_sample=1008000,asetpts=PTS-STARTPTS,volume=0.125893,"
        "afade=t=in:st=0:d=1.000000,afade=t=out:st=8.000000:d=1.000000,"
        "adelay=1056000S|1056000S[m1]"
    ) in graph
    assert graph.endswith(
        "[speech][m0][m1]amix=inputs=3:duration=first:dropout_transition=0:normalize=0,"
        "volume=0.900000[audio]"
    )
    mono = {"sample_rate": 44100, "channels": 1}
    graph = reel_graph(
        7, (160, 90), Fraction(10), mono, music, DURATIONS, CARDS, Fraction(0), pieces, 1.0
    )
    assert "adelay=970200S[m1]" in graph and ",volume=1" not in graph


class FakeTools:
    def __init__(self, peaks):
        self.peaks = peaks

    def audio_stats(self, path):
        return self.peaks[path], 48000


def test_headroom_gain_only_lowers_a_mix_that_could_clip(capsys):
    quiet = FakeTools({"a": -6.0, "b": None})
    mix = headroom_gain(quiet, Style(music=Music(Path("bed"))), ["a", "b"])
    assert mix == Mix(1.0, pytest.approx(0.5012, abs=0.0005))
    assert mix.ceiling_db == PEAK_SLACK_DB
    assert headroom_gain(quiet, Style(music=Music(Path("bed"), under="all")), ["a", "b"]).gain == 1
    # Speech that already peaks above full scale is the recording's doing: under `cards` the
    # bed never plays at the same time, so nothing is lowered and the ceiling follows the speech.
    hot = FakeTools({"a": 0.4})
    mix = headroom_gain(hot, Style(music=Music(Path("bed"))), ["a"])
    assert mix.gain == 1.0 and mix.ceiling_db == pytest.approx(0.4 + PEAK_SLACK_DB)
    assert capsys.readouterr().err == ""
    # Under `all` the bed at -30 dB sits on the speech: 1.047 + 0.032 exceeds the -1 dB target.
    mix = headroom_gain(hot, Style(music=Music(Path("bed"), under="all")), ["a"])
    expected = 10 ** (-HEADROOM_DB / 20) / (10 ** (0.4 / 20) + 10 ** (-30 / 20))
    assert mix.gain == pytest.approx(expected)
    assert mix.ceiling_db == PEAK_SLACK_DB
    assert "lowered by" in capsys.readouterr().err
    # A dissolve overlaps the speech tail with a card's music at full `gain`.
    mix = headroom_gain(FakeTools({"a": -0.5}), Style("dissolve", music=Music(Path("bed"))), ["a"])
    expected = 10 ** (-HEADROOM_DB / 20) / (10 ** (-0.5 / 20) + 10 ** (-18 / 20))
    assert mix.gain == pytest.approx(expected)


def test_parse_audio_stats_reads_the_overall_peak_and_sample_count():
    text = (
        "[Parsed_volumedetect_1 @ 0x1] n_samples: 0\n"
        "[Parsed_astats_0 @ 0x2] Channel: 1\n"
        "[Parsed_astats_0 @ 0x2] Peak level dB: -6.020600\n"
        "[Parsed_astats_0 @ 0x2] Number of samples: 48000\n"
        "[Parsed_astats_0 @ 0x2] Channel: 2\n"
        "[Parsed_astats_0 @ 0x2] Peak level dB: -3.5\n"
        "[Parsed_astats_0 @ 0x2] Number of samples: 48000\n"
        "[Parsed_astats_0 @ 0x2] Overall\n"
        "[Parsed_astats_0 @ 0x2] Peak level dB: -3.500000\n"
        "[Parsed_astats_0 @ 0x2] Number of samples: 48000\n"
    )
    assert parse_audio_stats(text) == (pytest.approx(-3.5), 48000)
    silence = "Peak level dB: -inf\nNumber of samples: 960\n"
    assert parse_audio_stats(silence) == (None, 960)
    with pytest.raises(RenderError, match="no audio statistics"):
        parse_audio_stats("Output file is empty\n")
