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


def test_blur_radius_respects_boxblur_limits():
    assert rd.blur_radius((0, 0, 1440, 620)) == (rd.MAX_RADIUS, rd.MAX_RADIUS // 2)
    lr, cr = rd.blur_radius((0, 0, 200, 16))
    assert lr <= 16 // 2 and cr <= 16 // 4 and lr >= 1 and cr >= 1


# ----------------------------------------------------------------- redactions.json

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

def test_filter_graph_blurs_each_box_only_in_its_range_shifted_by_start_time():
    g = rd.filter_graph([Redaction(1, 2, (0, 230, 1440, 620)), Redaction(5, 6.5, (100, 100, 40, 20))], start_time=0.5)
    assert g.startswith("[0:v]split=3[base][c0][c1];")
    assert "[c0]crop=1440:620:0:230,boxblur=luma_radius=40:luma_power=3:chroma_radius=20:chroma_power=3[b0]" in g
    assert "[base][b0]overlay=0:230:enable='between(t,1.500,2.500)'[v0]" in g
    assert "[v0][b1]overlay=100:100:enable='between(t,5.500,7.000)'[vout]" in g
    assert rd.filter_graph([]) == "[0:v]null[vout]"


def test_apply_args_copy_audio_and_captions_and_tag_the_file():
    args = rd.apply_args("in file.mp4", [Redaction(0, 1, (0, 0, 20, 20))], "out.mp4", tag="abc")
    assert args[0] == "ffmpeg" and "in file.mp4" in args and args[-1] == "out.mp4"
    assert args[args.index("-c:a") + 1] == "copy" and args[args.index("-c:s") + 1] == "copy"
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
    ("Zx9Qp2Lm7Rt4Vb8Nc1Kd6Hf3", "token"),
])
def test_find_sensitive_kinds(text, why):
    assert why in [w for _, w in rd.find_sensitive(line_words("value:", text))]


@pytest.mark.parametrize("text", [
    "4111-1111-1111-1112",  # fails the Luhn check: a part number, not a card
    "Pipeline", "2026-10-03", "github.com/kyletabor/video-editor-bot", "transcription_pipeline_overview",
])
def test_find_sensitive_leaves_ordinary_text(text):
    assert rd.find_sensitive(line_words("see", text)) == []


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


def test_detect_fails_closed_without_tesseract(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="tesseract"):
        rd.detect("x.mp4", [(0, 1)], info=rd.VideoInfo(640, 360, 0.0))


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
        "-c:s", "mov_text", "-shortest", str(src),
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
