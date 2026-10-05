"""Story fields (contract v1.4): extra slides, image/QR cards, overlays, picture-in-picture."""

import json
import subprocess
import sys

import pytest

from clipbot import story
from clipbot.framing import parse_framing
from clipbot.plan import load_schema, validate
from clipbot.probe import SourceInfo
from clipbot.reel import build_reel_plan, moments_from_specs, plan_runtime
from tests.test_select import DEMO

INFO = SourceInfo("assets/demo-clip.mp4", 72.0, True, True, 0)
PIP = {"kind": "pip", "screen": [0, 240, 1440, 600], "speaker": [1440, 270, 480, 270], "corner": "bottom-right"}
REPO = "https://github.com/kyletabor/video-editor-bot"


@pytest.fixture
def shot(tmp_path):
    png = tmp_path / "shots" / "page.png"
    png.parent.mkdir()
    png.write_bytes(b"\x89PNG\r\n\x1a\n")  # the bot only checks the file is there; the renderer reads it
    return png


def test_moment_story_fields_reach_the_plan_and_validate(tmp_path, shot):
    specs = [
        {"start": 16, "end": 28, "title": "The recording",
         "cards": [{"title": "Part 1: the clip bot", "lines": ["What to remember"]},
                   {"title": "Get the clip bot", "lines": ["Open source"], "qr": REPO}],
         "overlays": [{"start": 17, "end": "0:00:22", "text": "The recording the bot will cut"},
                      {"start": 60, "end": 61, "text": "never shown"}],
         "layout": "full"},
        {"start": 36, "end": 52, "title": "What a page looks like", "image": "shots/page.png"},
    ]
    with pytest.raises(ValueError, match="outside the moment"):  # the 60-61 s label is not in moment 1
        moments_from_specs(specs, DEMO, base=tmp_path)
    specs[0]["overlays"].pop()
    ms = moments_from_specs(specs, DEMO, base=tmp_path)
    fr = parse_framing({"layout": PIP})
    plan = build_reel_plan(INFO, ms, "out/x", title="t", date="d", framing=fr)
    validate(plan, load_schema())
    c1, c2 = plan["clips"]
    assert [c["title"] for c in c1["cards"]] == ["Part 1: the clip bot", "Get the clip bot"]
    assert c1["cards"][1]["qr"] == REPO and c1["cards"][1]["seconds"] == story.DEFAULT_CARD_SECONDS
    assert c1["cards"][0]["seconds"] == 3.0
    assert c1["overlays"] == [{"start": 17.0, "end": 22.0, "text": "The recording the bot will cut", "position": "top"}]
    assert "layout" not in c1  # "full" opts out of framing's pip
    assert c2["layout"] == PIP  # framing's default
    assert c2["card"]["image"] == shot.resolve().as_posix() and c2["card"]["seconds"] == story.DEFAULT_CARD_SECONDS
    # every card counts toward the runtime on the intro card
    kept = sum(s["end"] - s["start"] for c in plan["clips"] for s in c["segments"])
    reel = plan["output"]["reel"]
    cards = [reel["intro"], reel["outro"], *reel.get("opening", []), *reel.get("closing", []),
             *c1["cards"], c1["card"], c2["card"]]
    assert plan_runtime(plan) == pytest.approx(kept + sum(c["seconds"] for c in cards))
    # moments.json round-trip keeps the story fields
    again = moments_from_specs([m.spec() for m in ms], DEMO, base=tmp_path)
    assert [m.spec() for m in again] == [m.spec() for m in ms]


def test_overlays_on_cut_away_seconds_are_dropped_from_the_clip():
    assert story.clip_overlays(({"start": 10, "end": 12, "text": "a", "position": "top"},
                                {"start": 20, "end": 21, "text": "b", "position": "top"}),
                               [(9, 15), (25, 30)]) == [{"start": 10, "end": 12, "text": "a", "position": "top"}]


@pytest.mark.parametrize("card, message", [
    ({"title": "t", "image": "a.png", "qr": REPO}, "not both"),
    ({"title": "t", "qr": "github.com/x"}, "http"),
    ({"title": "t", "image": "missing.png"}, "does not exist"),
    ({"title": "t", "image": "notes.txt"}, "PNG or JPEG"),
    ({"title": "x" * 81}, "at most 80"),
    ({"title": "t", "lines": ["a", "b", "c", "d", "e"]}, "at most 4"),
    ({"title": "t", "seconds": 11}, "seconds"),
    ({"title": "t", "colour": "red"}, "unknown key"),
])
def test_bad_cards_are_errors_not_silent_clipping(tmp_path, card, message):
    (tmp_path / "notes.txt").write_text("x")
    with pytest.raises(ValueError, match=message):
        story.parse_card(card, "card", tmp_path)


def test_too_many_cards_and_bad_overlays():
    with pytest.raises(ValueError, match="1-4"):
        story.parse_cards([{"title": "a"}] * 5, "cards", None)
    with pytest.raises(ValueError, match="after start"):
        story.parse_overlays([{"start": 5, "end": 5, "text": "x"}], "o")
    with pytest.raises(ValueError, match="top or bottom"):
        story.parse_overlays([{"start": 1, "end": 2, "text": "x", "position": "middle"}], "o")


def test_layout_parsing():
    assert story.parse_layout(None, "l", PIP) == PIP
    assert story.parse_layout("full", "l", PIP) is None
    assert story.parse_layout("pip", "l", PIP) == PIP
    assert story.parse_layout({"screen": [0, 0, 10, 10]}, "l") == {"kind": "pip", "screen": [0, 0, 10, 10],
                                                                   "corner": "bottom-right"}
    with pytest.raises(ValueError, match="framing.layout"):
        story.parse_layout("pip", "l", None)
    with pytest.raises(ValueError, match=r"\[x, y, w, h\]"):
        story.parse_layout({"kind": "pip", "screen": [0, 0, 0, 10]}, "l")
    with pytest.raises(ValueError, match="corner"):
        story.parse_layout({"kind": "pip", "screen": [0, 0, 10, 10], "corner": "middle"}, "l")
    with pytest.raises(ValueError, match="screen"):
        parse_framing({"layout": {"kind": "pip"}})


def test_pip_moment_without_framing_layout_is_an_error():
    [m] = moments_from_specs([{"start": 16, "end": 28, "layout": "pip"}], DEMO)
    with pytest.raises(ValueError, match="framing.layout"):
        build_reel_plan(INFO, [m], "out/x", title="t", date="d")


def test_cli_reel_takes_story_fields_from_moments_and_framing(tmp_path, shot):
    moments = tmp_path / "moments.json"
    moments.write_text(json.dumps([
        {"start": 16, "end": 28, "title": "The recording", "cards": [{"title": "Get it", "qr": REPO}]},
        {"start": 36, "end": 52, "title": "A page", "image": "shots/page.png", "layout": "full"},
    ]))
    framing = tmp_path / "framing.json"
    framing.write_text(json.dumps({"layout": {"kind": "pip", "screen": [0, 0, 1440, 810]}}))
    out = tmp_path / "out" / "plan.json"
    proc = subprocess.run([sys.executable, "-m", "clipbot.cli", "reel", "--source", "../assets/demo-clip.mp4",
                           "--moments", str(moments), "--framing", str(framing), "--out", str(out)],
                          capture_output=True, text=True, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    if proc.returncode and "ffprobe" in proc.stderr:
        pytest.skip("needs ffprobe")
    assert proc.returncode == 0, proc.stderr
    plan = json.loads(out.read_text())
    validate(plan, load_schema())
    a, b = plan["clips"]
    assert a["cards"][0]["qr"] == REPO and a["layout"]["screen"] == [0, 0, 1440, 810]
    assert b["card"]["image"].endswith("shots/page.png") and "layout" not in b
