"""Blur private information on screen: a redacted copy of the source.

A shared screen in a recorded talk can show an inbox, a calendar, a client list
or a key clearly enough to read. The first time it happened the fix was done by
hand: an ffmpeg `boxblur` over the screen-share rectangle during set time
ranges, written to a copy of the recording, the original left alone. This
module makes that a clipbot step.

Where it sits: bot/ never cuts a frame and the edit plan is the only thing that
crosses to render/ (README, *How it works*). Redaction keeps both rules: it
writes a redacted copy of the *source*, same length, same timestamps, audio and
caption streams copied untouched, and the plan's `source.path` points at that
copy. The renderer and the contract do not change, and a plan that names the
original is never silently "redacted".

A redaction is a box and a time range (`Redaction`). They come from:

- a hand-written `redactions.json` (`load_redactions`), the same shape this
  module writes: `[{"start": "12:30", "end": "13:05", "box": [x, y, w, h],
  "why": "screen share"}]`, `start`/`end` in seconds or h:mm:ss, the box in
  source pixels;
- `detect` (`--redact auto` / `--redact text`): frames sampled every `every`
  seconds inside the given spans, read with OCR, and every word that
  matches a sensitive pattern (`SENSITIVE`: e-mail addresses, phone numbers,
  card numbers that pass the Luhn check, SSNs, API keys and long random-looking
  tokens) or a caller's term (`--redact-terms FILE`: names, a client, a
  project) is boxed. `text` boxes every line of text instead: the automatic
  version of blurring the whole screen.

Two OCR engines (`--ocr`): Apple Vision on a Mac (`vision`, the `vision`
extra: pyobjc-framework-Vision) and tesseract everywhere else (`tesseract`, a
separate install: `brew install tesseract`, `apt install tesseract-ocr`).
`auto` picks Vision when it can be loaded. A shared screen in a 1080p Meet
recording often carries 7-10 px text; tesseract read almost none of it on a
real talk (a doctor's name, phone numbers and e-mail addresses all missed), so
Vision reads each frame as overlapping tiles (`VISION_TILE`, `VISION_STEP`),
each upscaled `VISION_SCALE` times: whole-frame Vision downsamples the image and
loses the small text too.

A word seen at sample t was not seen at t - every, so each detection is blurred
from the sample before it to the sample after it (`every` on each side) and
consecutive detections of the same box are merged into one range. A frame that
did not change since the previous sample (a static slide, a paused screen) is
not read again: its detections carry over.

Privacy fails closed: missing tesseract or an ffmpeg error stops the run, it
never falls back to an unredacted reel. OCR misses things (tiny or low-contrast
text, handwriting, images of text), so the presenter's review of the reel is
still the last check; `redactions.json` is written next to the plan so a
missed spot can be added by hand and the run repeated.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from .speakers import parse_clock

EVERY_SECONDS = 1.0  # sample spacing inside a reel's moments
OCR_SCALE = 2  # tesseract reads screen text best at ~30 px; Meet screen shares are ~12-16 px
VISION_TILE = (420, 300)  # source pixels per Vision tile: small enough that Vision keeps 7 px text
VISION_STEP = (300, 200)  # tile spacing: the overlap holds any word up to 120 px wide whole in some tile
VISION_SCALE = 3  # each tile is upscaled before Vision reads it
OCR_ENGINES = ("auto", "vision", "tesseract")
MIN_CONFIDENCE = 0  # tesseract's word confidence (0-100; -1 = not a word)
TEXT_MODE_CONFIDENCE = 40  # `text` mode skips the noise OCR reports on faces and backgrounds
PAD_PIXELS = 6  # around every OCR box: glyph tops, descenders and anti-aliasing
MIN_BOX = 16  # boxblur needs room for its radius; nothing smaller hides a glyph
THUMB = (192, 108)  # grey thumbnail compared between samples
SAME_FRAME_DIFF = 8  # largest per-pixel |Δ| on it for two samples to count as the same picture
MAX_RADIUS = 40
WORKERS = 4
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
TAG = "clipbot-redact"
VERSION = 2  # bump when the copy a given source + boxes produce changes (cached copies are then rewritten)

# One regex per kind, run over each OCR line (words joined by single spaces).
SENSITIVE: dict[str, re.Pattern[str]] = {
    # OCR often reads "a@b.com" as "a @ b.com"
    "email": re.compile(r"[\w.+-]+ ?@ ?[\w-]+(?:\.[\w-]+)+"),
    # North American with separators or 10 bare digits (a country code needs a separator after it, so a
    # 13-digit order number is not a phone), and any "+" international number of 7-15 digits.
    "phone": re.compile(
        r"(?<![\w.])(?:(?:\+?1[\s.-])?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}"
        r"|\+\d{1,3}(?:[\s.-]?\(?\d{1,5}\)?){2,5})(?![\w])"
    ),
    "ssn": re.compile(r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)"),
    "card": re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"),
    "key": re.compile(
        r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|xox[abprs]-[\w-]{10,}"
        r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]*)"
    ),
    # A long unbroken run of letters and digits with both in it: a token, a password, a hash.
    "token": re.compile(r"(?<![\w/])(?=[A-Za-z0-9_\-+/=]*\d)(?=[A-Za-z0-9_\-+/=]*[A-Za-z])[A-Za-z0-9_\-+/=]{24,}(?![\w/])"),
}


@dataclass(frozen=True)
class Redaction:
    start: float
    end: float
    box: tuple[int, int, int, int]  # x, y, w, h in source pixels
    why: str = ""

    def spec(self) -> dict:
        """The redactions.json record: what `load_redactions` reads back."""
        return {"start": round(self.start, 2), "end": round(self.end, 2), "box": list(self.box), "why": self.why}


@dataclass(frozen=True)
class Word:
    """One OCR word in source pixels."""
    text: str
    box: tuple[int, int, int, int]
    conf: float
    line: tuple[int, int, int]  # (block, paragraph, line): tesseract's grouping


# ----------------------------------------------------------------- boxes and ranges

def clamp_box(box, width: int, height: int, *, pad: int = 0) -> tuple[int, int, int, int] | None:
    """Pad, grow to MIN_BOX, snap to even pixels (yuv420p chroma) and keep inside
    the frame. None when nothing of it is on screen."""
    x, y, w, h = (int(round(float(v))) for v in box)
    if w <= 0 or h <= 0:
        return None
    x0, y0, x1, y1 = x - pad, y - pad, x + w + pad, y + h + pad
    if x1 - x0 < MIN_BOX:
        grow = MIN_BOX - (x1 - x0)
        x0 -= grow // 2
        x1 += grow - grow // 2
    if y1 - y0 < MIN_BOX:
        grow = MIN_BOX - (y1 - y0)
        y0 -= grow // 2
        y1 += grow - grow // 2
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    x0 -= x0 % 2
    y0 -= y0 % 2
    x1 -= x1 % 2
    y1 -= y1 % 2
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def union(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
    x1, y1 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0)


def overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection over the smaller box: 1.0 when one holds the other."""
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    small = min(a[2] * a[3], b[2] * b[3])
    return (ix * iy) / small if small else 0.0


def merge(redactions: list[Redaction], *, same_box: float = 0.5) -> list[Redaction]:
    """Join redactions whose time ranges touch and whose boxes mostly overlap
    (a word OCR boxes a pixel differently from one sample to the next). The
    merged box is the union, the range the union of the ranges."""
    out: list[Redaction] = []
    for r in sorted(redactions, key=lambda r: (r.start, r.box)):
        for k, o in enumerate(out):
            if r.start <= o.end + 1e-6 and overlap(r.box, o.box) >= same_box:
                why = o.why if r.why in o.why.split(", ") else f"{o.why}, {r.why}".strip(", ")
                out[k] = Redaction(o.start, max(o.end, r.end), union(o.box, r.box), why)
                break
        else:
            out.append(r)
    return sorted(out, key=lambda r: (r.start, r.box))


# ----------------------------------------------------------------- redactions.json

def load_redactions(path: str | Path, width: int, height: int, duration: float | None = None) -> list[Redaction]:
    """Read a redactions.json (module docstring). Errors name the record."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of {{start, end, box, why}}")
    out: list[Redaction] = []
    for k, rec in enumerate(data, 1):
        where = f"{path}: redaction {k}"
        try:
            start, end = parse_clock(rec["start"]), parse_clock(rec["end"])
            box = rec["box"]
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"{where}: needs start, end (seconds or h:mm:ss) and box [x, y, w, h] ({e})") from e
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
            raise ValueError(f"{where}: box must be [x, y, w, h] in source pixels")
        if end <= start:
            raise ValueError(f"{where}: end must be after start")
        clamped = clamp_box(box, width, height)
        if clamped is None:
            raise ValueError(f"{where}: box {box} lies outside the {width}x{height} frame")
        if duration is not None:
            start, end = max(0.0, start), min(duration, end)
        out.append(Redaction(start, end, clamped, str(rec.get("why", "")).strip()))
    return out


def write_redactions(path: str | Path, redactions: list[Redaction]) -> None:
    Path(path).write_text(json.dumps([r.spec() for r in redactions], indent=2) + "\n", encoding="utf-8")


# ----------------------------------------------------------------- applying: ffmpeg

def blur_radius(box: tuple[int, int, int, int]) -> tuple[int, int]:
    """(luma, chroma) boxblur radii: as strong as the box allows. boxblur
    refuses a radius above half the plane's smaller side, and the chroma planes
    of yuv420p are half size."""
    side = min(box[2], box[3])
    return max(1, min(MAX_RADIUS, side // 2 - 1)), max(1, min(MAX_RADIUS // 2, side // 4 - 1))


def filter_graph(redactions: list[Redaction]) -> str:
    """One crop + boxblur + overlay per redaction, enabled only in its range.
    Without -copyts ffmpeg shifts the input so its first frame is t = 0, the same
    clock `-ss` and the renderer use, so the ranges go in as written (a .ts
    source starting at 1.46 s was blurred 1.46 s late when they were shifted)."""
    if not redactions:
        return "[0:v]null[vout]"
    n = len(redactions)
    parts = [f"[0:v]split={n + 1}[base]" + "".join(f"[c{k}]" for k in range(n))]
    prev = "base"
    for k, r in enumerate(redactions):
        x, y, w, h = r.box
        lr, cr = blur_radius(r.box)
        a, b = r.start, r.end
        out = "vout" if k == n - 1 else f"v{k}"
        parts.append(f"[c{k}]crop={w}:{h}:{x}:{y},boxblur=luma_radius={lr}:luma_power=3:chroma_radius={cr}:chroma_power=3[b{k}]")
        parts.append(f"[{prev}][b{k}]overlay={x}:{y}:enable='between(t,{a:.3f},{b:.3f})'[{out}]")
        prev = out
    return ";".join(parts)


def fingerprint(source: str | Path, redactions: list[Redaction]) -> str:
    """Identifies one (source, redactions) pair; stored in the copy's metadata so
    an unchanged re-run reuses the copy instead of encoding an hour again."""
    st = Path(source).stat()
    blob = json.dumps([VERSION, ENCODE, str(Path(source).resolve()), st.st_size, int(st.st_mtime),
                       [r.spec() for r in redactions]])
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _stored_fingerprint(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=comment", "-of", "json", str(path)],
            check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    comment = json.loads(out).get("format", {}).get("tags", {}).get("comment", "")
    return comment[len(TAG) + 1:] if comment.startswith(TAG + ":") else None


def apply_args(source: str | Path, redactions: list[Redaction], out: str | Path, *, tag: str = "") -> list[str]:
    """ffmpeg argv for the redacted copy: video re-encoded through the blur
    graph, audio copied as is, text captions converted to mov_text (the one
    caption codec mp4 holds: an MKV's subrip cannot be copied in) so the renderer
    still reads them from the copy, container metadata kept."""
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(source),
        "-filter_complex", filter_graph(redactions),
        "-map", "[vout]", "-map", "0:a?", "-map", "0:s?",
        *ENCODE, "-c:a", "copy", "-c:s", "mov_text",
        "-map_metadata", "0", "-metadata", f"comment={TAG}:{tag}", "-movflags", "+faststart",
        str(out),
    ]


def apply(source: str | Path, redactions: list[Redaction], out: str | Path, *, log=lambda msg: None) -> bool:
    """Write the redacted copy. False when an identical copy is already there."""
    out = Path(out)
    tag = fingerprint(source, redactions)
    if out.is_file() and _stored_fingerprint(out) == tag:
        log(f"redact: reusing {out} (same source and redactions)")
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".partial" + out.suffix)
    log(f"redact: writing {out} ({len(redactions)} blurred region(s)); the whole video is re-encoded, "
        "about a quarter of its length on a laptop")
    try:
        proc = subprocess.run(apply_args(source, redactions, tmp, tag=tag),
                              capture_output=True, text=True)
    except FileNotFoundError as e:
        raise RuntimeError("ffmpeg not found on PATH") from e
    if proc.returncode:
        tmp.unlink(missing_ok=True)
        lines = proc.stderr.strip().splitlines()
        cause = [ln for ln in lines if "rror" in ln or "not find" in ln or "nvalid" in ln][:2] or lines[-1:] or ["no output"]
        raise RuntimeError(f"redact: ffmpeg failed: {' / '.join(cause)}")
    tmp.replace(out)
    return True


# ----------------------------------------------------------------- the source's picture

@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int


def video_info(source: str | Path) -> VideoInfo:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "json", str(source)], check=True, capture_output=True, text=True,
        ).stdout
    except FileNotFoundError as e:
        raise RuntimeError("ffprobe not found on PATH") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffprobe failed: {e.stderr.strip()}") from e
    data = json.loads(out)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"{source}: no video stream to redact")
    return VideoInfo(int(streams[0]["width"]), int(streams[0]["height"]))


# ----------------------------------------------------------------- detection: sample + OCR

def sample_times(spans: list[tuple[float, float]], every: float = EVERY_SECONDS) -> list[float]:
    """Sample instants covering every span: its start, every `every` seconds, and
    its end, deduplicated across touching spans."""
    times: set[float] = set()
    for a, b in spans:
        if b <= a:
            continue
        t = a
        while t < b:
            times.add(round(t, 2))
            t += every
        times.add(round(max(a, b - 0.05), 2))
    return sorted(times)


def frame_args(source: str | Path, t: float, png: Path, thumb: Path, scale: int = OCR_SCALE) -> list[str]:
    """One frame at t: upscaled PNG for OCR, a small grey thumbnail to compare samples."""
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-ss", f"{max(t, 0):.3f}", "-i", str(source),
        "-filter_complex", f"[0:v]split[a][b];[a]scale=iw*{scale}:ih*{scale}:flags=lanczos,format=gray[o];"
                           f"[b]scale={THUMB[0]}:{THUMB[1]}:flags=area,format=gray[s]",
        "-map", "[o]", "-frames:v", "1", str(png),
        "-map", "[s]", "-frames:v", "1", "-f", "rawvideo", str(thumb),
    ]


def parse_tsv(tsv: str, scale: float = 1.0) -> list[Word]:
    """Words from `tesseract ... tsv`, boxes divided by the OCR upscale."""
    words: list[Word] = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if row.get("level") != "5":
            continue
        text = (row.get("text") or "").strip()
        try:
            conf = float(row.get("conf") or -1)
            box = tuple(int(round(int(row[k]) / scale)) for k in ("left", "top", "width", "height"))
            line = (int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
        except (KeyError, TypeError, ValueError):
            continue
        if text:
            words.append(Word(text, box, conf, line))  # type: ignore[arg-type]
    return words


def luhn(digits: str) -> bool:
    d = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(d) <= 19:
        return False
    total = 0
    for k, v in enumerate(reversed(d)):
        if k % 2:
            v *= 2
            if v > 9:
                v -= 9
        total += v
    return total % 10 == 0


def compile_terms(lines: list[str]) -> list[re.Pattern[str]]:
    """--redact-terms: one term per line, matched case-insensitively on word
    boundaries; `re:` starts a regular expression; `#` lines are comments."""
    out = []
    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("re:"):
            out.append(re.compile(s[3:], re.I))
        else:
            out.append(re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, s.split())) + r"(?!\w)", re.I))
    return out


def find_sensitive(words: list[Word], *, terms: list[re.Pattern[str]] = (), all_text: bool = False
                   ) -> list[tuple[tuple[int, int, int, int], str]]:
    """(box, why) for every match. Patterns run over whole OCR lines so a phone
    number or a name split into several words is caught; the box is the union
    of the words the match touches."""
    lines: dict[tuple[int, int, int], list[Word]] = {}
    for w in words:
        if w.conf >= MIN_CONFIDENCE:
            lines.setdefault(w.line, []).append(w)
    found: list[tuple[tuple[int, int, int, int], str]] = []
    for ws in lines.values():
        if all_text:
            readable = [w for w in ws if w.conf >= TEXT_MODE_CONFIDENCE and len(w.text) >= 2]
            if readable:
                box = readable[0].box
                for w in readable[1:]:
                    box = union(box, w.box)
                found.append((box, "text"))
            continue
        text, spans, pos = "", [], 0
        for w in ws:
            if text:
                text += " "
                pos += 1
            spans.append((pos, pos + len(w.text), w))
            text += w.text
            pos += len(w.text)
        checks = [(k, p) for k, p in SENSITIVE.items()] + [("term", p) for p in terms]
        for why, pat in checks:
            for m in pat.finditer(text):
                if why == "card" and not luhn(m.group()):
                    continue
                hit = [w for a, b, w in spans if a < m.end() and b > m.start()]
                if not hit:
                    continue
                box = hit[0].box
                for w in hit[1:]:
                    box = union(box, w.box)
                found.append((box, why))
    return found


# ----------------------------------------------------------------- Apple Vision OCR

def vision_available() -> bool:
    """Apple Vision can be used: a Mac with the `vision` extra (pyobjc-framework-Vision)."""
    return sys.platform == "darwin" and importlib.util.find_spec("Vision") is not None


def resolve_ocr(ocr: str) -> str:
    """`auto` -> `vision` when it can be loaded, else `tesseract`. Fails closed: the
    engine asked for (or the only one left) must be there."""
    if ocr not in OCR_ENGINES:
        raise ValueError(f"redact: --ocr must be one of {', '.join(OCR_ENGINES)}, not {ocr!r}")
    if ocr in ("auto", "vision") and vision_available():
        return "vision"
    if ocr == "vision":
        raise RuntimeError("redact: Apple Vision OCR needs macOS and the vision extra "
                           "(uv run --project bot --extra vision clipbot ...); or pass --ocr tesseract")
    if not shutil.which("tesseract"):
        raise RuntimeError("redact: tesseract (OCR) not found on PATH; install it (macOS: brew install tesseract, "
                           "or use Apple Vision: --extra vision; Ubuntu: sudo apt install tesseract-ocr) "
                           "or pass --redact FILE with boxes drawn by hand")
    return "tesseract"


def _starts(size: int, tile: int, step: int) -> list[int]:
    if size <= tile:
        return [0]
    out = list(range(0, size - tile, step))
    return out + [size - tile]  # the last tile sits flush with the far edge


def tile_rects(width: int, height: int, tile: tuple[int, int] = VISION_TILE,
               step: tuple[int, int] = VISION_STEP) -> list[tuple[int, int, int, int]]:
    """Overlapping (x, y, w, h) tiles that cover the whole frame."""
    tw, th = min(tile[0], width), min(tile[1], height)
    return [(x, y, tw, th) for y in _starts(height, th, step[1]) for x in _starts(width, tw, step[0])]


def tile_frame_args(source: str | Path, t: float, tiles: list[tuple[int, int, int, int]], pngs: list[Path],
                    thumb: Path, scale: int = VISION_SCALE) -> list[str]:
    """One frame at t: each tile cropped and upscaled to its own PNG, plus the grey thumbnail."""
    n = len(tiles)
    graph = [f"[0:v]split={n + 1}" + "".join(f"[c{k}]" for k in range(n)) + "[s0]"]
    graph += [f"[c{k}]crop={w}:{h}:{x}:{y},scale=iw*{scale}:ih*{scale}:flags=lanczos[o{k}]"
              for k, (x, y, w, h) in enumerate(tiles)]
    graph.append(f"[s0]scale={THUMB[0]}:{THUMB[1]}:flags=area,format=gray[s]")
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-ss", f"{max(t, 0):.3f}", "-i", str(source),
            "-filter_complex", ";".join(graph)]
    for k, png in enumerate(pngs):
        args += ["-map", f"[o{k}]", "-frames:v", "1", str(png)]
    return args + ["-map", "[s]", "-frames:v", "1", "-f", "rawvideo", str(thumb)]


def vision_box(norm: tuple[float, float, float, float], rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """A Vision box (normalised, origin bottom-left) in a tile -> source pixels (origin top-left)."""
    nx, ny, nw, nh = norm
    x, y, w, h = rect
    return (int(round(x + nx * w)), int(round(y + (1 - ny - nh) * h)),
            max(1, int(round(nw * w))), max(1, int(round(nh * h))))


def split_line(text: str, conf: float, line: tuple[int, int, int], rect: tuple[int, int, int, int],
               box_for_range) -> list[Word]:
    """One Vision line -> Words: each whitespace-separated run with its own box
    (`box_for_range(start, length)` returns the normalised box or None)."""
    words = []
    for m in re.finditer(r"\S+", text):
        norm = box_for_range(m.start(), m.end() - m.start())
        if norm is not None:
            words.append(Word(m.group(), vision_box(norm, rect), conf, line))
    return words


def vision_words(png: Path, rect: tuple[int, int, int, int], *, tile: int = 0) -> list[Word]:
    """Read one upscaled tile with Apple Vision; boxes come back in source pixels."""
    import objc
    import Vision
    from Foundation import NSURL

    with objc.autorelease_pool():
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(png)), None)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setUsesLanguageCorrection_(False)  # names, numbers and addresses are not dictionary words
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise RuntimeError(f"redact: Apple Vision failed on a frame: {err}")
        words: list[Word] = []
        for n, obs in enumerate(req.results() or []):
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            cand = cands[0]

            def box_for_range(start: int, length: int, cand=cand):
                found, _ = cand.boundingBoxForRange_error_((start, length), None)
                if found is None:
                    return None
                b = found.boundingBox()
                return (b.origin.x, b.origin.y, b.size.width, b.size.height)

            words += split_line(str(cand.string()), float(cand.confidence()) * 100, (tile, n, 0), rect, box_for_range)
        return words


def _thumb_diff(a: bytes | None, b: bytes | None) -> float:
    if not a or not b or len(a) != len(b):
        return 255.0
    return float(max(abs(x - y) for x, y in zip(a, b)))


def detect(source: str | Path, spans: list[tuple[float, float]], *, info: VideoInfo, every: float = EVERY_SECONDS,
           terms: list[re.Pattern[str]] = (), all_text: bool = False, ocr: str = "auto",
           log=lambda msg: None) -> list[Redaction]:
    """Sample, OCR and box (module docstring). Raises RuntimeError when the OCR engine is missing."""
    engine = resolve_ocr(ocr)
    tesseract = shutil.which("tesseract") if engine == "tesseract" else None
    tiles = tile_rects(info.width, info.height) if engine == "vision" else []
    times = sample_times(spans, every)
    if not times:
        return []
    log(f"redact: reading {len(times)} frames (every {every:g} s) with {engine} "
        f"for {'all text' if all_text else 'private details'}")
    with tempfile.TemporaryDirectory(prefix="clipbot-redact-") as tmp:
        tmpdir = Path(tmp)

        def grab(k_t):
            k, t = k_t
            thumb = tmpdir / f"f{k:05d}.gray"
            if engine == "vision":
                pngs = [tmpdir / f"f{k:05d}-{n:03d}.png" for n in range(len(tiles))]
                args = tile_frame_args(source, t, tiles, pngs, thumb)
            else:
                pngs = [tmpdir / f"f{k:05d}.png"]
                args = frame_args(source, t, pngs[0], thumb)
            proc = subprocess.run(args, capture_output=True, text=True)
            if proc.returncode or not all(p.is_file() for p in pngs):
                raise RuntimeError(f"redact: could not read the frame at {t:.1f} s: "
                                   f"{(proc.stderr.strip().splitlines() or ['no output'])[-1]}")
            data = thumb.read_bytes() if thumb.is_file() else None
            thumb.unlink(missing_ok=True)
            return pngs, data

        def ocr_frame(pngs: list[Path]) -> list[Word]:
            if engine == "vision":
                words: list[Word] = []
                for n, (png, rect) in enumerate(zip(pngs, tiles)):
                    words += vision_words(png, rect, tile=n)
                return words
            # One thread per tesseract: its OpenMP threads on top of WORKERS processes thrash (40x slower here).
            proc = subprocess.run([tesseract, str(pngs[0]), "stdout", "--psm", "11", "tsv"], capture_output=True,
                                  text=True, env={**os.environ, "OMP_THREAD_LIMIT": "1"})
            if proc.returncode:
                raise RuntimeError(f"redact: tesseract failed: {(proc.stderr.strip().splitlines() or ['no output'])[-1]}")
            return parse_tsv(proc.stdout, OCR_SCALE)

        # Chunk by chunk, so a whole-recording scan never holds thousands of 4K PNGs on disk at once.
        # A frame that is the same picture as the frame last read (compared with that frame, not just
        # the previous sample, so slow drift adds up and forces a new read) reuses its words.
        owner: list[int] = []
        last_read: bytes | None = None  # thumbnail of the frame last sent to OCR
        read: dict[int, list[Word]] = {}
        chunk = WORKERS * 16
        try:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for lo in range(0, len(times), chunk):
                    ks = range(lo, min(lo + chunk, len(times)))
                    pngs = dict(zip(ks, pool.map(grab, ((k, times[k]) for k in ks))))
                    todo = []
                    for k in ks:
                        thumb = pngs[k][1]
                        same = (k and times[k] - times[k - 1] <= every + 0.01
                                and _thumb_diff(thumb, last_read) < SAME_FRAME_DIFF)
                        owner.append(owner[k - 1] if same else k)
                        if not same:
                            todo.append(k)
                            last_read = thumb
                    read.update(zip(todo, pool.map(lambda k: ocr_frame(pngs[k][0]), todo)))
                    for files, _ in pngs.values():
                        for png in files:
                            png.unlink(missing_ok=True)
        except FileNotFoundError as e:
            raise RuntimeError("ffmpeg not found on PATH") from e
        log(f"redact: OCR on {len(read)} distinct frames ({len(times) - len(read)} unchanged, reused)")

    found: list[Redaction] = []
    for k, t in enumerate(times):
        for box, why in find_sensitive(read[owner[k]], terms=terms, all_text=all_text):
            clamped = clamp_box(box, info.width, info.height, pad=PAD_PIXELS + box[3] // 4)
            if clamped:
                found.append(Redaction(max(0.0, t - every), t + every, clamped, why))
    return [_clip_to_spans(r, spans, every) for r in merge(found)]


def _clip_to_spans(r: Redaction, spans: list[tuple[float, float]], every: float) -> Redaction:
    """A blur never needs to reach further than `every` outside the spans it was
    sampled in; keeps the ranges in redactions.json readable."""
    lo = min((a for a, b in spans if b >= r.start), default=r.start)
    hi = max((b for a, b in spans if a <= r.end), default=r.end)
    return replace(r, start=max(r.start, lo - every, 0.0), end=min(r.end, hi + every))


# ----------------------------------------------------------------- the step clipbot runs

def redacted_path(source: str | Path, out_dir: str | Path) -> Path:
    return Path(out_dir) / f"{Path(source).stem}.redacted.mp4"


def run(source: str | Path, mode: str, spans: list[tuple[float, float]], out_dir: str | Path, *,
        terms_file: str | None = None, every: float = EVERY_SECONDS, duration: float | None = None,
        ocr: str = "auto", log=lambda msg: None) -> tuple[Path | None, list[Redaction]]:
    """`--redact MODE`: `auto` (private details), `text` (every line of text) or
    a redactions.json path. Writes redactions.json in `out_dir` and the redacted
    copy next to it; returns (copy, redactions), copy None when nothing needed
    blurring (the plan then keeps the original)."""
    info = video_info(source)
    terms = compile_terms(Path(terms_file).read_text(encoding="utf-8").splitlines()) if terms_file else []
    if mode in ("auto", "text"):
        redactions = detect(source, spans, info=info, every=every, terms=terms, all_text=mode == "text",
                            ocr=ocr, log=log)
    else:
        if terms_file:
            log("redact: --redact-terms ignored: a redactions file is applied as written")
        redactions = load_redactions(mode, info.width, info.height, duration)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    listing = out_dir / "redactions.json"
    if mode in ("auto", "text") or Path(mode).resolve() != listing.resolve():
        write_redactions(listing, redactions)
    kinds: dict[str, int] = {}
    for r in redactions:
        for why in (r.why or "manual").split(", "):
            kinds[why] = kinds.get(why, 0) + 1
    detail = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
    log(f"redact: {len(redactions)} region(s) to blur{f' ({detail})' if detail else ''}; listed in {listing}")
    if not redactions:
        return None, []
    copy = redacted_path(source, out_dir)
    apply(source, redactions, copy, log=log)
    return copy, redactions
