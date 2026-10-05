"""Contract v1.4 end to end: the story example reel, pip framing and overlays on real video."""

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageStat
from test_integration import ROOT, cli, probe

from cliprender.cards import BACKGROUND
from cliprender.framing import pip_framing
from cliprender.overlays import FONT_SHARE, MARGIN_X_SHARE, MARGIN_Y_SHARE

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Integration tests require FFmpeg and ffprobe on PATH",
)
DEMO = ROOT / "assets/demo-clip.mp4"
STORY = ROOT / "contract/examples/reel-story-v14.json"


def frame_at(path, seconds, size=None):
    """The decoded RGB frame shown at `seconds` (accurate seek), optionally resized."""
    data = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{seconds:.3f}", "-i", str(path), "-frames:v", "1"]
        + ["-f", "image2pipe", "-c:v", "png", "-"],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    image = Image.open(io.BytesIO(data)).convert("RGB")
    return image.resize(size, Image.Resampling.BILINEAR) if size else image


def difference(a, b):
    """Mean absolute difference per channel, averaged: 0 identical, 255 opposite."""
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def write_plan(tmp_path, plan):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def rows(result):
    assert result.returncode == 0, result.stderr
    return {row[0]: row for row in (line.split("\t") for line in result.stdout.splitlines())}


def label_box(size):
    """Where a top label's box sits on a frame of `size` (left part only, a few words wide)."""
    width, height = size
    x, y = round(width * MARGIN_X_SHARE), round(height * MARGIN_Y_SHARE)
    return (x, y, x + round(width * 0.12), y + round(height * FONT_SHARE))


def test_story_example_reel_has_its_cards_a_pip_clip_and_a_label(tmp_path):
    plan = json.loads(STORY.read_text(encoding="utf-8"))
    plan["output"]["dir"] = (tmp_path / "out").as_posix()
    result = rows(cli(write_plan(tmp_path, plan)))
    reel = Path(result["reel"][1])
    # intro 4 + cards 3 + 5 + clip 12 + image card 4 + clip 16: the v1.4 cards are in the reel.
    assert float(result["reel"][2]) == pytest.approx(44, abs=0.1)
    assert float(probe(reel)["format"]["duration"]) == pytest.approx(44, abs=0.15)

    video = next(s for s in probe(DEMO)["streams"] if s["codec_type"] == "video")
    layout = plan["clips"][0]["layout"]
    pip = pip_framing(video, layout, 1080)
    x, y, w, h = pip.tile
    # Reel 15.02 s is clip 3.02 s, source 19.02 s: inside the label's [17, 22).
    shown = frame_at(reel, 15.02)
    source = frame_at(DEMO, 19.02)
    sx, sy, sw, sh = layout["speaker"]
    expected_tile = source.crop((sx, sy, sx + sw, sy + sh)).resize((w, h))
    tile = shown.crop((x, y, x + w, y + h))
    # The tile shows the speaker region (and no caption runs over it) ...
    assert difference(tile, expected_tile) < 12
    # ... unlike the background colour or the screen picture under it.
    assert difference(tile, Image.new("RGB", (w, h), BACKGROUND)) > 40
    screen_under = source.crop((0, 0, 1440, 810)).resize((1920, 1080)).crop((x, y, x + w, y + h))
    assert difference(tile, screen_under) > 2 * difference(tile, expected_tile)
    # The light border frames the tile.
    border = shown.crop((x - 2, y + 10, x, y + h - 10)).convert("L")
    assert ImageStat.Stat(border).mean[0] > 180

    # The label sits top-left while the source time is in [17, 22) and is gone after it.
    expected_screen = source.crop((0, 0, 1440, 810)).resize((1920, 1080))
    box = label_box((1920, 1080))
    with_label = difference(shown.crop(box), expected_screen.crop(box))
    later = frame_at(reel, 19.02)  # source 23.02
    later_expected = frame_at(DEMO, 23.02).crop((0, 0, 1440, 810)).resize((1920, 1080))
    without_label = difference(later.crop(box), later_expected.crop(box))
    assert with_label > 25 and without_label < 8

    # QR and image cards are drawn into the reel: a white square right of centre at 9 s.
    qr_frame = frame_at(reel, 9.0).convert("L")
    assert qr_frame.getpixel((1450, 200)) > 235 and qr_frame.getpixel((300, 200)) < 40


def overlay_plan(tmp_path, overlays, *, aspect="16:9", layout=None, name="out"):
    clip = {
        "id": "labelled",
        "takeaway": "Labels follow the picture.",
        "segments": [{"start": 16, "end": 19}, {"start": 30, "end": 33}],
    }
    if overlays:
        clip["overlays"] = overlays
    if layout:
        clip["layout"] = layout
    plan = {
        "version": "1",
        "source": {"path": DEMO.as_posix(), "captions": {"kind": "none"}},
        "output": {"dir": (tmp_path / name).as_posix(), "aspect": aspect, "max_height": 720},
        "clips": [clip],
    }
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def test_overlays_burn_without_captions_on_exactly_the_kept_source_times(tmp_path):
    overlays = [
        {"start": 17, "end": 18, "text": "First label"},
        {"start": 31, "end": 40, "text": "Second label"},
    ]
    labelled = Path(rows(cli(overlay_plan(tmp_path, overlays)))["labelled"][1])
    plain = Path(rows(cli(overlay_plan(tmp_path, [], name="plain")))["labelled"][1])
    box = label_box((1280, 720))
    # Output time -> source time: 0.5->16.5, 1.5->17.5, 2.5->18.5, 3.5->30.5, 4.5->31.5.
    shown = {}
    for output, _ in ((0.52, 16.5), (1.52, 17.5), (2.52, 18.5), (3.52, 30.5), (4.52, 31.5)):
        a, b = frame_at(labelled, output), frame_at(plain, output)
        shown[output] = difference(a.crop(box), b.crop(box))
    assert shown[1.52] > 20 and shown[4.52] > 20
    assert shown[0.52] < 3 and shown[2.52] < 3 and shown[3.52] < 3


def test_pip_is_ignored_with_a_warning_off_16_9(tmp_path):
    layout = {"kind": "pip", "screen": [0, 0, 1440, 810], "speaker": [1440, 270, 480, 270]}
    result = cli(overlay_plan(tmp_path, [], aspect="9:16", layout=layout))
    assert "layout pip is only used for 16:9 output" in result.stderr
    out = Path(rows(result)["labelled"][1])
    plain = Path(rows(cli(overlay_plan(tmp_path, [], aspect="9:16", name="plain")))["labelled"][1])
    assert out.read_bytes() == plain.read_bytes()


def test_pip_region_outside_the_source_fails_before_writing(tmp_path):
    layout = {"kind": "pip", "screen": [0, 0, 2000, 810]}
    result = cli(overlay_plan(tmp_path, [], layout=layout))
    assert result.returncode != 0
    assert "layout.screen (0, 0, 2000, 810) reaches past" in result.stderr
    assert not (tmp_path / "out" / "labelled.mp4").exists()
