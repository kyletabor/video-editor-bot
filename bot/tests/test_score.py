"""Scoring a reel (clipbot/score.py): one music cue per card run, cut from a style's blocks.

The arithmetic is tested on a made-up style with round numbers (a 2 s bar); the shipped
pipeline style is then mixed for real when ffmpeg is on PATH.
"""

import json
import subprocess
import wave
from fractions import Fraction as F
from pathlib import Path

import pytest

from clipbot import cli, score, styles
from clipbot.plan import REPO_ROOT, validate
from clipbot.score import Blocks, Placement, Run, card_runs, cue_graph, plan_cue
from tests.test_reel import DEMO_MP4, needs_demo

FPS = F(24)


def _ffmpeg_present() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_present(), reason="needs ffmpeg")


def blocks(**kw) -> Blocks:
    """A 120 BPM style: 2 s bars, a 1 s pickup, a 5 s held chord, a 6 s outro with one lead bar."""
    base = dict(
        bar=F(2), vamp=((Path("a"), Path("a-home")), (Path("b"), None), (Path("c"), Path("c-home")), (Path("d"), None)),
        stings=(Path("d"), Path("a-home"), Path("b")), hold=Path("hold"), hold_seconds=F(5),
        pickup=Path("pickup"), pickup_seconds=F(1), outro=Path("outro"), outro_seconds=F(6), outro_lead=F(2),
    )
    base.update(kw)
    return Blocks(**base)


def names(cue):
    return [(p.file.name, p.at) for p in cue]


def plan(cards=(4, 7), chapter=3, closing=(7,), outro=6, clips=(20.0, 30.0), transition=None, chapter_cards="all"):
    reel = {"filename": "reel.mp4", "chapter_cards": chapter_cards}
    if cards:
        reel["intro"] = {"title": "T", "seconds": cards[0]}
        reel["opening"] = [{"title": "O", "seconds": s} for s in cards[1:]]
    if closing:
        reel["closing"] = [{"title": "C", "seconds": s} for s in closing]
    if outro:
        reel["outro"] = {"title": "Bye", "seconds": outro}
    if transition:
        reel["transition"] = transition
    out_clips = []
    for i, seconds in enumerate(clips):
        clip = {"id": f"clip-{i}", "takeaway": "x", "segments": [{"start": 100.0 * i, "end": 100.0 * i + seconds}]}
        if chapter:
            clip["card"] = {"title": "Ch", "seconds": chapter}
        out_clips.append(clip)
    return {"version": "1", "source": {"path": "x.mp4"}, "output": {"dir": "out", "reel": reel}, "clips": out_clips}


# --- where the cards are -----------------------------------------------------------------------------

def test_card_runs_join_consecutive_cards_and_name_their_position():
    runs = card_runs(plan(), FPS)
    # intro 4 + opening 7 + first chapter 3 | clip 20 | chapter 3 | clip 30 | closing 7 + outro 6
    assert runs == [Run(F(0), F(14), "first"), Run(F(34), F(3), "middle"), Run(F(67), F(13), "last")]


def test_the_run_that_opens_the_reel_is_first_even_without_an_intro_and_no_cards_means_no_runs():
    runs = card_runs(plan(cards=(), closing=(), outro=None), FPS)
    assert [(r.seconds, r.position) for r in runs] == [(F(3), "first"), (F(3), "middle")]  # the reel ends on a clip: no last
    assert card_runs(plan(cards=(), closing=(), outro=None, chapter_cards="none"), FPS) == []


def test_card_runs_count_whole_frames_like_the_renderer():
    # 3.03 s at 24 fps is 73 frames (72.72 rounds up): the run is 73/24 s, not 3.03 s
    runs = card_runs(plan(cards=(), closing=(), outro=None, chapter=3.03), FPS)
    assert runs[0].seconds == F(73, 24)


def test_a_dissolve_overlaps_the_joins_inside_and_around_a_run():
    cut = card_runs(plan(), FPS)
    dissolve = card_runs(plan(transition={"kind": "dissolve", "seconds": 0.5}), FPS)
    # the first run has two joins inside it (intro|opening|chapter): half a second less each
    assert dissolve[0].seconds == cut[0].seconds - 1
    # and every later run starts earlier by half a second per join before it (four joins before the second card run)
    assert dissolve[1].start == cut[1].start - 2
    assert card_runs(plan(transition={"kind": "dip", "seconds": 0.5}), FPS) == cut  # a dip overlaps nothing


def test_bridge_frames_leave_every_piece_a_frame_of_its_own():
    assert score.bridge_frames({"kind": "dissolve", "seconds": 1.5}, FPS, [24, 24, 24]) == 11
    assert score.bridge_frames({"kind": "cut"}, FPS, [24, 24]) == 0
    assert score.bridge_frames({"kind": "module", "seconds": 1.0}, FPS, [96, 96]) == 24


# --- what plays under each run -----------------------------------------------------------------------

def test_first_run_is_pickup_bars_and_a_held_chord_that_fills_the_run():
    cue = plan_cue(14, "first", blocks())
    # 14 s: pickup 1 s, then floor((14 - 1 - 0.6) / 2) = 6 bars, the last one resolved, then the hold
    assert names(cue) == [("pickup", 0), ("a", 1), ("b", 3), ("c", 5), ("d", 7), ("a", 9), ("b", 11), ("hold", 13)]
    hold = cue[-1]
    assert hold.at + hold.seconds == 14 and hold.fade_out == F(1, 2)


def test_the_last_bar_before_home_is_its_resolved_variant():
    cue = plan_cue(8, "first", blocks())  # pickup + 3 bars: a, b, then c turned home
    assert [p.file.name for p in cue] == ["pickup", "a", "b", "c-home", "hold"]


def test_chapter_card_gets_one_rotating_sting_then_the_hold():
    b = blocks()
    assert [names(plan_cue(3, "middle", b, k))[0][0] for k in range(4)] == ["d", "a-home", "b", "d"]
    cue = plan_cue(3, "middle", b, 0)
    assert names(cue) == [("d", 0), ("hold", 2)]
    assert cue[1].seconds == 1 and cue[1].fade_out == F(1, 2)
    # a longer card gets vamp bars instead of a single sting
    assert [p.file.name for p in plan_cue(7, "middle", b, 0)] == ["a", "b", "c-home", "hold"]


def test_short_runs_get_the_held_chord_alone_or_nothing():
    assert names(plan_cue(2, "middle", blocks())) == [("hold", 0)]
    assert plan_cue(F(2, 5), "middle", blocks()) == []
    # no room for pickup and a bar: the pickup goes, the bar stays
    assert [p.file.name for p in plan_cue(F(7, 2), "first", blocks())] == ["a-home", "hold"]


def test_last_run_is_back_timed_so_the_outro_ends_just_before_the_reel():
    b = blocks()
    cue = plan_cue(20, "last", b)
    outro = cue[-1]
    assert outro.file.name == "outro" and outro.at + b.outro_seconds == 20 - score.END_MARGIN
    # the bars butt up against the outro, the last of them resolved; the pickup leads into the first
    assert names(cue) == [("pickup", F(4, 5)), ("a", F(9, 5)), ("b", F(19, 5)), ("c", F(29, 5)), ("d", F(39, 5)),
                          ("a", F(49, 5)), ("b", F(59, 5)), ("outro", F(69, 5))]
    assert all(p.at >= 0 for p in cue)


def test_last_run_trims_the_pickup_when_there_is_less_than_a_pickup_of_room():
    cue = plan_cue(F(71, 10), "last", blocks())  # outro starts at 0.9 s: 0.9 s of pickup fits
    assert names(cue) == [("pickup", 0), ("outro", F(9, 10))]
    assert cue[0].skip == F(1, 10) and cue[0].fade_in > 0


def test_last_run_too_short_for_the_whole_ending_plays_the_pickup_into_its_final_chord():
    b = blocks()
    cue = plan_cue(5, "last", b)  # 6 s outro does not fit; its last 4 s (after the 2 s lead bar) do
    assert cue == [Placement(Path("pickup"), F(0), skip=F(1, 5), fade_in=F(3, 100)),  # 0.8 s of the 1 s pickup
                   Placement(Path("outro"), F(4, 5), skip=F(2), fade_in=F(1, 200))]  # cut in without a click
    assert names(plan_cue(F(59, 10), "last", b)) == [("pickup", F(7, 10)), ("outro", F(17, 10))]  # room for all of the pickup
    assert [p.file.name for p in plan_cue(3, "last", b)] == ["hold"]
    # a style without an outro ends on a bar turned home and the held chord
    assert [p.file.name for p in plan_cue(3, "last", blocks(outro=None))] == ["a-home", "hold"]


def test_no_block_starts_after_its_run_ends():
    b = blocks()
    for tenths in range(5, 260):
        seconds = F(tenths, 10)
        for position in ("first", "middle", "last"):
            for p in plan_cue(seconds, position, b):
                assert 0 <= p.at < seconds, (seconds, position, p)
                if p.seconds is not None:
                    assert p.at + p.seconds <= seconds, (seconds, position, p)


def test_runs_match_the_fixture_the_renderer_is_pinned_to():
    """contract/fixtures/card-runs.json is written from the renderer's arithmetic and asserted by
    render/tests too: score.card_runs is a copy of that arithmetic and must not drift from it."""
    fixture = json.loads((REPO_ROOT / "contract" / "fixtures" / "card-runs.json").read_text(encoding="utf-8"))
    assert len(fixture["cases"]) >= 7
    for case in fixture["cases"]:
        validate(case["plan"])
        runs = card_runs(case["plan"], F(case["fps"]))
        assert [[str(r.start), str(r.seconds)] for r in runs] == case["runs"], case["name"]


def test_source_fps_refuses_a_source_it_cannot_read(tmp_path):
    with pytest.raises(RuntimeError, match="is not there to take the frame rate from"):
        score.source_fps(tmp_path / "moved.mp4")


# --- the graph ---------------------------------------------------------------------------------------

def test_cue_graph_lays_the_runs_end_to_end_and_cuts_each_to_its_length():
    b = blocks()
    scored = [(Run(F(0), F(3), "middle"), plan_cue(3, "middle", b)), (Run(F(40), F(2), "middle"), plan_cue(2, "middle", b))]
    inputs, graph, total = cue_graph(scored, rate=1000)
    assert [p.name for p in inputs] == ["d", "hold", "hold"]
    assert total == 3000 + 2000 + 1000  # the two runs and the second of padding
    run0, run1 = [c for c in graph.split(";") if c.endswith("[r0]") or c.endswith("[r1]")]
    assert "atrim=end_sample=3000" in run0 and "adelay" not in run0  # the first run sits at 0
    assert "atrim=end_sample=2000" in run1 and "adelay=3000S|3000S" in run1  # the second right after it, not at 40 s
    assert "[p1]" in graph and "adelay=2000S|2000S[p1]" in graph  # the hold two seconds into the first cue
    assert graph.endswith("[out]")
    assert cue_graph([(Run(F(0), F(1, 5), "middle"), [])]) is None


# --- the shipped style, for real ---------------------------------------------------------------------

def test_pipeline_style_lengths_match_its_files():
    b = score.blocks_of(styles.load("pipeline"))
    assert b.bar == F(60 * 4, 104) and b.pickup_seconds == b.bar / 2 and b.outro_lead == b.bar
    assert len(b.vamp) == 4 and len(b.stings) == 4


def _copy_of_pipeline(tmp_path, **outro):
    """The pipeline style in a temp directory with its outro entry changed."""
    import shutil

    shipped = styles.load("pipeline")
    shutil.copytree(shipped.root, tmp_path / "copy")
    data = json.loads((tmp_path / "copy" / "style.json").read_text(encoding="utf-8"))
    data["music"]["outro"].update(outro)
    (tmp_path / "copy" / "style.json").write_text(json.dumps(data), encoding="utf-8")
    return styles.load(str(tmp_path / "copy"))


@needs_ffmpeg
def test_a_style_whose_stated_block_lengths_are_wrong_is_refused(tmp_path):
    score.verify_blocks(styles.load("pipeline"))  # the shipped numbers are the files' own
    with pytest.raises(ValueError, match=r"music\.outro\.seconds says 4 s but music/outro\.flac is 6\.08"):
        score.verify_blocks(_copy_of_pipeline(tmp_path, seconds=4.0))
    with pytest.raises(ValueError, match=r"lead_bars \(5\) is the whole 6\.081 s outro"):
        score.blocks_of(_copy_of_pipeline(tmp_path / "b", lead_bars=5))


def _levels(path: Path, spans):
    """Peak (0..1) of the WAV inside each (start, end) span in seconds."""
    with wave.open(str(path)) as w:
        rate, channels, total = w.getframerate(), w.getnchannels(), w.getnframes()
        peaks = []
        for start, end in spans:
            w.setpos(min(total, int(start * rate)))
            raw = w.readframes(max(0, min(total, int(end * rate)) - int(start * rate)))
            samples = memoryview(raw).cast("h")
            peaks.append(max((abs(s) for s in samples), default=0) / 32768)
        return rate, channels, total, peaks


@needs_ffmpeg
def test_pipeline_cues_mix_to_one_file_with_music_where_the_runs_are(tmp_path):
    style = styles.load("pipeline")
    outro_file = F(str(style.music["outro"]["seconds"]))
    probed = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                             str(style.file(style.music["outro"]["file"]))], capture_output=True, text=True, check=True)
    assert abs(float(probed.stdout) - float(outro_file)) < 0.02  # style.json states the outro's real length
    scored = score.score(plan(), style, FPS)
    assert [run.position for run, _ in scored] == ["first", "middle", "last"]
    out = tmp_path / "music-cues.wav"
    total = score.render_cues(scored, out)
    rate, channels, frames, peaks = _levels(out, [(0.0, 0.2), (1.2, 3.0), (13.99, 14.0), (14.0, 14.3), (16.99, 17.0),
                                                 (27.0, 29.0), (29.85, 30.0), (30.2, 30.9)])
    assert (rate, channels, frames) == (48000, 2, total) and total == (14 + 3 + 13 + 1) * 48000
    start, riff, first_end, sting, sting_end, final_chord, margin, padding = peaks
    assert start > 0.02 and riff > 0.2  # the pickup fill starts the file; the riff follows
    assert first_end < 0.02 and sting_end < 0.02  # every cue is faded out by the end of its run
    assert sting > 0.2  # the second cue starts right where the first run's 14 s end
    assert final_chord > 0.01 and margin < 0.01 and padding == 0  # the ending rings, stops, then silence
    assert max(_levels(out, [(0.0, 31.0)])[3]) < 0.99  # overlapping ring-outs never clip


@needs_demo
@needs_ffmpeg
def test_cli_reel_with_a_cue_style_writes_the_cues_and_points_the_plan_at_them(tmp_path, capsys):
    out = tmp_path / "plan.json"
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--out", str(out), "--style", "pipeline",
                   "--keep-fillers"])
    err = capsys.readouterr().err
    assert rc == 0, err
    reel = json.loads(out.read_text(encoding="utf-8"))["output"]["reel"]
    cues = tmp_path / "music-cues.wav"
    assert reel["music"] == {"path": cues.as_posix(), "under": "cards", "fade_seconds": 0.0, "loop": False,
                             "gain_db": -4.5}
    assert reel["transition"] == {"kind": "dip", "seconds": 0.4}
    assert "style pipeline:" in err and "cues over" in err
    runs = card_runs(json.loads(out.read_text(encoding="utf-8")), score.source_fps(DEMO_MP4))
    with wave.open(str(cues)) as w:
        assert w.getnframes() == round((sum(r.seconds for r in runs) + 1) * 48000)

    # clipbot score re-cuts the music after a hand edit: a longer outro card makes a longer file
    plan_data = json.loads(out.read_text(encoding="utf-8"))
    plan_data["output"]["reel"]["outro"]["seconds"] = 9
    plan_data["output"]["reel"].pop("music")
    out.write_text(json.dumps(plan_data), encoding="utf-8")
    assert cli.main(["score", str(out), "--style", "pipeline", "--gain-db", "-10"]) == 0
    rescored = json.loads(out.read_text(encoding="utf-8"))
    validate(rescored)
    assert rescored["output"]["reel"]["music"]["gain_db"] == -10
    longer = card_runs(rescored, score.source_fps(DEMO_MP4))
    assert sum(r.seconds for r in longer) > sum(r.seconds for r in runs)
    with wave.open(str(cues)) as w:
        assert w.getnframes() == round((sum(r.seconds for r in longer) + 1) * 48000)

    # a plan that is not a plan is reported as that, and a source that moved is not scored at 24 fps
    broken = dict(rescored)
    broken.pop("clips")
    out.write_text(json.dumps(broken), encoding="utf-8")
    assert cli.main(["score", str(out), "--style", "pipeline"]) == 1
    assert "plan violates contract" in capsys.readouterr().err
    rescored["source"]["path"] = str(tmp_path / "moved.mp4")
    out.write_text(json.dumps(rescored), encoding="utf-8")
    assert cli.main(["score", str(out), "--style", "pipeline"]) == 1
    assert "is not there to take the frame rate from" in capsys.readouterr().err
