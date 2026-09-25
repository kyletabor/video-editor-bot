"""Card drawing and reel timeline rules that need no media tools."""

from fractions import Fraction

import pytest
from PIL import Image

from cliprender.cards import (
    BACKGROUND,
    Card,
    chapter_footer,
    draw_card,
    load_font,
    timestamp_label,
    wrap,
    write_card_png,
)
from cliprender.reel import planned_seconds, reel_fps, timeline


def test_footer_timestamp_floors_to_h_mm_ss():
    assert timestamp_label(0) == "0:00:00"
    assert timestamp_label(16.0) == "0:00:16"
    assert timestamp_label(3725.9) == "1:02:05"
    assert chapter_footer(2, 7, 4738.4) == "2 of 7 · 1:18:58"


def test_wrap_limits_rows_breaks_long_words_and_ellipsizes():
    font = load_font(40)
    rows = wrap("one two three four five six seven eight nine ten eleven", font, 300, 2)
    assert len(rows) == 2
    assert rows[-1].endswith("...")
    assert all(font.getlength(row) <= 300 for row in rows)
    assert wrap("short", font, 300, 2) == ["short"]
    rows = wrap("x" * 200, font, 300, 3)
    assert len(rows) == 3
    assert all(font.getlength(row) <= 300 for row in rows)
    assert rows[0] and rows[-1].endswith("...")


def test_card_paints_bright_text_on_the_dark_background(tmp_path):
    card = Card("Title", ("first line", "second"), Fraction(3), chapter_footer(1, 2, 16))
    image = draw_card(card, (320, 180))
    assert image.size == (320, 180)
    assert image.getpixel((0, 0)) == BACKGROUND
    darkest, brightest = image.convert("L").getextrema()
    assert darkest < 40 and brightest > 200
    png = write_card_png(card, (320, 180), tmp_path / "card.png")
    with Image.open(png) as saved:
        assert saved.size == (320, 180)
    with pytest.raises(ValueError):
        draw_card(card, (1, 1))


@pytest.fixture
def two_clip_plan():
    return {
        "clips": [
            {
                "id": "a",
                "takeaway": "A idea",
                "segments": [{"start": 10, "end": 15}],
                "card": {"title": "A card", "seconds": 2},
            },
            {"id": "b", "takeaway": "B idea", "segments": [{"start": 60.5, "end": 66}]},
        ]
    }


def test_timeline_modes_and_footers(two_clip_plan):
    reel = {"intro": {"title": "Intro", "seconds": 4}, "outro": {"title": "Bye"}}
    auto = timeline(two_clip_plan, reel)
    assert [item if isinstance(item, str) else item.title for item in auto] == [
        "Intro",
        "A card",
        "a",
        "b",
        "Bye",
    ]
    assert auto[0].footer == "" and auto[-1].footer == ""
    assert auto[1].footer == "1 of 2 · 0:00:10"
    assert auto[1].seconds == 2 and auto[-1].seconds == 3
    assert planned_seconds(auto, two_clip_plan) == Fraction("19.5")

    everything = timeline(two_clip_plan, {**reel, "chapter_cards": "all"})
    assert [item if isinstance(item, str) else item.title for item in everything] == [
        "Intro",
        "A card",
        "a",
        "B idea",
        "b",
        "Bye",
    ]
    assert everything[3].footer == "2 of 2 · 0:01:00"
    assert timeline(two_clip_plan, {"chapter_cards": "none"}) == ["a", "b"]


def test_reel_frame_rate_keeps_plausible_source_rates_only():
    assert reel_fps({"r_frame_rate": "30000/1001"}) == Fraction(30000, 1001)
    assert reel_fps({"r_frame_rate": "10/1"}) == 10
    for bad in ({"r_frame_rate": "0/0"}, {"r_frame_rate": "1000/1"}, {"r_frame_rate": "x"}, {}):
        assert reel_fps(bad) == 24
