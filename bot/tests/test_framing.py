"""--framing FILE (clipbot/framing.py): every card around the moments from one JSON file,
and a runtime line that is computed after every card's seconds are final."""

import json
from pathlib import Path

import pytest

from clipbot import cli
from clipbot.framing import Framing, load_framing, parse_framing
from clipbot.plan import validate
from clipbot.probe import SourceInfo
from clipbot.reel import INTRO_SECONDS, Moment, build_reel_plan, plan_runtime
from clipbot.summarize import _ts
from tests.test_reel import DEMO_MP4, REPO, needs_demo

# The shape of talk2-framing.json (Pipeline AI Talk #2), with the timing and music keys the file may add.
TALK2 = {
    "title": "Pipeline AI Talk #2: Build a clip bot, live",
    "date": "2026-09-25",
    "what_you_will_learn": [
        "Why two AI agents coordinating through one repo beats one doing everything, and what 'beads' is",
        "How to scope with AI: define 'good' first, make the AI the interface, ask what must never happen",
        "The mechanics: bots polling a shared task DB, work split by ownership, cross-review before merge",
        "What broke (two writers, one DB) and the honest verdict: the brief, not the bots, was the weak link",
    ],
    "takeaways": [
        "Define 'good output' before the bots start. Open questions ('5 minutes? 1 minute?') are not a brief.",
        "Beads is 'built for AI agents to coordinate, not for humans': a shared task list, not a message bus.",
        "Split by ownership, then the other agent reviews before merge: how two vendors' bots shipped one repo.",
        "Two writers on one shared DB will collide. Recovery: freeze one agent, back up, resync, never force-overwrite.",
        "Kyle: 'This blew me out of the water.' Ramsey: 'More time fixing their issues than one chat by ourselves.'",
        "Rules from the room: never cut mid-word, kill the ums and the dead air, ship a summary + transcript PDF.",
    ],
    "outro": {
        "title": "Next: Pipeline AI Talk #3",
        "lines": [
            "Jeff Weiner's personal operating system. Friday, Oct 2, 12:00 PT",
            "Talk #2 recorded Sep 25, 2026 with Kyle Tabor and Ramsey Jamoul",
            "Rendered by the clip bot built in this session",
        ],
    },
    "opening_seconds": 7,
    "closing_seconds": 7,
    "outro_seconds": 6,
    "music_gain_db": -14,
}


def write(tmp_path: Path, data) -> Path:
    p = tmp_path / "framing.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_load_framing_round_trips_the_talk2_file(tmp_path):
    fr = load_framing(write(tmp_path, TALK2))
    assert fr.title == TALK2["title"] and fr.date == "2026-09-25"
    assert fr.what_you_will_learn == tuple(TALK2["what_you_will_learn"])
    assert fr.takeaways == tuple(TALK2["takeaways"])
    assert (fr.outro_title, fr.outro_lines) == (TALK2["outro"]["title"], tuple(TALK2["outro"]["lines"]))
    assert (fr.opening_seconds, fr.closing_seconds, fr.outro_seconds) == (7.0, 7.0, 6.0)
    assert fr.music_gain_db == -14.0 and fr.music_fade_seconds is None
    assert fr.transition_kind is None and fr.transition_seconds is None
    assert fr.outro_card() == {"title": "Next: Pipeline AI Talk #3", "lines": TALK2["outro"]["lines"], "seconds": 6.0}
    # every key optional: an empty object is a framing that says nothing
    assert parse_framing({}) == Framing() and Framing().outro_card() is None
    # transitions: a name, or {kind, seconds}; nulls are "not set"
    assert parse_framing({"transition": "dissolve", "title": None}).transition_kind == "dissolve"
    fr = parse_framing({"transition": {"kind": "cut", "seconds": 0.2}})
    assert (fr.transition_kind, fr.transition_seconds) == ("cut", 0.2)
    # an outro without seconds reads for as long as its lines need (lessons.card_seconds)
    assert parse_framing({"outro": {"title": "Bye", "lines": ["a", "b"]}}).outro_card()["seconds"] == 4.5


@pytest.mark.parametrize("data, message", [
    ({"takeaway": ["x"]}, "unknown key"),  # a typo must not be silently ignored
    ({"what_you_will_learn": ["a", "b", "c", "d", "e"]}, "at most 4"),
    ({"takeaways": ["t"] * 13}, "at most 12"),
    ({"takeaways": ["x" * 121]}, "the card holds 120"),
    ({"title": "T" * 81}, "the card holds 80"),
    ({"title": "   "}, "non-empty"),
    ({"opening_seconds": 12}, "outside 1..10"),
    ({"closing_seconds": "7"}, "expected a number"),
    ({"outro_seconds": True}, "expected a number"),
    ({"music_gain_db": 3}, "outside -40..0"),
    ({"music_fade_seconds": 9}, "outside 0..5"),
    ({"transition": "wipe"}, "not one of"),
    ({"transition": {"kind": "dip", "seconds": 3}}, "outside 0.1..1.5"),
    ({"outro": {"lines": ["no title"]}}, "expected {title, lines}"),
    ({"outro": {"title": "t", "seconds": 3}}, "unknown key"),
    ([], "expected a JSON object"),
])
def test_parse_framing_is_loud_about_mistakes(data, message):
    with pytest.raises(ValueError, match=message):
        parse_framing(data)


def test_load_framing_reports_the_path_and_bad_json(tmp_path):
    p = tmp_path / "framing.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_framing(p)
    with pytest.raises(ValueError, match=str(p).replace("\\", "\\\\")):
        load_framing(write(tmp_path, {"nope": 1}))


def test_build_reel_plan_applies_the_framing_and_computes_the_runtime_last():
    """The third reel's intro said 4:50 while the reel ran 4:55: the cards were edited after the
    plan was built. With a framing every card's seconds are final before the runtime line."""
    info = SourceInfo("assets/demo-clip.mp4", 72.0, True, True, 0)
    ms = [Moment(10.0, 30.0, "a.", "a", lesson="Lesson a"), Moment(40.0, 61.0, "b.", "b", lesson="Lesson b")]
    fr = parse_framing(dict(TALK2, music_fade_seconds=1.5, transition={"kind": "dissolve", "seconds": 0.6}))
    plan = build_reel_plan(info, ms, "out/x", title="flag title", date="flag date", takeaways=["flag takeaway"],
                           transition="dip", music="assets/music/bed.mp3", framing=fr)
    validate(plan)
    reel = plan["output"]["reel"]
    assert reel["intro"]["title"] == TALK2["title"] and reel["intro"]["lines"][0] == "Recorded 2026-09-25"
    assert reel["opening"] == [{"title": "What you'll learn", "lines": TALK2["what_you_will_learn"], "seconds": 7.0}]
    assert [c["lines"] for c in reel["closing"]] == [TALK2["takeaways"][:4], TALK2["takeaways"][4:]]
    assert [(c["title"], c["seconds"]) for c in reel["closing"]] == [("Takeaways", 7.0), ("Takeaways (continued)", 7.0)]
    assert reel["outro"] == {"title": "Next: Pipeline AI Talk #3", "lines": TALK2["outro"]["lines"], "seconds": 6.0}
    assert reel["music"] == {"path": "assets/music/bed.mp3", "gain_db": -14.0, "fade_seconds": 1.5}
    assert reel["transition"] == {"kind": "dissolve", "seconds": 0.6}
    # the runtime string on the intro is the sum of everything the renderer will show
    segments = sum(s["end"] - s["start"] for c in plan["clips"] for s in c["segments"])
    cards = sum(c["card"]["seconds"] for c in plan["clips"])
    total = INTRO_SECONDS + 7.0 + cards + segments + 7.0 + 7.0 + 6.0
    assert total == pytest.approx(4 + 7 + 6 + (20.45 + 21.45) + 14 + 6)
    assert plan_runtime(plan) == pytest.approx(total)
    assert reel["intro"]["lines"][-1] == f"2 moments · {_ts(round(total))}" == "2 moments · 1:19"
    # a framing that says nothing leaves the flags in charge (existing behaviour, byte for byte)
    plain = build_reel_plan(info, ms, "out/x", title="flag title", date="flag date", takeaways=["flag takeaway"])
    assert build_reel_plan(info, ms, "out/x", title="flag title", date="flag date", takeaways=["flag takeaway"],
                           framing=Framing()) == plain
    assert plain["output"]["reel"]["intro"]["title"] == "flag title" and "music" not in plain["output"]["reel"]
    # partial framings override only what they say
    part = build_reel_plan(info, ms, "out/x", title="flag title", date="d", takeaways=["one", "two"],
                           framing=parse_framing({"closing_seconds": 9.5, "outro": {"title": "Bye"}}))
    r = part["output"]["reel"]
    assert r["intro"]["title"] == "flag title" and r["closing"] == [{"title": "Takeaways", "lines": ["one", "two"], "seconds": 9.5}]
    assert r["outro"] == {"title": "Bye", "lines": [], "seconds": 3.0} and r["opening"][0]["lines"] == ["Lesson a", "Lesson b"]
    assert r["intro"]["lines"][-1] == f"2 moments · {_ts(round(plan_runtime(part)))}"


@needs_demo
def test_cli_reel_with_framing_file(tmp_path, capsys):
    framing = write(tmp_path, dict(TALK2, transition="cut"))
    out = tmp_path / "plan.json"
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--out", str(out), "--framing", str(framing),
                   "--title", "flag title", "--transition", "dip", "--music", str(REPO / "assets" / "demo-clip.mp4")])
    captured = capsys.readouterr()
    assert rc == 0, captured
    plan = json.loads(out.read_text(encoding="utf-8"))
    validate(plan)
    reel = plan["output"]["reel"]
    assert reel["intro"]["title"] == TALK2["title"] and reel["intro"]["lines"][0] == "Recorded 2026-09-25"
    assert reel["opening"][0]["lines"] == TALK2["what_you_will_learn"] and reel["opening"][0]["seconds"] == 7
    assert [c["seconds"] for c in reel["closing"]] == [7, 7] and reel["outro"]["seconds"] == 6
    assert reel["music"]["gain_db"] == -14 and reel["transition"]["kind"] == "cut"
    # the intro card and the `reel:` line agree on the runtime, and both equal the plan
    shown = _ts(round(plan_runtime(plan)))
    assert reel["intro"]["lines"][-1].endswith(shown)
    assert f"runtime {shown} incl. all cards" in captured.out
    # summary.md carries the framing's title and takeaways
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert TALK2["title"] in summary and TALK2["takeaways"][5] in summary
    # music settings without --music are reported, not silently dropped; a bad file is a clean error
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--out", str(out), "--framing", str(framing)])
    assert rc == 0 and "music settings" in capsys.readouterr().err
    bad = write(tmp_path, {"opening_seconds": 99})
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--out", str(out), "--framing", str(bad)])
    assert rc == 1 and "outside 1..10" in capsys.readouterr().err
