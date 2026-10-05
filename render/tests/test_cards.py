"""Card drawing and reel timeline rules that need no media tools."""

from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image

from cliprender.cards import (
    BACKGROUND,
    LINE_SIZE,
    MAX_ROWS_PER_LINE,
    MAX_TITLE_ROWS,
    SHRINK_STEPS,
    TITLE_SIZE,
    Card,
    chapter_footer,
    draw_card,
    layout,
    load_font,
    timestamp_label,
    wrap,
    write_card_png,
)
from cliprender.reel import planned_seconds, reel_fps, timeline

ROOT = Path(__file__).resolve().parents[2]

# The contract's three aspects at their common short edge. The layout is proportional to the
# frame, so each stands for every resolution of its aspect.
FRAMES = [(1920, 1080), (1080, 1920), (1080, 1080)]

# Chapter cards from the 2026-09-25 recording's final reel whose titles (75, 80 and 77
# characters) the two-row layout cut with an ellipsis on 1920 x 1080, so the lesson read as a
# broken sentence for three seconds. Their lines are the real ones (116 to 120 characters).
TRUNCATED_CARDS = [
    (
        "Two agents, one repo: let them coordinate through the code, not through you",
        (
            (
                "Jeff Weiner asked how the two agents will interact. Kyle contrasts it with the"
                " usual one-agent-does-everything flow."
            ),
            (
                "One agent grinding the whole scope alone is the default. Two coordinating in the"
                " open shows where agent teamwork breaks."
            ),
        ),
    ),
    (
        "Split the work by ownership, then the other agent reviews before anything merges",
        (
            (
                "Jeff asked how the bots divided the work. Ramsey's Codex agent proposed this"
                " protocol at the start; both bots follow it."
            ),
            (
                "Clear ownership plus cross-review is how two agents from two vendors merge into"
                " one repo without overwriting each other."
            ),
        ),
    ),
    (
        "Two writers, one shared DB: when the sync breaks, freeze one agent and resync",
        (
            (
                "Beads sync failed with merge conflicts ('requires operator resolution'). Ramsey"
                " asks Kyle to stop his agent to resync."
            ),
            (
                "Shared state with two writers will collide. Recovery: freeze one writer, back up,"
                " resync. Never force-overwrite."
            ),
        ),
    ),
]

# The contract's title limit (80 characters) spent on long words, which wrap worst.
LONG_WORD_TITLE = "Internationalization infrastructure: interoperability, standardization, observed"
LONG_WORDS = [
    "characteristically",
    "interoperability",
    "standardization",
    "internationalization",
    "observability",
    "maintainability",
]


def long_line(offset):
    """A 120-character line of long words starting at `offset` in the cycle."""
    text = ""
    while len(text) < 120:
        text = f"{text} {LONG_WORDS[offset % len(LONG_WORDS)]}".strip()
        offset += 1
    return text[:120].rstrip()


LONG_LINES = tuple(long_line(offset) for offset in range(4))


def words(rows):
    return " ".join(rows).split()


def test_footer_timestamp_floors_to_h_mm_ss():
    assert timestamp_label(0) == "0:00:00"
    assert timestamp_label(16.0) == "0:00:16"
    assert timestamp_label(3725.9) == "1:02:05"
    assert chapter_footer(2, 7, 4738.4) == "2 of 7 · 1:18:58"


def test_wrap_breaks_on_word_boundaries_only():
    font = load_font(40)
    text = "one two three four five six seven eight nine ten eleven"
    rows = wrap(text, font, 300)
    assert " ".join(rows) == text
    assert len(rows) > 2
    assert all(font.getlength(row) <= 300 for row in rows)
    assert wrap("short", font, 300) == ["short"]
    # A word wider than the column stays whole unless the caller allows the last resort.
    assert wrap("x" * 200, font, 300) == ["x" * 200]
    rows = wrap("x" * 200, font, 300, break_words=True)
    assert "".join(rows) == "x" * 200 and len(rows) > 1
    assert all(font.getlength(row) <= 300 for row in rows)


@pytest.mark.parametrize(("title", "lines"), TRUNCATED_CARDS)
def test_reel_titles_that_were_truncated_fit_in_three_rows_at_full_size(title, lines):
    plan = layout(Card(title, lines, Fraction(3), chapter_footer(1, 10, 117.39)), (1920, 1080))
    assert plan.title_scale == 1.0 and plan.line_scale == 1.0
    assert len(plan.title_rows) == MAX_TITLE_ROWS
    assert words(plan.title_rows) == title.split()
    assert "..." not in " ".join(plan.title_rows) + " ".join(plan.body_rows)
    assert all(plan.title_font.getlength(row) <= plan.column for row in plan.title_rows)
    for line, rows in zip(lines, plan.line_rows):
        assert words(rows) == line.split() and len(rows) <= 2
    assert plan.bottom <= plan.limit < plan.footer_y


@pytest.mark.parametrize("size", FRAMES)
def test_an_80_character_title_of_long_words_fits_without_an_ellipsis(size):
    assert len(LONG_WORD_TITLE) == 80
    plan = layout(Card(LONG_WORD_TITLE), size)
    assert words(plan.title_rows) == LONG_WORD_TITLE.split()
    assert "..." not in " ".join(plan.title_rows)
    assert 1 <= len(plan.title_rows) <= MAX_TITLE_ROWS
    assert all(plan.title_font.getlength(row) <= plan.column for row in plan.title_rows)
    assert plan.title_scale in SHRINK_STEPS
    if size[0] > size[1]:
        assert plan.title_scale == 1.0
    else:  # the narrow frames hold about 10 title ems per row: the font has to shrink
        assert 0.52 <= plan.title_scale < 1.0
    assert plan.bottom <= plan.limit


@pytest.mark.parametrize("size", FRAMES)
def test_four_long_lines_stay_whole_and_clear_of_the_footer(size):
    assert all(110 <= len(line) <= 120 for line in LONG_LINES)
    card = Card(LONG_WORD_TITLE, LONG_LINES, Fraction(3), chapter_footer(9, 10, 3947.23))
    plan = layout(card, size)
    assert len(plan.line_rows) == 4
    for line, rows in zip(LONG_LINES, plan.line_rows):
        assert words(rows) == line.split()
        assert 1 <= len(rows) <= MAX_ROWS_PER_LINE
        assert all(plan.line_font.getlength(row) <= plan.column for row in rows)
    assert "..." not in " ".join(plan.body_rows)
    assert plan.line_scale in SHRINK_STEPS and plan.line_scale >= 0.52
    # The narrow frames may shrink the title further than the lines for width; in pixels the
    # title still leads.
    assert TITLE_SIZE * plan.title_scale > LINE_SIZE * plan.line_scale
    assert plan.top >= plan.margin * 0.5
    assert plan.bottom <= plan.limit < plan.footer_y


# v1.4: a card's picture takes the right 45 % of a 16:9 frame or the lower part of a 9:16 or
# 1:1 one, so the same extremes must still fit the narrower or shorter text area.
EXAMPLE_IMAGE = ROOT / "contract/examples/images/example-page.png"
PICTURES = [
    pytest.param({"image": EXAMPLE_IMAGE}, id="image"),
    pytest.param({"qr": "https://github.com/kyletabor/video-editor-bot/" + "x" * 250}, id="qr"),
]


@pytest.mark.parametrize("picture", PICTURES)
@pytest.mark.parametrize("size", FRAMES)
def test_contract_extremes_beside_a_picture_stay_whole(size, picture):
    card = Card(LONG_WORD_TITLE, LONG_LINES, Fraction(3), chapter_footer(9, 10, 3947.23), **picture)
    plan = layout(card, size)
    assert words(plan.title_rows) == LONG_WORD_TITLE.split()
    for line, rows in zip(LONG_LINES, plan.line_rows):
        assert words(rows) == line.split()
        assert 1 <= len(rows) <= MAX_ROWS_PER_LINE
        assert all(plan.line_font.getlength(row) <= plan.column for row in rows)
    assert all(plan.title_font.getlength(row) <= plan.column for row in plan.title_rows)
    assert "..." not in " ".join(plan.title_rows) + " ".join(plan.body_rows)
    assert TITLE_SIZE * plan.title_scale > LINE_SIZE * plan.line_scale
    assert plan.bottom <= plan.limit < plan.footer_y
    left, top, right, bottom = plan.picture
    width, height = size
    assert 0 <= left < right <= width and 0 <= top < bottom < plan.footer_y
    if width > height:  # beside: the text column ends left of the picture
        assert plan.margin + plan.column < left and right - left > width * 0.3
    else:  # below: the picture starts under the text and keeps a usable share of the frame
        assert top >= plan.bottom and bottom - top > height * 0.25
    # The drawn text stays left of (or above) the picture box.
    image = draw_card(card, size)
    bright = image.convert("L").point(lambda value: 255 if value > 170 else 0)
    area = (0, 0, left, height) if width > height else (0, 0, width, top)
    text_box = bright.crop(area).getbbox()
    assert text_box is not None
    assert text_box[2] <= plan.margin + plan.column + 3


def test_a_tall_card_shrinks_its_lines_before_its_title():
    # Everything fits the width at full size on 16:9, but four two-row lines under a three-row
    # title would run into the footer band: the lines give way first.
    card = Card(
        TRUNCATED_CARDS[1][0],
        tuple(card[1][0] for card in TRUNCATED_CARDS) + (TRUNCATED_CARDS[0][1][1],),
    )
    plan = layout(card, (1920, 1080))
    assert plan.title_scale == 1.0 and plan.line_scale < 1.0
    assert words(plan.title_rows) == card.title.split()
    assert plan.bottom <= plan.limit


@pytest.mark.parametrize("size", FRAMES)
def test_card_png_keeps_the_text_inside_the_safe_area(size):
    card = Card(LONG_WORD_TITLE, LONG_LINES, Fraction(3), chapter_footer(9, 10, 3947.23))
    plan = layout(card, size)
    image = draw_card(card, size)
    # Title and lines are the only bright pixels: the footer (L 140) and the accent bar
    # (L 142) stay under the threshold, so this box is the text block alone.
    bright = image.convert("L").point(lambda value: 255 if value > 170 else 0)
    left, top, right, bottom = bright.getbbox()
    width, height = size
    slack = 3  # antialiasing and glyph ink past the advance width
    assert plan.margin - slack <= left and right <= width - plan.margin + slack
    assert plan.top - slack <= top and bottom <= plan.limit
    assert bright.crop((0, round(plan.limit), width, height)).getbbox() is None
    footer_band = image.convert("L").crop((0, round(plan.footer_y), width, height))
    assert footer_band.getextrema()[1] > 100  # the footer itself was drawn


def test_text_beyond_the_contract_falls_back_to_the_smallest_step_and_an_ellipsis():
    absurd = " ".join(["word"] * 400)
    plan = layout(Card(absurd, ("x" * 600,)), (1920, 1080))
    assert plan.title_scale == SHRINK_STEPS[-1]
    assert len(plan.title_rows) == MAX_TITLE_ROWS and plan.title_rows[-1].endswith("...")
    assert len(plan.line_rows[0]) == MAX_ROWS_PER_LINE and plan.line_rows[0][-1].endswith("...")
    assert all(plan.line_font.getlength(row) <= plan.column for row in plan.line_rows[0])
    assert plan.bottom <= plan.limit


def test_card_paints_bright_text_on_the_dark_background(tmp_path):
    card = Card("Title", ("first line", "second"), Fraction(3), chapter_footer(1, 2, 16))
    plan = layout(card, (320, 180))
    assert plan.title_scale == 1.0 and plan.line_scale == 1.0
    assert plan.title_rows == ("Title",) and plan.body_rows == ("first line", "second")
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
