"""redact.py: boxes, ranges, the ffmpeg graph, OCR parsing and pattern matching.

Every name, address and number below is made up (example.com, 555-01xx phone
numbers, the standard Visa test card): nothing here comes from a recording.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clipbot import cli
from clipbot import redact as rd
from clipbot.redact import Redaction, Word


def _ffmpeg_present() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_present(), reason="needs ffmpeg")


def line_words(*texts: str, y: int = 100, line=(1, 1, 1), conf: float = 90.0) -> list[Word]:
    """Words laid out left to right, 10 px per character plus a 10 px space."""
    out, x = [], 10
    for t in texts:
        out.append(Word(t, (x, y, 10 * len(t), 20), conf, line))
        x += 10 * len(t) + 10
    return out


# ----------------------------------------------------------------- boxes and ranges

def test_clamp_box_pads_grows_to_the_minimum_snaps_even_and_stays_in_frame():
    assert rd.clamp_box([101, 51, 40, 21], 1920, 1080, pad=5) == (96, 46, 50, 30)
    tiny = rd.clamp_box([500, 500, 4, 4], 1920, 1080)
    assert tiny[2] >= rd.MIN_BOX and tiny[3] >= rd.MIN_BOX  # boxblur needs room for its radius
    assert rd.clamp_box([1900, 1070, 100, 100], 1920, 1080) == (1900, 1070, 20, 10)  # cut at the frame edge
    assert all(v % 2 == 0 for v in rd.clamp_box([3, 5, 33, 17], 640, 360))  # yuv420p chroma
    assert rd.clamp_box([3000, 10, 50, 50], 1920, 1080) is None
    assert rd.clamp_box([10, 10, 0, 50], 1920, 1080) is None


def test_merge_joins_the_same_box_across_touching_ranges_only():
    a = Redaction(0, 2, (100, 100, 200, 30), "email")
    b = Redaction(1, 3, (102, 101, 198, 30), "email")  # OCR boxes the same word a pixel off
    c = Redaction(5, 7, (100, 100, 200, 30), "email")  # same box, later, not touching
    d = Redaction(1, 3, (900, 600, 80, 30), "phone")
    merged = rd.merge([c, d, b, a])
    assert merged == [
        Redaction(0, 3, (100, 100, 200, 31), "email"),
        Redaction(1, 3, (900, 600, 80, 30), "phone"),
        Redaction(5, 7, (100, 100, 200, 30), "email"),
    ]
    both = rd.merge([Redaction(0, 2, (0, 0, 100, 20), "key"), Redaction(2, 4, (0, 0, 100, 20), "token")])
    assert both == [Redaction(0, 4, (0, 0, 100, 20), "key, token")]


def test_load_redactions_reads_clock_times_and_clamps(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps([
        {"start": "1:02:03", "end": "1:02:10.5", "box": [0, 230, 1440, 620], "why": "screen share"},
        {"start": 5, "end": 9999, "box": [10, 10, 50, 50]},
    ]))
    got = rd.load_redactions(p, 1920, 1080, duration=4000)
    assert got[0] == Redaction(3723.0, 3730.5, (0, 230, 1440, 620), "screen share")
    assert got[1].end == 4000 and got[1].why == ""
    assert [r.spec() for r in got][0] == {"start": 3723.0, "end": 3730.5, "box": [0, 230, 1440, 620], "why": "screen share"}


@pytest.mark.parametrize("record, message", [
    ({"start": 1, "box": [0, 0, 10, 10]}, "needs start, end"),
    ({"start": 1, "end": 2, "box": [0, 0, 10]}, r"box must be \[x, y, w, h\]"),
    ({"start": 3, "end": 2, "box": [0, 0, 10, 10]}, "end must be after start"),
    ({"start": 1, "end": 2, "box": [5000, 0, 10, 10]}, r"box \[5000, 0, 10, 10\] lies outside the 1920x1080 frame"),
])
def test_load_redactions_names_the_bad_record(tmp_path, record, message):
    p = tmp_path / "r.json"
    p.write_text(json.dumps([record]))
    with pytest.raises(ValueError, match=f"redaction 1: {message}"):
        rd.load_redactions(p, 1920, 1080)


def test_write_then_load_round_trips(tmp_path):
    rs = [Redaction(1.5, 4.25, (10, 20, 100, 40), "email")]
    rd.write_redactions(tmp_path / "r.json", rs)
    assert rd.load_redactions(tmp_path / "r.json", 640, 360) == rs


# ----------------------------------------------------------------- ffmpeg arguments

def test_mask_timeline_widens_by_a_frame_and_merges_equal_neighbours():
    a, b = (0, 230, 1440, 620), (100, 100, 40, 20)
    tl = rd.mask_timeline([Redaction(1.5, 2.5, a), Redaction(2.0, 7, b), Redaction(7.0, 9, b)], 10, 0.04)
    assert tl == [(0.0, 1.46, ()), (1.46, 1.96, (a,)), (1.96, 2.54, tuple(sorted({a, b}))),
                  (2.54, 9.04, (b,)), (9.04, 10.0, ())]
    assert rd.mask_timeline([], 4, 0.04) == [(0.0, 4.0, ())]


def test_mask_track_writes_one_png_per_distinct_mask_and_a_concat_list(tmp_path):
    info = rd.VideoInfo(64, 48, 25.0, 10.0)
    listing = rd.write_mask_track([Redaction(1, 2, (8, 8, 16, 16)), Redaction(5, 6, (8, 8, 16, 16))], info, tmp_path)
    text = listing.read_text().splitlines()
    assert text[0] == "ffconcat version 1.0"
    files = [ln for ln in text if ln.startswith("file")]
    assert len(set(files)) == 2 and len(list(tmp_path.glob("mask*.png"))) == 2  # black and the one box, reused
    durations = [float(ln.split()[1]) for ln in text if ln.startswith("duration")]
    assert sum(durations) == pytest.approx(10 + 2)  # the source plus the padding past its end
    png = (tmp_path / "mask0000.png").read_bytes()
    assert png.startswith(b"\x89PNG") and b"IHDR" in png


def test_filter_graph_is_one_blur_and_one_blend_whatever_the_box_count():
    g = rd.filter_graph(rd.VideoInfo(1920, 1080, 24.0))
    assert g.count("boxblur") == 1 and g.count("alphamerge") == 1 and "fps=24.000000" in g
    assert "luma_radius=20" in g and g.endswith("[vout]")
    assert "luma_radius=7" in rd.filter_graph(rd.VideoInfo(32, 16, 25.0))  # small frames: boxblur's limit


def test_apply_args_copy_audio_and_captions_and_tag_the_file():
    args = rd.apply_args("in file.mp4", Path("m/masks.ffconcat"), rd.VideoInfo(640, 360, 25.0), "out.mp4", tag="abc")
    assert args[0] == "ffmpeg" and "in file.mp4" in args and args[-1] == "out.mp4"
    assert args[args.index("concat") - 1] == "-f" and str(Path("m/masks.ffconcat")) in args
    assert args[args.index("-c:a") + 1] == "copy" and args[args.index("-c:s") + 1] == "mov_text"
    assert ["-map", "0:a?"] == args[args.index("0:a?") - 1: args.index("0:a?") + 1]
    assert ["-map", "0:s?"] == args[args.index("0:s?") - 1: args.index("0:s?") + 1]
    assert f"comment={rd.TAG}:abc" in args


def test_sample_times_cover_each_span_including_its_end():
    assert rd.sample_times([(10, 12.5)], every=1) == [10, 11, 12, 12.45]
    assert rd.sample_times([(0, 1), (1, 2)], every=1) == [0, 0.95, 1, 1.95]
    assert rd.sample_times([(5, 5)], every=1) == []


# ----------------------------------------------------------------- OCR output and patterns

TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "1\t1\t0\t0\t0\t0\t0\t0\t3840\t2160\t-1\t\n"
    "4\t1\t1\t1\t1\t0\t200\t700\t900\t40\t-1\t\n"
    "5\t1\t1\t1\t1\t1\t200\t700\t140\t40\t95.1\tContact\n"
    "5\t1\t1\t1\t1\t2\t360\t700\t400\t40\t91.0\tpat@example.com\n"
    "5\t1\t1\t1\t1\t3\t780\t700\t30\t40\t-1\t \n"
)
TSV_HEADER = TSV.splitlines(keepends=True)[0]
TSV_ROW = "5\t1\t1\t1\t1\t1\t200\t300\t140\t40\t95\t{text}\n"
THUMB_SIZE = rd.THUMB[0] * rd.THUMB[1]


def test_parse_tsv_keeps_words_and_undoes_the_upscale():
    words = rd.parse_tsv(TSV, scale=2)
    assert words == [
        Word("Contact", (100, 350, 70, 20), 95.1, (1, 1, 1)),
        Word("pat@example.com", (180, 350, 200, 20), 91.0, (1, 1, 1)),
    ]


def test_find_sensitive_boxes_only_the_private_part_of_a_line():
    words = line_words("Contact", "pat@example.com", "or", "(415)", "555-0142", "today")
    hits = rd.find_sensitive(words)
    whys = sorted(w for _, w in hits)
    assert whys == ["email", "phone"]
    boxes = dict((w, b) for b, w in hits)
    assert boxes["email"] == words[1].box
    assert boxes["phone"] == rd.union(words[3].box, words[4].box)  # a number split over two OCR words


@pytest.mark.parametrize("text, why", [
    ("sk-test1234567890abcdefXYZ", "key"),
    ("ghp_abcdefghijklmnopqrstuvwxyz0123", "key"),
    ("AKIAABCDEFGHIJKLMNOP", "key"),
    ("4111-1111-1111-1111", "card"),
    ("078-05-1120", "ssn"),
    ("078 05 1120", "ssn"),
    ("+44 20 7946 0958", "phone"),
    ("+49 30 901820", "phone"),
    ("+1 415 555 0142", "phone"),
    ("4155550142", "phone"),
    ("Zx9Qp2Lm7Rt4Vb8Nc1Kd6Hf3", "token"),
])
def test_find_sensitive_kinds(text, why):
    assert why in [w for _, w in rd.find_sensitive(line_words("value:", text))]


@pytest.mark.parametrize("text", [
    "4111-1111-1111-1112",  # fails the Luhn check: a part number, not a card
    "1234567890123",  # an order number: 13 digits with no separators is not a phone
    "Pipeline", "2026-10-03", "github.com/kyletabor/video-editor-bot", "transcription_pipeline_overview",
])
def test_find_sensitive_leaves_ordinary_text(text):
    assert rd.find_sensitive(line_words("see", text)) == []


def test_find_sensitive_catches_an_address_ocr_split_at_the_at_sign():
    words = line_words("mail", "alice", "@", "example.com")
    hits = rd.find_sensitive(words)
    assert [w for _, w in hits] == ["email"]
    assert hits[0][0] == rd.union(words[1].box, words[3].box)


def test_terms_match_case_insensitively_across_words_and_regexes():
    terms = rd.compile_terms(["# clients", "", "Acme Robotics", "re:proj(ect)?-\\d+"])
    assert len(terms) == 2
    words = line_words("call", "ACME", "robotics", "about", "PROJ-42")
    hits = rd.find_sensitive(words, terms=terms)
    assert [w for _, w in hits] == ["term", "term"]
    assert hits[0][0] == rd.union(words[1].box, words[2].box)
    assert rd.find_sensitive(line_words("acmerobotics"), terms=terms) == []  # whole words only


def test_all_text_mode_boxes_each_readable_line():
    words = line_words("Quarterly", "plan") + line_words("x", y=300, line=(1, 1, 2)) \
        + line_words("noise", y=500, line=(2, 1, 1), conf=12)
    hits = rd.find_sensitive(words, all_text=True)
    assert hits == [(rd.union(words[0].box, words[1].box), "text")]  # one-letter and low-confidence lines skipped


def test_refine_adds_samples_only_where_the_screen_moved():
    still, moved = bytes(100), bytes([200] * 100)
    picture = lambda t: moved if t >= 2.3 else still  # the screen scrolls at 2.3 s
    times, thumbs = rd.refine([0.0, 1.0, 2.0, 3.0, 4.0], [picture(t) for t in (0, 1, 2, 3, 4)],
                              lambda ts: [picture(t) for t in ts], every=1.0, step=0.04, budget=50)
    extra = [t for t in times if t not in (0, 1, 2, 3, 4)]
    assert extra and all(2.0 < t < 3.0 for t in extra)  # only between the samples either side of the change
    edge = [b - a for a, b in zip(times, times[1:]) if picture(a) != picture(b)]
    assert edge == [pytest.approx(1 / 32)]  # halved down to under 1.5 frames
    capped, _ = rd.refine([0.0, 1.0], [still, moved], lambda ts: [moved for _ in ts], every=1.0, step=0.001, budget=3)
    assert len(capped) == 5  # the budget holds


def test_stretches_and_readings():
    a, b = bytes(100), bytes([200] * 100)
    times = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 10.0]
    thumbs = [a, a, a, a, b, b, b, b]
    runs = rd.stretches(times, thumbs, every=1.0)
    assert runs == [(0, 3), (4, 6), (7, 7)]  # a new screen at 4 s; 10 s is a new span
    assert rd.readings((0, 3), times) == [0, 3] and rd.readings((7, 7), times) == [7]
    long = [float(t) for t in range(10)]
    assert rd.readings((0, 9), long) == [0, 3, 6, 9]


def test_detect_blurs_a_whole_still_run_with_what_any_reading_found(monkeypatch, tmp_path):
    """OCR reads the name at 2 s only; the blur still covers the run, 0-1 s before to after it."""
    monkeypatch.setattr(rd, "resolve_ocr", lambda ocr: "tesseract")
    monkeypatch.setattr(rd.shutil, "which", lambda name: "/bin/tesseract")
    still = bytes(THUMB_SIZE)

    def fake_run(args, **kw):
        if "-f" in args and args[args.index("-f") + 1] == "rawvideo" and "-filter_complex" not in args:
            Path(args[-1]).write_bytes(still)  # thumb_args
        elif args[0] == "ffmpeg":
            for a in args:
                if a.endswith(".png"):
                    Path(a).write_bytes(b"x")
        else:  # tesseract: the name is read at 3 s only
            k = int(Path(args[1]).name[1:6])  # the sample's index: 0, 1, 2, 3 s and 3.95 s
            tsv = TSV_HEADER + (TSV_ROW.format(text="Robin") if k == 3 else "")
            return subprocess.CompletedProcess(args, 0, tsv, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rd.subprocess, "run", fake_run)
    rs = rd.detect("x.mp4", [(0.0, 4.0)], info=rd.VideoInfo(640, 360, 25.0), every=1.0,
                   terms=rd.compile_terms(["Robin"]))
    assert [r.why for r in rs] == ["term"] and rs[0].start == 0.0 and rs[0].end >= 4.0


def test_detect_fails_closed_without_tesseract(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(rd, "vision_available", lambda: False)
    with pytest.raises(RuntimeError, match="tesseract"):
        rd.detect("x.mp4", [(0, 1)], info=rd.VideoInfo(640, 360))


# ----------------------------------------------------------------- OCR engines and Vision tiles

def test_resolve_ocr_prefers_vision_and_fails_closed(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(rd, "vision_available", lambda: True)
    assert rd.resolve_ocr("auto") == "vision"
    assert rd.resolve_ocr("tesseract") == "tesseract"
    monkeypatch.setattr(rd, "vision_available", lambda: False)
    assert rd.resolve_ocr("auto") == "tesseract"
    with pytest.raises(RuntimeError, match="vision extra"):
        rd.resolve_ocr("vision")  # asked for Vision: never quietly falls back to tesseract
    with pytest.raises(ValueError, match="--ocr"):
        rd.resolve_ocr("easyocr")


def test_detect_fails_closed_when_vision_is_asked_for_but_missing(monkeypatch):
    monkeypatch.setattr(rd, "vision_available", lambda: False)
    with pytest.raises(RuntimeError, match="Vision"):
        rd.detect("x.mp4", [(0, 1)], info=rd.VideoInfo(640, 360), ocr="vision")


@pytest.mark.parametrize("width, height", [(1920, 1080), (1280, 720), (3840, 2160), (300, 200)])
def test_tiles_cover_the_frame_and_hold_every_short_word_whole(width, height):
    tiles = rd.tile_rects(width, height)
    for x, y, w, h in tiles:
        assert 0 <= x and 0 <= y and x + w <= width and y + h <= height
    # every 120 x 40 px box (a long word, a phone number) lies wholly inside at least one tile
    for bx in range(0, width - 120 + 1, 37):
        for by in range(0, height - 40 + 1, 23):
            assert any(x <= bx and y <= by and bx + 120 <= x + w and by + 40 <= y + h for x, y, w, h in tiles), (bx, by)


def test_tile_frame_args_write_one_upscaled_png_per_tile_and_the_thumbnail(tmp_path):
    tiles = [(0, 0, 420, 300), (300, 0, 420, 300)]
    pngs = [tmp_path / "a.png", tmp_path / "b.png"]
    args = rd.tile_frame_args("in.mp4", 12.5, tiles, pngs, tmp_path / "t.gray", scale=3)
    graph = args[args.index("-filter_complex") + 1]
    assert "split=3" in graph and "crop=420:300:300:0,scale=iw*3:ih*3" in graph
    assert args.count("-map") == 3 and str(pngs[1]) in args and args[-1] == str(tmp_path / "t.gray")


def test_vision_box_flips_to_top_left_source_pixels():
    # Vision: normalised, origin bottom-left. A box in the top-left quarter of tile (300, 200, 400, 300).
    assert rd.vision_box((0.0, 0.5, 0.5, 0.5), (300, 200, 400, 300)) == (300, 200, 200, 150)
    assert rd.vision_box((0.25, 0.0, 0.5, 0.1), (0, 0, 400, 300)) == (100, 270, 200, 30)


def test_split_line_gives_each_word_its_own_box():
    text = "fax 816-795-0144 now"
    spans = {}

    def box_for_range(start, length):
        spans[text[start:start + length]] = (start, length)
        return (start / len(text), 0.0, length / len(text), 1.0)  # a 1-line tile, x proportional to characters

    words = rd.split_line(text, 87.5, (2, 0, 0), (100, 50, 200, 10), box_for_range)
    assert [w.text for w in words] == ["fax", "816-795-0144", "now"]
    assert words[1].box == (100 + round(4 / 20 * 200), 50, round(12 / 20 * 200), 10)
    assert all(w.conf == 87.5 and w.line == (2, 0, 0) for w in words)
    # and the line-level patterns still find the number across the split words
    assert rd.find_sensitive(words) == [(words[1].box, "phone")]


def _vision_ready() -> bool:
    return _ffmpeg_present() and rd.vision_available()


def _draw_screen(png: Path, lines: list[tuple[str, int, int]], size: float = 9.0) -> None:
    """A white 1920x1080 'screen' with small black text, drawn with AppKit (no ffmpeg drawtext needed)."""
    import AppKit

    img = AppKit.NSImage.alloc().initWithSize_((1920, 1080))
    img.lockFocus()
    AppKit.NSColor.whiteColor().set()
    AppKit.NSRectFill(((0, 0), (1920, 1080)))
    attrs = {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(size),
             AppKit.NSForegroundColorAttributeName: AppKit.NSColor.blackColor()}
    for text, x, y in lines:  # y from the top, like the source pixels
        AppKit.NSString.stringWithString_(text).drawAtPoint_withAttributes_((x, 1080 - y - size), attrs)
    img.unlockFocus()
    rep = AppKit.NSBitmapImageRep.alloc().initWithData_(img.TIFFRepresentation())
    rep.setSize_((1920, 1080))
    png.write_bytes(bytes(rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})))


@pytest.mark.skipif(not _vision_ready(), reason="needs ffmpeg, macOS and the vision extra")
def test_vision_reads_small_shared_screen_text(tmp_path):
    """The Talk #3 case, made up: 9 px text on a 1080p 'screen', compressed like a Meet recording."""
    png, src = tmp_path / "screen.png", tmp_path / "screen.mp4"
    _draw_screen(png, [("Could you please fax them to 816-555-0144?", 410, 300),
                       ("Draft is in your pat@example.com folder", 1250, 760),
                       ("Pick the demo workflow for Robin and Sam", 700, 520)])
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-loop", "1", "-i", str(png), "-t", "2", "-r", "25",
                    "-vf", "scale=1920:1080", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", str(src)],
                   check=True)
    rs = rd.detect(src, [(0, 2)], info=rd.VideoInfo(1920, 1080), every=1.0, ocr="vision",
                   terms=rd.compile_terms(["Robin"]))
    kinds = {r.why for r in rs}
    assert {"phone", "email", "term"} <= kinds
    phone = next(r for r in rs if r.why == "phone")
    assert 410 < phone.box[0] < 700 and 290 < phone.box[1] < 320 and phone.box[2] < 160  # the number, not the line


# ----------------------------------------------------------------- with ffmpeg

def _make_source(tmp_path: Path) -> Path:
    """4 s of a moving test pattern, a tone and a mov_text caption track, like a Meet export."""
    srt = tmp_path / "c.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:03,500\n(Speaker) hello there\n\n", encoding="utf-8")
    src = tmp_path / "talk.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=4", "-f", "lavfi", "-i", "sine=f=440:d=4", "-i", str(srt),
        "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-c:s", "mov_text", "-t", "4", str(src),  # -t, not -shortest: ffmpeg 9 never ends with an srt input
    ], check=True)
    return src


def _frame(path: Path, t: float) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"], check=True, capture_output=True,
    ).stdout


def _region_diff(a: bytes, b: bytes, box, width: int = 320) -> float:
    x, y, w, h = box
    total = sum(abs(a[r * width + c] - b[r * width + c]) for r in range(y, y + h) for c in range(x, x + w))
    return total / (w * h)


@needs_ffmpeg
def test_run_blurs_the_box_in_its_range_keeps_streams_and_reuses_the_copy(tmp_path):
    src = _make_source(tmp_path)
    spec = tmp_path / "boxes.json"
    spec.write_text(json.dumps([{"start": 1, "end": 2, "box": [0, 0, 160, 120], "why": "screen share"}]))
    logs: list[str] = []
    copy, rs = rd.run(src, str(spec), [(0, 4)], tmp_path / "out", duration=4.0, log=logs.append)
    assert copy == tmp_path / "out" / "talk.redacted.mp4" and len(rs) == 1
    assert json.loads((tmp_path / "out" / "redactions.json").read_text())[0]["why"] == "screen share"

    inside, outside = (0, 0, 160, 120), (160, 120, 160, 120)
    orig, red = _frame(src, 1.5), _frame(copy, 1.5)
    assert _region_diff(orig, red, inside) > 10  # blurred
    assert _region_diff(orig, red, outside) < 3  # untouched apart from re-encoding
    assert _region_diff(_frame(src, 3), _frame(copy, 3), inside) < 3  # out of range: untouched

    def probe(path):
        return json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(path)],
            check=True, capture_output=True, text=True).stdout)

    assert sorted(s["codec_type"] for s in probe(copy)["streams"]) == ["audio", "subtitle", "video"]
    assert abs(float(probe(copy)["format"]["duration"]) - float(probe(src)["format"]["duration"])) < 0.1

    assert rd.apply(src, rs, copy, log=logs.append) is False  # same source, same boxes: no second encode
    assert any("reusing" in m for m in logs)
    assert rd.apply(src, [Redaction(0, 1, (0, 0, 40, 40))], copy) is True


@needs_ffmpeg
@pytest.mark.parametrize("container", ["ts", "mkv"])
def test_run_times_the_blur_on_sources_that_do_not_start_at_zero_and_keeps_srt_captions(tmp_path, container):
    """An MPEG-TS starts at ~1.4 s: the blur must still land on 1-2 s as -ss and the renderer
    count it. An MKV carries subrip captions, which mp4 cannot hold as is."""
    srt = tmp_path / "c.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:03,000\nhello there\n\n", encoding="utf-8")
    src = tmp_path / f"talk.{container}"
    subs = ["-i", str(srt), "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:s", "srt"] if container == "mkv" else []
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=4", "-f", "lavfi", "-i", "sine=f=440:d=4", *subs,
        "-c:v", "libx264", "-g", "5", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src),
    ], check=True)  # -g 5: a seek into a TS needs a keyframe nearby
    copy = tmp_path / "out.mp4"
    rd.apply(src, [Redaction(1, 2, (0, 0, 160, 120))], copy)
    box = (0, 0, 160, 120)
    for t, blurred in ((0.5, False), (1.2, True), (1.8, True), (2.6, False)):
        diff = _region_diff(_frame(src, t), _frame(copy, t), box)
        assert (diff > 10) is blurred, (t, diff)
    if container == "mkv":
        kinds = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                                str(copy)], check=True, capture_output=True, text=True).stdout.split()
        assert "mov_text" in kinds


@needs_ffmpeg
def test_cli_redact_refuses_a_plan_for_another_recording(tmp_path, capsys):
    src = _make_source(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "version": "1", "source": {"path": (tmp_path / "other.mp4").as_posix(), "captions": {"kind": "embedded"}},
        "output": {"dir": tmp_path.as_posix(), "preset": "internal", "aspect": "16:9", "captions": "burn_in"},
        "clips": [{"id": "clip-01-x", "takeaway": "x", "segments": [{"start": 0.5, "end": 3.0}]}],
    }))
    assert cli.main(["redact", "--source", str(src), "--redact", "auto", "--plan", str(plan_path)]) == 1
    assert "is a plan for" in capsys.readouterr().err


@needs_ffmpeg
def test_cli_redact_points_the_plan_at_the_copy(tmp_path, capsys):
    src = _make_source(tmp_path)
    plan = {
        "version": "1",
        "source": {"path": src.as_posix(), "duration_seconds": 4.0, "captions": {"kind": "embedded"}},
        "output": {"dir": (tmp_path / "out").as_posix(), "preset": "internal", "aspect": "16:9", "captions": "burn_in"},
        "clips": [{"id": "clip-01-x", "takeaway": "x", "segments": [{"start": 0.5, "end": 3.0}], "trim_silence": True}],
    }
    plan_path = tmp_path / "out" / "plan.json"
    plan_path.parent.mkdir()
    plan_path.write_text(json.dumps(plan))
    spec = tmp_path / "boxes.json"
    spec.write_text(json.dumps([{"start": 0, "end": 4, "box": [0, 0, 100, 100]}]))
    assert cli.main(["redact", "--source", str(src), "--redact", str(spec), "--plan", str(plan_path)]) == 0
    out = capsys.readouterr().out
    assert "plan updated" in out and "0,0 100x100\tmanual" in out
    assert json.loads(plan_path.read_text())["source"]["path"].endswith("out/talk.redacted.mp4")


@pytest.mark.skipif(not (_ffmpeg_present() and shutil.which("tesseract")), reason="needs ffmpeg and tesseract")
def test_detect_reads_a_made_up_screen_and_the_copy_hides_it(tmp_path):
    """End to end on a white 'screen' that shows an address and a key part way through."""
    font = next((p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Helvetica.ttc",
                             "C:/Windows/Fonts/arial.ttf") if Path(p).is_file()), None)
    if font is None:
        pytest.skip("no known font for drawtext")
    src = tmp_path / "screen.mp4"
    draw = (f"drawtext=fontfile='{font}':text='Weekly notes':x=60:y=80:fontsize=28:fontcolor=black,"
            f"drawtext=fontfile='{font}':text='Mail pat@example.com':x=60:y=200:fontsize=16:fontcolor=black:"
            "enable='between(t,2,4)'")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=white:s=960x540:r=25:d=6",
                    "-vf", draw, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    copy, rs = rd.run(src, "auto", [(0, 6)], tmp_path / "out", every=1.0)
    assert [r.why for r in rs] == ["email"]
    assert rs[0].start <= 2 and rs[0].end >= 4
    frame = tmp_path / "f.png"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "3", "-i", str(copy), "-frames:v", "1",
                    "-vf", "scale=iw*2:ih*2", str(frame)], check=True)
    seen = subprocess.run(["tesseract", str(frame), "stdout", "--psm", "11"], capture_output=True, text=True).stdout
    assert "example" not in seen and "Weekly" in seen


DEMO_MP4 = Path(__file__).resolve().parents[2] / "assets" / "demo-clip.mp4"


@pytest.mark.skipif(not (DEMO_MP4.exists() and _ffmpeg_present()), reason="needs ffmpeg + assets/demo-clip.mp4")
def test_cli_reel_redact_reads_only_the_kept_segments_and_names_the_copy(tmp_path, monkeypatch):
    seen = {}

    def fake_run(source, mode, spans, out_dir, **kw):
        seen.update(source=source, mode=mode, spans=spans, out_dir=Path(out_dir))
        copy = Path(out_dir) / "demo-clip.redacted.mp4"
        return copy, [Redaction(0, 1, (0, 0, 20, 20))]

    monkeypatch.setattr(rd, "run", fake_run)
    out = tmp_path / "plan.json"
    assert cli.main(["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--redact", "auto", "--out", str(out)]) == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["source"]["path"].endswith("demo-clip.redacted.mp4")
    assert seen["mode"] == "auto" and seen["out_dir"] == tmp_path
    kept = sorted((s["start"], s["end"]) for c in plan["clips"] for s in c["segments"])
    assert sorted((round(a, 3), round(b, 3)) for a, b in seen["spans"]) == kept  # OCR only what the reel shows


@pytest.mark.skipif(not _vision_ready(), reason="needs ffmpeg, macOS and the vision extra")
def test_check_redaction_finds_what_is_readable_and_passes_once_it_is_blurred(tmp_path, capsys):
    """End to end: a screen with a phone, an e-mail and a name; the check lists them,
    `run` blurs them, and the check on the copy comes back clean."""
    png, src = tmp_path / "screen.png", tmp_path / "screen.mp4"
    _draw_screen(png, [("Call 816-555-0144 about the invoice", 300, 300),
                       ("Draft is in your pat@example.com folder", 900, 600),
                       ("Weekly notes for Robin", 300, 800)], size=11.0)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-loop", "1", "-i", str(png), "-t", "3", "-r", "25",
                    "-vf", "scale=1920:1080", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    terms = tmp_path / "names.txt"
    terms.write_text("Robin\n")
    assert cli.main(["check-redaction", "--video", str(src), "--terms", str(terms), "--every", "1"]) == 1
    cap = capsys.readouterr()
    listed = cap.out
    assert "816-555-0144" in listed, cap.err and "pat@example.com" in listed and "Robin" in listed
    copy, rs = rd.run(src, "auto", [(0, 3)], tmp_path / "out", terms_file=str(terms), every=1.0, ocr="vision")
    assert {r.why for r in rs} >= {"phone", "email", "term"}
    assert cli.main(["check-redaction", "--video", str(copy), "--terms", str(terms), "--every", "1"]) == 0
    assert "nothing private readable" in capsys.readouterr().out


def test_readings_reread_a_sample_whose_picture_changed_at_all():
    a, b = bytes(100), bytes([0] * 99 + [40])  # one pixel changed: a toast, a typed address
    times = [0.0, 1.0, 2.0]
    assert rd.readings((0, 2), times, [a, a, b]) == [0, 2]
    assert rd.readings((0, 2), times, [a, b, b]) == [0, 1, 2]


def test_leak_check_fails_closed_when_tesseract_fails(monkeypatch):
    monkeypatch.setattr(rd, "resolve_ocr", lambda ocr: "tesseract")
    monkeypatch.setattr(rd.shutil, "which", lambda name: "/bin/tesseract")
    monkeypatch.setattr(rd, "video_info", lambda v: rd.VideoInfo(64, 48, 25.0, 1.0))

    def fake_run(args, **kw):
        if args[0] == "ffmpeg":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "tesseract crashed")

    monkeypatch.setattr(rd.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="tesseract failed"):
        rd.leak_check("x.mp4", every=0.5)


def test_check_and_patch_blurs_what_reads_back_and_stops_when_clean(monkeypatch, tmp_path):
    rounds = []

    def fake_check(copy, *, boxes, every, spans, **kw):
        if every < 0.1:  # the frame-by-frame look around a hit: the word one frame earlier, elsewhere
            assert spans == [(2.0, 4.0)]
            boxes.append((2.48, (100, 90, 60, 12), "term"))
            return 50, [(2.48, "term", "Robin")]
        rounds.append(1)
        if len(rounds) == 1:
            boxes.append((3.0, (100, 100, 60, 12), "term"))
            return 10, [(3.0, "term", "Robin")]
        return 10, []

    applied = []
    monkeypatch.setattr(rd, "leak_check", fake_check)
    monkeypatch.setattr(rd, "apply", lambda src, rs, out, log=None: applied.append(list(rs)))
    rs = rd.check_and_patch("s.mp4", tmp_path / "c.mp4", [Redaction(0, 1, (0, 0, 20, 20), "email")], [(0, 10)],
                            info=rd.VideoInfo(640, 360, 25.0), terms=[], ocr="vision")
    assert len(rounds) == 2 and len(applied) == 1
    assert any(r.start <= 2.0 and r.end >= 4.0 and r.box[1] < 100 for r in rs)  # the hit, widened to t ± 1 s
    assert any(r.start <= 2.48 <= r.end and r.box[1] < 90 for r in rs)  # the frame-by-frame sighting, where it was

    rounds.clear()
    monkeypatch.setattr(rd, "leak_check", lambda copy, *, boxes, **kw: (10, [(1.0, "phone", "816-555-0144")]))
    (tmp_path / "c.mp4").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="still readable"):
        rd.check_and_patch("s.mp4", tmp_path / "c.mp4", [], [(0, 10)], info=rd.VideoInfo(640, 360, 25.0),
                           terms=[], ocr="vision", rounds=1)
    assert not (tmp_path / "c.mp4").exists() and (tmp_path / "c.unsafe.mp4").exists()


def test_vision_frame_recycles_workers_and_retries_a_failed_frame_once(monkeypatch):
    made = []

    class FakeWorker:
        def __init__(self):
            made.append(self)
            self.done = 0
            self.proc = type("P", (), {"poll": lambda self: None})()

        def read(self, pngs, tiles):
            if len(made) == 1 and self.done == 2:
                raise RuntimeError("redact: Apple Vision failed on a frame: imageOperationFailed")
            self.done += 1
            return [Word("x", (0, 0, 1, 1), 90.0, (0, 0, 0))]

        def close(self):
            pass

    monkeypatch.setattr(rd, "_VisionWorker", FakeWorker)
    monkeypatch.setattr(rd, "VISION_TASKS_PER_PROCESS", 3)
    monkeypatch.setattr(rd._workers, "vision", None, raising=False)
    for _ in range(6):
        assert rd.vision_frame([Path("a.png")], [(0, 0, 10, 10)])
    # worker 1 failed on its 3rd frame -> a fresh worker read it; that one is replaced after 3 frames
    assert len(made) == 3 and [w.done for w in made] == [2, 3, 1]
