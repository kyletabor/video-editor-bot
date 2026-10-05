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

Where two neighbouring samples differ a lot (a scroll, a page switch), more are taken
in between, down to one frame apart, so a moving word is boxed where it actually is.
Samples that show the same screen form a run; a run is OCR'd a few times (OCR misses
a word in one frame and reads it in the next) and everything found is blurred for the
whole run, from the sample before it to the sample after it. The blur itself is one
blurred copy of each frame shown through a mask track (`write_mask_track`), so its cost
does not grow with the number of boxes.

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
import struct
import subprocess
import sys
import tempfile
import zlib
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
MOTION_DELTA = 12  # a thumbnail pixel that changed by more than this counts as changed
MOTION_SHARE = 0.05  # this share changed between two samples: the screen moved, look in between
STILL_SHARE = 0.01  # under this share changed: the same screen (a speaker tile alone stays under it)
REREAD_SECONDS = 3.0  # a still screen is OCR'd again this often; OCR misses differ frame to frame
MAX_REFINE = 3  # extra samples where the screen moved, at most this many per regular sample
WORKERS = 4
BLUR_RADIUS = 20  # luma box radius, 3 passes: a 7-60 px word comes out as a smudge
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
TAG = "clipbot-redact"
VERSION = 3  # bump when the copy a given source + boxes produce changes (cached copies are then rewritten)

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

def mask_timeline(redactions: list[Redaction], duration: float, frame: float) -> list[tuple[float, float, tuple]]:
    """[0, duration] cut into intervals, each with the boxes blurred during it
    (empty tuple = nothing). Every range is widened by one frame on both sides: the
    mask track is a still per interval resampled to the source's frame rate, and a
    frame that lands on an interval edge must never fall on the unblurred side."""
    edges = {0.0, float(duration)}
    spans = []
    for r in redactions:
        a, b = max(0.0, r.start - frame), min(float(duration), r.end + frame)
        if b > a:
            spans.append((a, b, r.box))
            edges.update((a, b))
    cuts = sorted(edges)
    out: list[tuple[float, float, tuple]] = []
    for a, b in zip(cuts, cuts[1:]):
        boxes = tuple(sorted({box for s, e, box in spans if s < b and e > a}))
        if out and out[-1][2] == boxes:
            out[-1] = (out[-1][0], b, boxes)
        else:
            out.append((a, b, boxes))
    return out


def _png_gray(width: int, height: int, boxes) -> bytes:
    """An 8-bit greyscale PNG: white inside the boxes, black elsewhere (no Pillow needed)."""
    row_black = bytes(width)
    rows = []
    for y in range(height):
        row = bytearray(row_black)
        for x, by, w, h in boxes:
            if by <= y < by + h:
                row[x:x + w] = b"\xff" * len(row[x:x + w])
        rows.append(b"\x00" + bytes(row))
    raw = zlib.compress(b"".join(rows), 6)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def write_mask_track(redactions: list[Redaction], info: "VideoInfo", folder: Path) -> Path:
    """One mask PNG per distinct set of boxes and an ffconcat list that shows each
    for its interval: the mask track `apply` blends through. Returns the list."""
    duration = info.duration or max((r.end for r in redactions), default=1.0) + 1.0
    frame = 1.0 / info.fps
    pngs: dict[tuple, Path] = {}
    lines = ["ffconcat version 1.0"]
    timeline = mask_timeline(redactions, duration, frame) + [(duration, duration + 2.0, ())]  # padding past the end
    for a, b, boxes in timeline:
        if boxes not in pngs:
            png = folder / f"mask{len(pngs):04d}.png"
            png.write_bytes(_png_gray(info.width, info.height, boxes))
            pngs[boxes] = png
        lines += [f"file '{pngs[boxes].name}'", f"duration {b - a:.6f}"]
    lines.append(f"file '{pngs[timeline[-1][2]].name}'")  # the concat demuxer drops the last duration otherwise
    listing = folder / "masks.ffconcat"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return listing


def filter_graph(info: "VideoInfo") -> str:
    """Every frame blurred once, then shown only where the mask track is white.

    Why a mask track instead of one crop + blur + overlay per box: Talk #3 needed 471
    boxes, and that graph split every frame 472 ways; the hour-long copy had not
    finished after an hour. One blur and one alpha blend per frame cost the same for
    5 boxes or 5,000 and wrote the same copy in about 12 minutes. Without -copyts
    ffmpeg shifts the source so its first frame is t = 0, and the mask track also
    starts at 0, so the ranges go in as written (a .ts source starting at 1.46 s was
    once blurred 1.46 s late when they were shifted)."""
    luma = max(1, min(BLUR_RADIUS, min(info.width, info.height) // 2 - 1))  # boxblur's limit is half a side
    chroma = max(1, min(BLUR_RADIUS // 2, min(info.width, info.height) // 4 - 1))  # yuv420p chroma is half size
    blur = f"boxblur=luma_radius={luma}:luma_power=3:chroma_radius={chroma}:chroma_power=3"
    return (f"[0:v]split=2[base][soft];[soft]{blur}[blur];"
            f"[1:v]fps={info.fps:.6f},format=gray,scale={info.width}:{info.height}[mask];"
            "[blur][mask]alphamerge[masked];[base][masked]overlay=0:0:eof_action=pass:format=auto,format=yuv420p[vout]")


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


def apply_args(source: str | Path, masks: Path, info: "VideoInfo", out: str | Path, *, tag: str = "") -> list[str]:
    """ffmpeg argv for the redacted copy: video re-encoded through the blur
    graph and the mask track (`write_mask_track`), audio copied as is, text captions
    converted to mov_text (the one caption codec mp4 holds: an MKV's subrip cannot be
    copied in) so the renderer still reads them from the copy, container metadata kept."""
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(source),
        "-f", "concat", "-safe", "0", "-i", str(masks),
        "-filter_complex", filter_graph(info),
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
        "about a fifth of its length")
    info = video_info(source)
    try:
        with tempfile.TemporaryDirectory(prefix="clipbot-masks-") as folder:
            masks = write_mask_track(redactions, info, Path(folder))
            proc = subprocess.run(apply_args(source, masks, info, tmp, tag=tag), capture_output=True, text=True)
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
    fps: float = 25.0
    duration: float | None = None


def video_info(source: str | Path) -> VideoInfo:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,avg_frame_rate,r_frame_rate:format=duration", "-of", "json", str(source)],
            check=True, capture_output=True, text=True,
        ).stdout
    except FileNotFoundError as e:
        raise RuntimeError("ffprobe not found on PATH") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffprobe failed: {e.stderr.strip()}") from e
    data = json.loads(out)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"{source}: no video stream to redact")
    st = streams[0]
    fps = 25.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = str(st.get(key) or "0/0").partition("/")
        try:
            if float(num) > 0 and float(den or 1) > 0:
                fps = float(num) / float(den or 1)
                break
        except ValueError:
            continue
    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return VideoInfo(int(st["width"]), int(st["height"]), fps, duration)


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


def thumb_args(source: str | Path, t: float, thumb: Path) -> list[str]:
    """Only the grey thumbnail at t: cheap enough to take many times over a scroll."""
    return ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-ss", f"{max(t, 0):.3f}", "-i", str(source),
            "-vf", f"scale={THUMB[0]}:{THUMB[1]}:flags=area,format=gray", "-frames:v", "1", "-f", "rawvideo", str(thumb)]


def changed_share(a: bytes | None, b: bytes | None) -> float:
    """Share of thumbnail pixels that changed by more than MOTION_DELTA (1.0 when unknown).
    A call's speaker tile moves all the time but is a few percent of the frame; a scroll
    or a page switch changes much more."""
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(1 for x, y in zip(a, b) if abs(x - y) > MOTION_DELTA) / len(a)


def refine(times: list[float], thumbs: list[bytes | None], take_thumbs, *, every: float, step: float,
           budget: int) -> tuple[list[float], list[bytes | None]]:
    """Add samples where the picture moved between two neighbours (a scroll, a page
    switch) until neighbours are one frame (`step`) apart or alike, at most `budget`
    extra samples. Why: a word seen at 1 s and again at 2 s after a scroll is in two
    places; a blur from 0 to 2 s at the first place leaves the second readable (Talk #3:
    a client's name showed for a second mid-scroll)."""
    added = 0
    while added < budget:
        mids = [(times[i] + times[i + 1]) / 2 for i in range(len(times) - 1)
                if step * 1.5 < times[i + 1] - times[i] <= every + 0.01
                and changed_share(thumbs[i], thumbs[i + 1]) >= MOTION_SHARE]
        mids = mids[:budget - added]
        if not mids:
            break
        merged = sorted(zip(times + mids, thumbs + take_thumbs(mids)), key=lambda p: p[0])
        times, thumbs = [t for t, _ in merged], [th for _, th in merged]
        added += len(mids)
    return times, thumbs


def stretches(times: list[float], thumbs: list[bytes | None], every: float) -> list[tuple[int, int]]:
    """[first, last] sample indices of each run that shows one unchanged screen
    (compared with the run's first frame, so slow drift ends a run too)."""
    runs: list[tuple[int, int]] = []
    for k in range(len(times)):
        if (runs and times[k] - times[k - 1] <= every + 0.01
                and changed_share(thumbs[runs[-1][0]], thumbs[k]) < STILL_SHARE):
            runs[-1] = (runs[-1][0], k)
        else:
            runs.append((k, k))
    return runs


def readings(run: tuple[int, int], times: list[float]) -> list[int]:
    """Which samples of a run to OCR: the first, then one every REREAD_SECONDS, and the
    last. OCR misses a word in one frame and reads it in the next, so a still screen is
    read more than once and what any reading found is blurred for the whole run."""
    first, last = run
    picked = [first]
    for k in range(first + 1, last + 1):
        if times[k] - times[picked[-1]] >= REREAD_SECONDS or (k == last and times[k] - times[picked[-1]] >= 1.0):
            picked.append(k)
    return picked


def detect(source: str | Path, spans: list[tuple[float, float]], *, info: VideoInfo, every: float = EVERY_SECONDS,
           terms: list[re.Pattern[str]] = (), all_text: bool = False, ocr: str = "auto",
           log=lambda msg: None) -> list[Redaction]:
    """Sample, OCR and box (module docstring). Raises RuntimeError when the OCR engine is missing.

    1. A grey thumbnail every `every` seconds inside the spans; where neighbours differ
       a lot (a scroll), more thumbnails in between, down to one frame (`refine`).
    2. Runs of samples that show the same screen (`stretches`); each run is OCR'd a few
       times (`readings`) and everything found is blurred for the whole run, from the
       sample before it to the sample after it. A sample in motion is a run of one, so
       its blur spans only the frames between its neighbours."""
    engine = resolve_ocr(ocr)
    tesseract = shutil.which("tesseract") if engine == "tesseract" else None
    tiles = tile_rects(info.width, info.height) if engine == "vision" else []
    times = sample_times(spans, every)
    if not times:
        return []
    with tempfile.TemporaryDirectory(prefix="clipbot-redact-") as tmp:
        tmpdir = Path(tmp)
        counter = iter(range(10 ** 9))

        def thumb_at(t: float) -> bytes | None:
            path = tmpdir / f"t{next(counter):06d}.gray"
            proc = subprocess.run(thumb_args(source, t, path), capture_output=True, text=True)
            if proc.returncode:
                raise RuntimeError(f"redact: could not read the frame at {t:.1f} s: "
                                   f"{(proc.stderr.strip().splitlines() or ['no output'])[-1]}")
            data = path.read_bytes() if path.is_file() else None
            path.unlink(missing_ok=True)
            return data

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
            thumb.unlink(missing_ok=True)
            return pngs

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

        def read_one(k: int) -> list[tuple[tuple[int, int, int, int], str]]:
            pngs = grab((k, times[k]))
            try:
                return find_sensitive(ocr_frame(pngs), terms=terms, all_text=all_text)
            finally:
                for png in pngs:
                    png.unlink(missing_ok=True)

        try:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                base = len(times)
                thumbs = list(pool.map(thumb_at, times))
                times, thumbs = refine(times, thumbs, lambda ts: list(pool.map(thumb_at, ts)), every=every,
                                       step=1.0 / info.fps, budget=MAX_REFINE * base)
                runs = stretches(times, thumbs, every)
                todo = sorted({k for run in runs for k in readings(run, times)})
                log(f"redact: {base} samples (every {every:g} s) + {len(times) - base} where the screen moved; "
                    f"{len(runs)} distinct screens, {len(todo)} read with {engine} "
                    f"for {'all text' if all_text else 'private details'}")
                # one frame at a time through the pool: a whole-recording scan never holds thousands of PNGs
                hits = dict(zip(todo, pool.map(read_one, todo)))
        except FileNotFoundError as e:
            raise RuntimeError("ffmpeg not found on PATH") from e

    def near(i: int, j: int) -> bool:
        return 0 <= i < len(times) and 0 <= j < len(times) and abs(times[j] - times[i]) <= every + 0.01

    found: list[Redaction] = []
    for first, last in runs:
        start = times[first - 1] if near(first - 1, first) else times[first] - every
        end = times[last + 1] if near(last, last + 1) else times[last] + every
        for k in readings((first, last), times):
            for box, why in hits.get(k, []):
                clamped = clamp_box(box, info.width, info.height, pad=PAD_PIXELS + box[3] // 4)
                if clamped:
                    found.append(Redaction(max(0.0, start), end, clamped, why))
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
