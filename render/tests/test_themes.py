"""Themes (`cliprender --theme`): loading, the Art of Intelligence cards, captions, labels and
the horizon wipe. No media tools needed."""

import json
from fractions import Fraction

import pytest
from PIL import Image
from test_cards import FRAMES, LONG_LINES, LONG_WORD_TITLE

from cliprender.cards import Card, draw_card, wrap, write_card_png
from cliprender.media import RenderError
from cliprender.overlays import LabelLook, Run, format_ass
from cliprender.reel import timeline
from cliprender.themes import THEMES, Theme, aoi, load_theme

KINDS = ("intro", "opening", "section", "chapter", "closing", "outro")
VALUES = aoi.parse_values({"episode": "04", "date": "Friday, October 9"})


def card(kind, title=LONG_WORD_TITLE, lines=LONG_LINES, **picture):
    extra = {"chapter": (7, 12), "at": Fraction(3428)} if kind == "chapter" else {}
    return Card(title, tuple(lines), kind=kind, **extra, **picture)


def words(text):
    return text.upper().split()


def shown_in_order(expected, drawn):
    """True when the words of `expected` appear, in order and whole, among the drawn runs."""
    pool = [word for run in drawn for word in words(run.text)]
    position = 0
    for word in words(expected):
        try:
            position = pool.index(word, position) + 1
        except ValueError:
            return False
    return True


def test_built_in_names_and_the_theme_file(tmp_path):
    assert THEMES == ("aoi",)
    by_name = load_theme("aoi")
    assert isinstance(by_name, Theme) and by_name.name == "aoi"
    path = tmp_path / "theme.json"
    path.write_text(json.dumps({"theme": "aoi", "episode": "04", "date": "Friday, October 9"}))
    from_file = load_theme(str(path))
    assert from_file.name == "aoi"
    # Relative paths may also be given from the repository root.
    assert load_theme("theme.json", root=tmp_path).name == "aoi"


@pytest.mark.parametrize(
    "content, message",
    [
        ({"theme": "nope"}, "theme"),
        ({"episode": "04"}, "theme"),
        ({"theme": "aoi", "episod": "04"}, "unknown key"),
        ({"theme": "aoi", "episode": 4}, "episode"),
        ({"theme": "aoi", "episode": "12345"}, "at most 4"),
        ({"theme": "aoi", "date": "x" * 41}, "at most 40"),
    ],
)
def test_theme_files_are_checked_before_anything_renders(tmp_path, content, message):
    path = tmp_path / "theme.json"
    path.write_text(json.dumps(content))
    with pytest.raises(RenderError, match=message):
        load_theme(str(path))


def test_an_unknown_name_says_what_exists():
    with pytest.raises(RenderError, match="aoi"):
        load_theme("pipeline-orange")


def test_timeline_tells_the_theme_where_each_card_sits():
    plan = {
        "clips": [
            {
                "id": "a",
                "takeaway": "A",
                "segments": [{"start": 744, "end": 790}],
                "cards": [{"title": "Section"}],
                "card": {"title": "Chapter A"},
            },
            {"id": "b", "takeaway": "B", "segments": [{"start": 900, "end": 930}]},
        ]
    }
    reel = {
        "intro": {"title": "Intro"},
        "opening": [{"title": "Learn"}],
        "closing": [{"title": "Takeaways"}],
        "outro": {"title": "Bye"},
        "chapter_cards": "all",
    }
    items = [item for item in timeline(plan, reel) if isinstance(item, Card)]
    assert [(item.title, item.kind) for item in items] == [
        ("Intro", "intro"),
        ("Learn", "opening"),
        ("Section", "section"),
        ("Chapter A", "chapter"),
        ("B", "chapter"),
        ("Takeaways", "closing"),
        ("Bye", "outro"),
    ]
    assert items[3].chapter == (1, 2) and items[3].at == 744
    assert items[4].chapter == (2, 2) and items[4].at == 900
    assert items[2].chapter is None and items[2].footer == ""
    # The default look still gets exactly the footer it always had.
    assert items[3].footer == "1 of 2 · 0:12:24"


def test_the_default_look_is_unchanged_without_a_theme(tmp_path):
    sample = Card("A title", ("A line",), footer="1 of 2 · 0:00:10")
    plain = write_card_png(sample, (640, 360), tmp_path / "plain.png")
    with Image.open(plain) as written:
        assert written.tobytes() == draw_card(sample, (640, 360)).tobytes()


@pytest.mark.parametrize("size", FRAMES)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("picture", [{}, {"qr": "https://example.com/a/long/path?with=query"}])
def test_contract_extremes_are_shown_whole_inside_the_frame(size, kind, picture):
    """The contract's longest title and four longest lines, in long words, on every aspect:
    every word is drawn, no run is ellipsized, and every run stays inside the frame and
    above the horizon band at the bottom edge."""
    width, height = size
    sample = card(kind, **picture)
    with aoi.recording() as drawn:
        image = aoi.draw_card(sample, size, VALUES)
    assert image.size == size and image.mode == "RGB"
    assert shown_in_order(sample.title, drawn)
    for line in sample.lines:
        assert shown_in_order(line, drawn)
    assert not any("..." in run.text for run in drawn)
    band = 4 * min(size) / 1080
    for run in drawn:
        left, top, right, bottom = run.box
        assert left >= 0 and top >= 0, run
        assert right <= width + 1 and bottom <= height - band, run


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_paints_light_text_on_the_navy_ground(kind):
    image = aoi.draw_card(card(kind, title="A title", lines=("A line",)), (960, 540), VALUES)
    assert image.getpixel((2, 2)) == pytest.approx(aoi.NAVY_900, abs=6)
    colours = {colour for _, colour in image.getcolors(960 * 540)}
    assert any(min(colour) > 200 for colour in colours), "no light text"
    assert any(colour[0] > 200 and colour[2] < 80 for colour in colours), "no sun colours"


def test_chapter_header_names_the_episode_and_where_the_clip_starts():
    with aoi.recording() as drawn:
        aoi.draw_card(card("chapter", title="A", lines=()), (1920, 1080), VALUES)
    texts = [run.text for run in drawn]
    assert "EP 04 · 0:57:08" in texts
    assert "CHAPTER 07 / 12" in texts


def test_without_values_the_cards_leave_out_what_they_would_show():
    with aoi.recording() as drawn:
        aoi.draw_card(card("intro", title="A", lines=()), (1920, 1080), aoi.parse_values({}))
    texts = " ".join(run.text for run in drawn)
    assert "EP" not in texts.split()


def test_captions_and_labels_take_the_overlay_board_look():
    theme = load_theme("aoi")
    assert "BorderStyle=3" in theme.caption_style
    assert "PrimaryColour=&H00F8F1EA" in theme.caption_style
    assert "OutlineColour=&H2026160A" in theme.caption_style
    script = format_ass(
        [Run(Fraction(1), Fraction(2), "Label")], (1920, 1080), look=theme.label_look
    )
    assert ",&H00F8F1EA,&H00F8F1EA,&H2026160A,&H2026160A,-1," in script


def test_labels_keep_the_renderer_look_by_default():
    run = [Run(Fraction(1), Fraction(2), "Label")]
    assert format_ass(run, (1920, 1080)) == format_ass(run, (1920, 1080), look=LabelLook())
    assert "Style: top,Helvetica,35,&H00FFFFFF,&H00FFFFFF,&H50101010,&H50101010,0," in format_ass(
        run, (1920, 1080)
    )


def test_balanced_titles_keep_their_rows_and_words():
    face = aoi.font("display", 100)
    title = "Claude Design made the reel's slides and transitions, on brand"
    for column in (900, 1200, 1500, 1728):
        greedy = tuple(wrap(title, face, column))
        balanced = aoi.balance(greedy, face, column)
        assert len(balanced) == len(greedy)
        assert " ".join(balanced).split() == title.split()
        assert all(face.getlength(row) <= column for row in balanced)
        widths = [face.getlength(row) for row in balanced]
        assert max(widths) - min(widths) <= max(face.getlength(row) for row in greedy) - min(
            face.getlength(row) for row in greedy
        )


@pytest.fixture
def wipe():
    pytest.importorskip("numpy")  # module transitions need the renderer's `styles` extra
    from cliprender.themes import horizon_wipe

    return horizon_wipe


def shots(height=90, width=160):
    import numpy as np

    return np.full((height, width, 3), 20, np.uint8), np.full((height, width, 3), 200, np.uint8)


def test_wipe_starts_on_the_outgoing_shot_and_ends_on_the_incoming_one(wipe):
    a, b = shots()
    assert (wipe.render(a, b, 0.0, {}) == a).all()
    assert (wipe.render(a, b, 1.0, {}) == b).all()


def test_wipe_raises_the_new_shot_from_the_bottom(wipe):
    a, b = shots()
    state = {}
    frame = wipe.render(a, b, 0.5, state)
    assert frame.shape == a.shape and frame.dtype == a.dtype
    assert (frame[0] == a[0]).all() and (frame[-1] == b[-1]).all()
    # Rows still showing the outgoing shot, untouched by the band or its glow, shrink as the
    # band rises.
    outgoing = [
        int((wipe.render(a, b, t, state)[:, 0, 0] == 20).sum()) for t in (0.2, 0.4, 0.6, 0.8)
    ]
    assert outgoing == sorted(outgoing, reverse=True) and outgoing[0] > outgoing[-1]


def test_wipe_band_glows_in_the_horizon_colours(wipe):
    a, b = shots()
    frame = wipe.render(a, b, 0.5, {})
    assert (frame[..., 0].astype(int) - frame[..., 2] > 100).any()


def test_wipe_eases_on_the_series_curve(wipe):
    assert wipe.ease(0) == pytest.approx(0, abs=1e-6)
    assert wipe.ease(1) == pytest.approx(1, abs=1e-6)
    assert wipe.ease(0.5) == pytest.approx(0.5, abs=1e-3)
    # Slow out of the start, slow into the end: cubic-bezier(0.6, 0, 0.4, 1).
    assert wipe.ease(0.25) < 0.25 and wipe.ease(0.75) > 0.75
