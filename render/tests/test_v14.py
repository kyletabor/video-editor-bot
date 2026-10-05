"""Contract v1.4 rules that need no media tools: card pictures, card lists, pip, overlays."""

import json
from fractions import Fraction
from pathlib import Path

import pytest
import segno
from PIL import Image, ImageChops

from cliprender.cards import (
    BACKGROUND,
    PICTURE_BORDER,
    QR_QUIET,
    Card,
    check_pictures,
    draw_card,
    layout,
    qr_matrix,
    qr_placement,
)
from cliprender.framing import (
    BAND_COLOR,
    MIN_BAND_TILE,
    OVERLAY_TILE_SHARE,
    caption_style,
    pip_framing,
)
from cliprender.media import RenderError
from cliprender.overlays import (
    Run,
    alignment,
    ass_text,
    event_bounds,
    format_ass,
    kept_frames,
    overlay_runs,
)
from cliprender.reel import planned_seconds, timeline
from cliprender.renderer import render_plan, video_graph

ROOT = Path(__file__).resolve().parents[2]
URL = "https://github.com/kyletabor/video-editor-bot"
VIDEO = {"width": 1920, "height": 1080, "sample_aspect_ratio": "1:1"}


def titles(items):
    return [item if isinstance(item, str) else item.title for item in items]


# --- clips[].cards -------------------------------------------------------------------------------


@pytest.fixture
def story_plan():
    return {
        "clips": [
            {
                "id": "a",
                "takeaway": "A idea",
                "segments": [{"start": 16, "end": 28}],
                "cards": [
                    {"title": "Part 1", "seconds": 3},
                    {"title": "Link", "qr": URL, "seconds": 5},
                ],
                "card": {"title": "A card", "seconds": 2, "image": "pics/a.png"},
            },
            {
                "id": "b",
                "takeaway": "B idea",
                "segments": [{"start": 36, "end": 52}],
                "cards": [{"title": "Part 2"}],
            },
            {"id": "c", "takeaway": "C idea", "segments": [{"start": 60, "end": 70}]},
        ]
    }


def test_clip_cards_play_before_the_card_and_only_the_last_carries_the_footer(story_plan):
    items = timeline(story_plan, {"intro": {"title": "Intro"}})
    assert titles(items) == ["Intro", "Part 1", "Link", "A card", "a", "Part 2", "b", "c"]
    footers = [item.footer for item in items if isinstance(item, Card)]
    assert footers == ["", "", "", "1 of 3 · 0:00:16", "2 of 3 · 0:00:36"]
    assert items[2].qr == URL and items[2].seconds == 5
    # Without a resolver the image path is taken as written.
    assert items[3].image == Path("pics/a.png")
    # Cards 3 + 3 + 5 + 2 + 3 (defaults 3 s), clips 12 + 16 + 10.
    assert planned_seconds(items, story_plan) == 54


def test_clip_cards_show_under_every_chapter_mode(story_plan):
    none = timeline(story_plan, {"chapter_cards": "none"})
    assert titles(none) == ["Part 1", "Link", "a", "Part 2", "b", "c"]
    assert all(item.footer == "" for item in none if isinstance(item, Card))
    everything = timeline(story_plan, {"chapter_cards": "all"})
    assert titles(everything) == ["Part 1", "Link", "A card", "a", "Part 2", "B idea", "b"] + [
        "C idea",
        "c",
    ]
    assert everything[4].footer == "" and everything[5].footer == "2 of 3 · 0:00:36"


def test_card_image_paths_resolve_through_the_renderer_rule(story_plan):
    items = timeline(story_plan, {}, lambda value: Path("/repo") / value)
    assert items[2].image == Path("/repo/pics/a.png")


# --- card.image ----------------------------------------------------------------------------------


def write_picture(path, size, colour=(220, 40, 40), mode="RGB", fmt=None):
    Image.new(mode, size, colour).save(path, format=fmt)
    return path


@pytest.mark.parametrize("size", [(1920, 1080), (1080, 1920), (1080, 1080)])
def test_image_is_scaled_to_fit_its_box_never_cropped(tmp_path, size):
    picture = write_picture(tmp_path / "wide.png", (400, 100))
    card = Card("Title", ("one line",), image=picture)
    plan = layout(card, size)
    image = draw_card(card, size)
    red, green, _ = image.split()
    mask = ImageChops.multiply(
        red.point(lambda value: 255 if value > 150 else 0),
        green.point(lambda value: 255 if value < 100 else 0),
    )
    x0, y0, x1, y1 = mask.getbbox()
    left, top, right, bottom = plan.picture
    assert left <= x0 and x1 <= right and top <= y0 and y1 <= bottom
    # Aspect kept (4:1) and the box's width used: scaled to fit, not cropped.
    assert (x1 - x0) / (y1 - y0) == pytest.approx(4, rel=0.03)
    assert x1 - x0 >= (right - left) * 0.95
    # A subtle border frames it.
    assert image.getpixel((x0 - 1, (y0 + y1) // 2)) == PICTURE_BORDER
    if size[0] > size[1]:
        assert left > size[0] * 0.55
    else:
        assert top >= plan.bottom


def test_jpeg_and_transparent_png_are_accepted(tmp_path):
    jpeg = write_picture(tmp_path / "shot.jpg", (64, 64), fmt="JPEG")
    clear = write_picture(tmp_path / "clear.png", (64, 64), (0, 0, 0, 0), mode="RGBA")
    check_pictures([Card("a", image=jpeg), Card("b", image=clear)])
    image = draw_card(Card("b", image=clear), (320, 180))
    box = layout(Card("b", image=clear), (320, 180)).picture
    centre = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    # A transparent picture shows the card background, not black.
    assert image.getpixel(centre) == BACKGROUND


def test_missing_or_unreadable_images_fail_with_their_path(tmp_path):
    with pytest.raises(RenderError, match="Missing card image: .*nope.png"):
        check_pictures([Card("t", image=tmp_path / "nope.png")])
    text = tmp_path / "notes.png"
    text.write_text("not an image", encoding="utf-8")
    with pytest.raises(RenderError, match="Cannot read card image .*notes.png"):
        check_pictures([Card("t", image=text)])
    gif = write_picture(tmp_path / "anim.gif", (8, 8), fmt="GIF")
    with pytest.raises(RenderError, match="GIF; use a PNG or JPEG"):
        check_pictures([Card("t", image=gif)])


def test_missing_card_image_fails_the_plan_before_any_tool_runs(tmp_path):
    plan = json.loads((ROOT / "contract/examples/reel-story-v14.json").read_text("utf-8"))
    plan["output"]["dir"] = (tmp_path / "out").as_posix()
    plan["clips"][1]["card"]["image"] = (tmp_path / "gone.png").as_posix()
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    # A tool that does not exist would fail with "Tool not found" if anything ran first.
    with pytest.raises(RenderError, match="Missing card image: .*gone.png"):
        render_plan(path, root=ROOT, ffmpeg="/nonexistent/ffmpeg", ffprobe="/nonexistent/x")
    assert not (tmp_path / "out" / "reel.mp4").exists()


# --- card.qr -------------------------------------------------------------------------------------


def test_qr_matrix_is_segnos_symbol_for_the_exact_url():
    expected = segno.make(URL, error="m", micro=False)
    matrix = qr_matrix(URL)
    assert matrix == [[bool(cell) for cell in row] for row in expected.matrix]
    assert len(matrix) == expected.symbol_size(border=0)[0]
    assert qr_matrix(URL + "/") != matrix


@pytest.mark.parametrize("size", [(1920, 1080), (1080, 1920), (1080, 1080)])
def test_qr_card_draws_every_module_on_a_white_quiet_zone(size):
    card = Card("Get the clip bot", ("Open source",), qr=URL)
    plan = layout(card, size)
    place = qr_placement(URL, plan.picture, min(size) / 1080)
    image = draw_card(card, size).convert("L")
    left, top, right, bottom = plan.picture
    assert left <= place.x and place.x + place.side <= right
    assert top <= place.y and place.url_y + len(place.url_rows) * place.url_step <= bottom + 1
    assert place.module >= 4  # big enough for a phone across a room at 1080p
    # The white square: its corners and the whole quiet zone are white.
    edge = place.side - 1
    for dx, dy in ((0, 0), (edge, 0), (0, edge), (edge, edge)):
        assert image.getpixel((place.x + dx, place.y + dy)) == 255
    quiet = QR_QUIET * place.module
    for offset in range(0, place.side, max(1, place.module // 2)):
        assert image.getpixel((place.x + offset, place.y + quiet // 2)) == 255
    # Every module's centre has the matrix's colour: what a scanner reads.
    x0, y0 = place.symbol_origin
    half = place.module // 2
    for r, row in enumerate(qr_matrix(URL)):
        for c, dark in enumerate(row):
            value = image.getpixel((x0 + c * place.module + half, y0 + r * place.module + half))
            assert value == (0 if dark else 255), (r, c)
    # The URL is printed under the square.
    below = image.crop((left, place.url_y, right, bottom)).point(lambda v: 255 if v > 150 else 0)
    assert below.getbbox() is not None


# --- clips[].layout pip --------------------------------------------------------------------------


def test_pip_screen_filling_the_frame_takes_the_tile_over_it():
    pip = pip_framing(
        VIDEO, {"kind": "pip", "screen": [0, 0, 1440, 810], "speaker": [1440, 270, 480, 270]}, 1080
    )
    assert pip.dimensions == (1920, 1080) and pip.screen == (0, 0, 1920, 1080)
    assert pip.overlaid and pip.corner == "bottom-right"
    x, y, w, h = pip.tile
    assert w == round(1920 * OVERLAY_TILE_SHARE / 2) * 2 and w / h == pytest.approx(16 / 9, 0.02)
    # Bottom-right with a margin and the 2 px border inside the frame.
    assert 1920 - (x + w) == 1080 - (y + h) and 20 <= 1920 - (x + w) <= 40
    assert "crop=1440:810:0:0" in pip.filter and "crop=480:270:1440:270" in pip.filter
    assert "overlay=" in pip.filter and pip.filter.endswith("format=yuv420p")
    assert f"color={BAND_COLOR}" in pip.filter and BAND_COLOR == "0x12151A"


def test_pip_four_by_three_share_puts_the_tile_in_the_side_band():
    pip = pip_framing(
        VIDEO, {"kind": "pip", "screen": [0, 0, 1440, 1080], "speaker": [1440, 0, 480, 270]}, 1080
    )
    assert not pip.overlaid
    sx, sy, sw, sh = pip.screen
    assert (sx, sy, sw, sh) == (0, 0, 1440, 1080)  # pushed away from the right-hand corner
    x, y, w, h = pip.tile
    assert x >= sx + sw  # beside the screen, covering none of it
    assert h >= 1080 * MIN_BAND_TILE and x + w <= 1920 and y + h <= 1080
    left = pip_framing(
        VIDEO,
        {
            "kind": "pip",
            "screen": [0, 0, 1440, 1080],
            "speaker": [1440, 0, 480, 270],
            "corner": "top-left",
        },
        1080,
    )
    assert left.screen[0] == 480 and left.tile[0] + left.tile[2] <= 480 and left.tile[1] < 100


def test_pip_wide_share_puts_the_tile_in_the_band_below_or_above():
    spec = {"kind": "pip", "screen": [0, 0, 1920, 640], "speaker": [0, 700, 480, 270]}
    below = pip_framing(VIDEO, spec, 1080)
    assert not below.overlaid
    assert below.screen == (0, 0, 1920, 640)  # top-centred
    assert below.tile[1] >= 640 and below.tile[3] >= 1080 * MIN_BAND_TILE
    above = pip_framing(VIDEO, {**spec, "corner": "top-left"}, 1080)
    assert above.screen[1] == 440 and above.tile[1] + above.tile[3] <= 440


def test_pip_band_too_thin_for_a_useful_tile_lays_it_over_the_centred_screen():
    # A 1600 x 1000 share leaves a 192 px side band: the tile would be under 18 % tall.
    pip = pip_framing(
        VIDEO, {"kind": "pip", "screen": [0, 0, 1600, 1000], "speaker": [1600, 0, 320, 180]}, 1080
    )
    assert pip.overlaid
    assert pip.screen[2] == 1728 and pip.screen[0] == 96  # centred


def test_pip_without_a_speaker_letterboxes_the_screen_alone():
    pip = pip_framing(VIDEO, {"kind": "pip", "screen": [100, 100, 800, 800]}, 720)
    assert pip.dimensions == (1280, 720) and pip.tile is None
    assert pip.screen == (280, 0, 720, 720)
    assert "split" not in pip.filter and "pad=1280:720:280:0" in pip.filter
    assert caption_style(pip, 1280) is None


@pytest.mark.parametrize(
    ("region", "label"),
    [
        ([0, 0, 1921, 1080], "screen"),
        ([1800, 900, 200, 200], "screen"),
        ([0, 0, 1, 50], "screen"),
    ],
)
def test_pip_regions_outside_the_source_frame_are_rejected(region, label):
    with pytest.raises(RenderError, match=f"Clip c1: layout.{label} "):
        pip_framing(VIDEO, {"kind": "pip", "screen": region}, 1080, "c1")
    with pytest.raises(RenderError, match="layout.speaker"):
        pip_framing(
            VIDEO, {"kind": "pip", "screen": [0, 0, 100, 100], "speaker": [1900, 0, 40, 40]}, 1080
        )


def test_captions_keep_clear_of_a_bottom_tile_only():
    spec = {"kind": "pip", "screen": [0, 0, 1440, 810], "speaker": [1440, 270, 480, 270]}
    bottom = pip_framing(VIDEO, spec, 1080)
    x = bottom.tile[0]
    clearance = bottom.caption_clearance()
    assert clearance > 1920 - x  # tile, border and margin
    units = int(caption_style(bottom, 1920).split(",")[0].split("=")[1])
    # libass scales SRT margins by width / 384: the margin in pixels covers the tile.
    assert units * 1920 / 384 >= 1920 - x
    assert caption_style(bottom, 1920) == f"MarginL={units},MarginR={units}"
    top = pip_framing(VIDEO, {**spec, "corner": "top-right"}, 1080)
    assert caption_style(top, 1920) is None


# --- clips[].overlays ----------------------------------------------------------------------------


def frame_times(fps, seconds):
    return [Fraction(i, fps) for i in range(int(seconds * fps))]


def test_overlay_runs_follow_source_time_across_reordered_non_contiguous_segments():
    times = frame_times(10, 60)  # 10 fps, origin 0
    # Segments as `selections` returns them: (first, stop, start, end, offset), output order.
    selected = [
        (300, 330, Fraction(30), Fraction(33), Fraction(0)),
        (100, 120, Fraction(10), Fraction(12), Fraction(3)),
        (310, 320, Fraction(31), Fraction(32), Fraction(5)),
    ]
    frames = kept_frames(selected, times, Fraction(0))
    assert (
        frames[0] == (30, 0)
        and frames[30] == (10, 3)
        and frames[-1] == (Fraction(319, 10), 6 - Fraction(1, 10))
    )
    overlays = [
        {"start": 31.5, "end": 40, "text": "Screen", "position": "top"},
        {"start": 11, "end": 11.25, "text": "Brief"},
        {"start": 0, "end": 5, "text": "Never kept"},
    ]
    runs = overlay_runs(overlays, frames)
    assert [(r.first, r.last, r.text) for r in runs] == [
        (Fraction(15, 10), Fraction(29, 10), "Screen"),  # source 31.5-32.9 in the first segment
        (Fraction(4), Fraction(42, 10), "Brief"),  # source 11.0-11.2, end exclusive
        (Fraction(55, 10), Fraction(59, 10), "Screen"),  # the repeat of source 31.5-31.9
    ]
    assert runs[1].position == "top"


@pytest.mark.parametrize("fps", [24, 25, Fraction(30000, 1001), 60])
def test_event_bounds_include_exactly_the_runs_frames(fps):
    times = [Fraction(i) / fps for i in range(200)]
    run = Run(times[37], times[90], "x")
    start, end = event_bounds(run)

    def shown(t):
        ms = round(t * 1000)  # FFmpeg may round to the nearest millisecond either way
        return start * 10 <= ms < end * 10 and start * 10 <= int(t * 1000) < end * 10

    assert [i for i in range(200) if shown(times[i])] == list(range(37, 91))


def test_ass_script_draws_labels_in_frame_pixels():
    runs = [
        Run(Fraction(1), Fraction(2), "Top {label}"),
        Run(Fraction(3), Fraction(4), "Low", "bottom"),
    ]
    script = format_ass(runs, (1920, 1080))
    assert "PlayResX: 1920" in script and "PlayResY: 1080" in script
    # 3.2 % of 1080 is 35 px; an opaque box (BorderStyle 3) behind white text.
    assert "Style: top,Helvetica,35,&H00FFFFFF,&H00FFFFFF,&H50101010" in script
    assert ",3,10,0,7,67,67,49,1" in script and ",3,10,0,1,67,67,49,1" in script
    assert "Dialogue: 0,0:00:00.99,0:00:02.01,top,,0,0,0,,Top (label)" in script
    assert "Dialogue: 0,0:00:02.99,0:00:04.01,bottom,,0,0,0,,Low" in script
    assert ass_text("a\\Nb {c}") == "a⧵Nb (c)"
    assert alignment("top", "top-left") == 9 and alignment("bottom", "bottom-left") == 3
    assert alignment("top", "bottom-right") == 7


# --- the per-part graph ------------------------------------------------------------------------------


def test_video_graph_without_v14_fields_is_unchanged():
    part = (10, 20, Fraction(1), Fraction(2), Fraction(0))
    assert video_graph(part, Fraction(0), "scale=2:2", 0, "_captions.srt") == (
        "[0:v:0]trim=start_frame=10:end_frame=20,settb=AVTB,setpts=PTS-(1000000),"
        "scale=2:2,subtitles=filename=_captions.srt[video]"
    )
    styled = video_graph(
        part, Fraction(0), "scale=2:2", 0, "_captions.srt", "MarginL=9,MarginR=9", "_overlays.ass"
    )
    assert styled.endswith(
        "subtitles=filename=_captions.srt:force_style='MarginL=9,MarginR=9',"
        "subtitles=filename=_overlays.ass[video]"
    )
    labels_only = video_graph(part, Fraction(0), "scale=2:2", 0, None, None, "_overlays.ass")
    assert labels_only.endswith("scale=2:2,subtitles=filename=_overlays.ass[video]")
