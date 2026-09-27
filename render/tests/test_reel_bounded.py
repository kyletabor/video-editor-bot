"""The bounded reel assembly: no FFmpeg process of the reel step opens more than three media
inputs, whatever the plan, and the joined reel is what the single graph produced, frame for
frame and sample for sample (`legacy_reel` is that graph, kept verbatim)."""

import json
from fractions import Fraction
from pathlib import Path

import legacy_reel
import pytest
from test_integration import HEIGHT, RATE, ROOT, WIDTH, audio, cli, probe, rms
from test_reel import luma_planes, rows
from test_reel_music import music_plan, needs_tools

from cliprender.media import Tools
from cliprender.reel import reel_style, timeline
from cliprender.renderer import render_plan


def recording(monkeypatch):
    """Record every tool command the renderer runs, in process, with its working directory."""
    seen = []
    original = Tools.run

    def record(tools, args, *, cwd=None, stderr=False):
        seen.append({"args": [str(a) for a in args], "cwd": cwd})
        return original(tools, args, cwd=cwd, stderr=stderr)

    monkeypatch.setattr(Tools, "run", record)
    return seen


def reel_step(seen):
    """The FFmpeg commands of the reel step: those run in, or on files of, the `_reel` workspace."""
    return [
        run
        for run in seen
        if Path(run["args"][0]).stem == "ffmpeg"
        and any("_reel" in Path(word).parts for word in [*run["args"], str(run["cwd"] or "")])
    ]


@needs_tools
@pytest.mark.parametrize("transition", [None, "dip", "dissolve"])
def test_reel_step_never_opens_more_than_three_media_inputs(
    tmp_path, talk, monkeypatch, transition
):
    seen = recording(monkeypatch)
    plan, output = music_plan(tmp_path, talk, transition=transition, under="all")
    records = render_plan(plan, root=ROOT)
    assert [record[0] for record in records] == ["first", "second", "reel"]
    commands = reel_step(seen)
    assert len(commands) >= 7 + 1  # seven pieces and the join, at the least
    inputs = [run["args"].count("-i") for run in commands]
    assert max(inputs) <= 3, inputs
    # `under: all` is the widest mix: a segment plus the base stretch plus one card run.
    assert 3 in inputs
    joins = [run for run in commands if "concat" in run["args"] and "copy" in run["args"]]
    assert len(joins) == 1 and joins[0]["args"].count("-i") == 2
    if transition == "dissolve":
        assert any(run["args"][-1].startswith("bridge-") for run in commands)
    assert not any("-threads" in run["args"] for run in commands)
    assert (output / "reel.mp4").exists()


def legacy(tmp_path, plan_path, output):
    """Assemble the same reel with the pre-rebuild single graph from the published clips."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    reel = plan["output"]["reel"]
    tools = Tools(timeout=300)
    rendered = {}
    for clip in plan["clips"]:
        path = output / f"{clip['id']}.mp4"
        streams = probe(path)["streams"]
        seconds = max(float(s.get("start_time", 0)) + float(s["duration"]) for s in streams)
        rendered[clip["id"]] = (path, seconds)
    job = tmp_path / "legacy"
    job.mkdir()
    old, _ = legacy_reel.render_reel(
        tools,
        job,
        timeline(plan, reel),
        rendered,
        (WIDTH, HEIGHT),
        Fraction(10),
        {"sample_rate": RATE, "channels": 2},
        tools.graph_flag(),
        tools.cfr_flags(),
        "reel.mp4",
        "Reel",
        style=reel_style(reel, Path),
    )
    return old


# The fixture reel (see test_reel_music): intro 4, opening 5, chapter card 3, clip A 5, clip B 5,
# closing 6 and outro 3 seconds at 10 fps; a dissolve shortens every join by 0.4 s.
DURATIONS = [4, 5, 3, 5, 5, 6, 3]


def interior(samples, start, end, margin=0.5):
    """RMS of a stretch of the reel, `margin` seconds in from both of its ends."""
    return rms(samples, start + margin, end - margin)


@needs_tools
@pytest.mark.parametrize("transition", [None, "dip", "dissolve"])
def test_pieces_reproduce_the_single_graph_reel(tmp_path, talk, timing_flags, transition):
    plan, output = music_plan(tmp_path, talk, transition=transition)
    rows(cli(plan))
    new, old = output / "reel.mp4", legacy(tmp_path, plan, output)
    overlap = 0.4 if transition == "dissolve" else 0
    starts = [sum(DURATIONS[:k]) - k * overlap for k in range(len(DURATIONS))]
    seconds = sum(DURATIONS) - overlap * (len(DURATIONS) - 1)
    # Video: exactly the frames the timeline adds up to. The single graph could add one: a
    # card's padded AAC silence outlasted its frames and `-vsync cfr` filled the gap.
    new_luma, old_luma = luma_planes(new, timing_flags), luma_planes(old, timing_flags)
    assert len(new_luma) == round(seconds * 10)
    assert abs(len(old_luma) - len(new_luma)) <= 1
    # Frame for frame the same picture, except where a dip's fade ramp meets the single
    # graph's sub-frame drift at a join.
    joins = {round(start * 10) for start in starts[1:]}
    for index, (a, b) in enumerate(zip(new_luma, old_luma)):
        near_join = any(abs(index - join) <= 3 for join in joins)
        assert abs(a - b) <= (20 if transition == "dip" and near_join else 1.5), index
    # Audio: exactly the samples of the timeline (the decode pads to a whole AAC frame). The
    # single graph ran up to about 20 ms long per card, for the same reason as the frame.
    new_audio, old_audio = audio(new), audio(old)
    assert 0 <= len(new_audio) - round(seconds * RATE) < 1024
    assert 0 <= len(old_audio) - len(new_audio) <= 5 * RATE // 40
    # Inside every piece, half a second in from its ends, the same levels: the bed under the
    # card runs only (its loud and quiet halves where the running offset puts them), speech in
    # the clips, and the silent second inside each clip still silent.
    for start, length in zip(starts, DURATIONS):
        expected = interior(old_audio, start, start + length)
        assert interior(new_audio, start, start + length) == pytest.approx(
            expected, rel=0.05, abs=0.003
        )
    for clip in (3, 4):
        assert rms(new_audio, starts[clip] + 2.1, starts[clip] + 2.9) < 0.003
        assert rms(old_audio, starts[clip] + 2.1, starts[clip] + 2.9) < 0.003
