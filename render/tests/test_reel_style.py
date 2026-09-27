"""Reel rules that need no media tools: style defaults, timeline, transition and music
arithmetic, the frame grid, the piece and bridge graphs, the join list, and the headroom rule."""

import struct
import wave
from fractions import Fraction
from pathlib import Path

import pytest

from cliprender.media import RenderError, parse_audio_stats
from cliprender.reel import (
    COLOR_TAGS,
    HEADROOM_DB,
    PEAK_SLACK_DB,
    Mix,
    Music,
    Piece,
    Segment,
    Style,
    bed_piece_graph,
    bridge_audio_graph,
    bridge_video_graph,
    card_runs,
    concat_list,
    effective_transition,
    frame_count,
    headroom_gain,
    join_wavs,
    mix_graph,
    music_pieces,
    piece_encoder_flags,
    piece_graph,
    reel_seconds,
    reel_style,
    sample_count,
    segment_starts,
    timeline,
    transition_frames,
)

AUDIO = {"sample_rate": 48000, "channels": 2}
# The reel of the music tests: intro, opening, chapter card, two clips, closing, outro.
DURATIONS = [Fraction(n) for n in (4, 5, 3, 5, 5, 6, 3)]
CARDS = [True, True, True, False, False, True, True]


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


def test_v11_plan_has_no_style_and_conforms_its_pieces_with_no_v12_filter():
    assert reel_style({"intro": {"title": "Intro"}, "chapter_cards": "all"}, Path) is None
    # The v1.1 conform chains, verbatim, then the exact length and the shared colour tags:
    # nothing a v1.2 field would add (no fade, no cross-fade, no mix, no split).
    video = (
        "[0:v:0]scale=160:90:force_original_aspect_ratio=decrease,"
        "pad=160:90:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=10/1:start_time=0,format=yuv420p,"
        f"tpad=stop_mode=clone:stop=-1,trim=end_frame=50,{COLOR_TAGS}[video]"
    )
    sound = (
        "[0:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        "asetpts=PTS-STARTPTS,apad=whole_len=240000,atrim=end_sample=240000[audio]"
    )
    plain = piece_graph(0, 0, (160, 90), Fraction(10), AUDIO, 50, 240000)
    assert plain == video + ";\n" + sound
    for banned in ("fade", "xfade", "amix", "split"):
        assert banned not in plain
    silent = piece_graph(0, 0, (160, 90), Fraction(24), None, 24, 0)
    assert silent.endswith(f"trim=end_frame=24,{COLOR_TAGS}[video]") and "[audio]" not in silent
    # A card is RGB: it is converted with the matrix the tags name, and its silence is input 1.
    card = piece_graph(0, 1, (160, 90), Fraction(10), AUDIO, 30, 144000, card=True)
    assert card.startswith(
        "[0:v:0]scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,scale=160:90:"
    )
    assert "[1:a:0]aresample=48000" in card and "afade" not in card
    flags = piece_encoder_flags(Fraction(10), ["-fps_mode", "cfr"])
    assert flags[:8] == ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-bf", "0"]
    assert flags[-4:] == ["-r", "10/1", "-fps_mode", "cfr"] and "-colorspace" in flags


def test_frame_grid_pins_every_piece_and_the_dissolve_to_whole_frames():
    assert frame_count(Fraction("4.98"), Fraction(24)) == 120
    assert frame_count(Fraction("0.01"), Fraction(24)) == 1
    assert sample_count(120, Fraction(24), 48000) == 240000
    assert sample_count(3, Fraction(30000, 1001), 48000) == 4805
    frames = [frame_count(seconds, Fraction(10)) for seconds in DURATIONS]
    assert frames == [40, 50, 30, 50, 50, 60, 30]
    assert transition_frames(Fraction("0.4"), Fraction(10), frames) == 4
    # Every piece keeps at least one frame of its own: a middle piece of 8 frames allows 3.
    assert transition_frames(Fraction("0.4"), Fraction(10), [8, 8, 8]) == 3
    assert transition_frames(Fraction("0.4"), Fraction(10), [50]) == 0
    assert transition_frames(Fraction(0), Fraction(10), frames) == 0


def test_styled_pieces_carry_dips_fades_and_dissolve_ends():
    dip = piece_graph(
        0, 1, (160, 90), Fraction(10), AUDIO, 30, 144000, card=True, dip=(Fraction("0.2"),) * 2
    )
    assert "trim=end_frame=30,fade=t=in:st=0:d=0.200000,fade=t=out:st=2.800000:d=0.200000," in dip
    assert "afade" not in dip  # cards are silent
    clip = piece_graph(
        0,
        0,
        (160, 90),
        Fraction(10),
        AUDIO,
        50,
        240000,
        audio_fade=Fraction("0.15"),
        head=(4, 19200),
        tail=(4, 19200),
    )
    lines = clip.split(";\n")
    assert lines[0].endswith(f"{COLOR_TAGS},split=3[vb][vh][vt]") and "fade=" not in lines[0]
    assert lines[1] == "[vb]trim=start_frame=4:end_frame=46,setpts=PTS-STARTPTS[video]"
    assert lines[2] == "[vh]trim=end_frame=4[head]"
    assert lines[3] == "[vt]trim=start_frame=46,setpts=PTS-STARTPTS[tail]"
    assert lines[4].endswith(
        "atrim=end_sample=240000,afade=t=in:st=0:d=0.150000,afade=t=out:st=4.850000:d=0.150000,"
        "asplit=3[ab][ah][at]"
    )
    assert lines[5] == "[ab]atrim=start_sample=19200:end_sample=220800,asetpts=PTS-STARTPTS[audio]"
    assert lines[6] == "[ah]atrim=end_sample=19200[ahead]"
    assert lines[7] == "[at]atrim=start_sample=220800,asetpts=PTS-STARTPTS[atail]"
    first = piece_graph(0, 0, (160, 90), Fraction(10), AUDIO, 50, 240000, tail=(4, 19200))
    assert "split=2[vb][vt]" in first and "[head]" not in first
    assert "[vb]trim=start_frame=0:end_frame=46" in first
    assert bridge_video_graph(4, Fraction(10)) == (
        "[0:v:0][1:v:0]xfade=transition=fade:duration=0.400000:offset=0,"
        f"tpad=stop_mode=clone:stop=-1,trim=end_frame=4,{COLOR_TAGS}[video]"
    )
    assert bridge_audio_graph(19200, 48000) == (
        "[0:a:0]afade=t=out:st=0:d=0.400000:curve=tri[out];\n"
        "[1:a:0]afade=t=in:st=0:d=0.400000:curve=tri[in];\n"
        "[out][in]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
        "apad=whole_len=19200,atrim=end_sample=19200[audio]"
    )


def test_music_is_cut_per_run_and_mixed_into_the_segments_it_overlaps():
    music = Music(Path("bed.wav"))
    pieces = music_pieces(music, DURATIONS, CARDS, Fraction(0))
    graph, samples = bed_piece_graph(pieces[0], AUDIO, Fraction(1))
    assert samples == 576000
    assert graph == (
        "[0:a:0]aformat=sample_fmts=fltp:channel_layouts=stereo,"
        "atrim=start_sample=0:end_sample=576000,asetpts=PTS-STARTPTS,volume=0.125893,"
        "afade=t=in:st=0:d=1.000000,afade=t=out:st=11.000000:d=1.000000[audio]"
    )
    graph, samples = bed_piece_graph(pieces[1], AUDIO, Fraction(1))
    assert samples == 432000
    assert "atrim=start_sample=576000:end_sample=1008000" in graph
    assert "afade=t=out:st=8.000000:d=1.000000[audio]" in graph
    mono = {"sample_rate": 44100, "channels": 1}
    assert bed_piece_graph(pieces[1], mono, Fraction(1))[1] == 396900
    assert mix_graph([(1, 0, 48000, 0), (2, 1000, 3000, 46000)], 2) == (
        "[1:a:0]atrim=start_sample=0:end_sample=48000,asetpts=PTS-STARTPTS[m1];\n"
        "[2:a:0]atrim=start_sample=1000:end_sample=3000,asetpts=PTS-STARTPTS,"
        "adelay=46000S|46000S[m2];\n"
        "[0:a:0][m1][m2]amix=inputs=3:duration=first:dropout_transition=0:normalize=0[audio]"
    )
    assert "adelay=46000S[m1]" in mix_graph([(1, 0, 10, 46000)], 1)


def test_join_lists_exact_lengths_and_concatenates_wavs_sample_by_sample(tmp_path):
    segments = [
        Segment("piece-00", 40, 192000),
        Segment("bridge-00", 4, 19200),
        Segment("piece-01", 42, 201600),
    ]
    assert concat_list(segments, Fraction(10)) == (
        "ffconcat version 1.0\n"
        "file piece-00.mp4\nduration 4.000000\n"
        "file bridge-00.mp4\nduration 0.400000\n"
        "file piece-01.mp4\nduration 4.200000\n"
    )
    assert (segments[1].video, segments[1].audio) == ("bridge-00.mp4", "bridge-00.wav")

    def write(path, values, channels=1):
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(struct.pack(f"<{len(values)}h", *values))

    write(tmp_path / "a.wav", [1, 2, 3])
    write(tmp_path / "b.wav", [4, 5])
    assert join_wavs([tmp_path / "a.wav", tmp_path / "b.wav"], tmp_path / "ab.wav") == 5
    with wave.open(str(tmp_path / "ab.wav")) as handle:
        assert (handle.getnchannels(), handle.getframerate(), handle.getnframes()) == (1, 8000, 5)
        assert handle.readframes(5) == struct.pack("<5h", 1, 2, 3, 4, 5)
    write(tmp_path / "stereo.wav", [1, 2, 3, 4], channels=2)
    with pytest.raises(RenderError, match="differ in format"):
        join_wavs([tmp_path / "a.wav", tmp_path / "stereo.wav"], tmp_path / "bad.wav")
    (tmp_path / "text.wav").write_bytes(b"not a wav")
    with pytest.raises(RenderError, match="not a WAV"):
        join_wavs([tmp_path / "text.wav"], tmp_path / "bad.wav")


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
