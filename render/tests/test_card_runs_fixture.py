"""contract/fixtures/card-runs.json: where the card runs of a reel are.

bot/clipbot/score.py cuts a style's music to the card runs and so carries a copy of this
arithmetic (`timeline`, `frame_count`, `effective_transition`, `transition_frames`,
`card_runs`). The fixture was written from the functions below and bot/tests asserts the same
numbers, so a change here that moves a run fails this test until the fixture, and with it the
bot, is brought along.
"""

import json
from fractions import Fraction
from pathlib import Path

import pytest

from cliprender import reel
from cliprender.cards import Card

FIXTURE = Path(__file__).resolve().parents[2] / "contract" / "fixtures" / "card-runs.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def runs_of(plan, fps):
    """(start, seconds) of every card run, by the steps `render_reel` takes before it encodes."""
    spec = plan["output"]["reel"]
    items = reel.timeline(plan, spec)
    style = reel.reel_style(spec, Path)
    kept = {
        clip["id"]: sum(
            Fraction(str(s["end"])) - Fraction(str(s["start"])) for s in clip["segments"]
        )
        for clip in plan["clips"]
    }
    durations = [item.seconds if isinstance(item, Card) else kept[item] for item in items]
    cards = [isinstance(item, Card) for item in items]
    transition = reel.effective_transition(style, durations)
    overlapping = style is not None and style.transition in reel.OVERLAPPING and transition > 0
    frames = [reel.frame_count(seconds, fps) for seconds in durations]
    bridge = reel.transition_frames(transition, fps, frames) if overlapping else 0
    lengths = [Fraction(count) / fps for count in frames]
    return [(a, b - a) for a, b in reel.card_runs(lengths, cards, Fraction(bridge) / fps)]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_card_runs_are_where_the_fixture_says(case):
    runs = runs_of(case["plan"], Fraction(case["fps"]))
    assert [[str(start), str(seconds)] for start, seconds in runs] == case["runs"]


def test_the_fixture_covers_every_transition_kind_and_card_mode():
    reels = [case["plan"]["output"]["reel"] for case in CASES]
    assert {r.get("transition", {}).get("kind", "cut") for r in reels} >= {"cut", "dip", "dissolve", "module"}
    assert {r.get("chapter_cards", "auto") for r in reels} == {"auto", "all", "none"}
