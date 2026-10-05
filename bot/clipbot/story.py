"""Story fields for a reel (contract v1.4): extra slides, pictures, labels, framing.

Why: Kyle's review of the Talk 3 reel (2026-10-05). A reel of good moments with one
title card each did not teach the session to someone who was not there. He asked for
a reel told as a story: a slide that opens each part, a "what this covers" slide, a
link or QR code whenever a repo comes up, slides and on-screen labels that say what the
shared screen shows (most of the substance was said, not shown), and the shared screen
large with the speaker small in a corner. The bot does not invent any of that: the
author writes it into moments.json and framing.json, and this module checks it against
the contract before it reaches the plan, so a mistake is an error here rather than a
renderer failure (or a silently clipped slide) later.

moments.json, per moment (all optional):

    "cards":    [{"title", "lines", "seconds", "image" | "qr"}, ...]   -> clip.cards (<= 4)
    "image":    "shots/brief.png"     -> a picture on the moment's own chapter card
    "qr":       "https://github.com/...", -> a QR code on the chapter card (image or qr)
    "overlays": [{"start", "end", "text", "position"}, ...]  -> clip.overlays (source time)
    "layout":   "full" | "pip" | {"kind": "pip", "screen": [...], "speaker": [...]}

framing.json: "layout": {"kind": "pip", "screen": [x, y, w, h], "speaker": [...],
"corner": "bottom-right"} is the default for every moment; a moment opts out with
"layout": "full" or names its own regions.

Picture paths are resolved against the folder of the file that names them and written
to the plan as absolute paths (the renderer resolves relative plan paths from the repo
root, which is not where a talk's screenshots live).
"""

from __future__ import annotations

from pathlib import Path

from .lessons import LINE_LIMIT, MAX_CARD_LINES, TITLE_LIMIT
from .speakers import parse_clock

MAX_CLIP_CARDS = 4  # contract: clip.cards maxItems
MAX_OVERLAYS = 20  # contract: clip.overlays maxItems
OVERLAY_LIMIT = 120  # contract: overlay.text maxLength
CARD_SECONDS_RANGE = (1.0, 10.0)
CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
DEFAULT_CARD_SECONDS = 4.0  # a card with a picture or a QR code needs longer than a text card's 3 s


def _text(value, where: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: expected non-empty text")
    value = " ".join(value.split())
    if len(value) > limit:
        raise ValueError(f"{where}: {len(value)} characters, at most {limit} fit: {value[:40]}...")
    return value


def image_path(value, where: str, base: Path | None) -> str:
    """An existing PNG/JPEG, as an absolute path (relative paths resolve against `base`)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: expected a path to a PNG or JPEG")
    path = Path(value).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(f"{where}: {value} is not a PNG or JPEG")
    if not path.is_file():
        raise ValueError(f"{where}: {path} does not exist")
    return path.resolve().as_posix()


def qr_url(value, where: str) -> str:
    if not isinstance(value, str) or not value.startswith(("http://", "https://")) or not 8 <= len(value) <= 300:
        raise ValueError(f"{where}: expected an http(s) URL of at most 300 characters")
    return value


def card_visual(spec: dict, where: str, base: Path | None) -> dict:
    """{"image": abs path} or {"qr": url} or {} from a record with optional image/qr."""
    if spec.get("image") and spec.get("qr"):
        raise ValueError(f"{where}: a card shows an image or a QR code, not both")
    if spec.get("image"):
        return {"image": image_path(spec["image"], f"{where}.image", base)}
    if spec.get("qr"):
        return {"qr": qr_url(spec["qr"], f"{where}.qr")}
    return {}


def parse_card(spec, where: str, base: Path | None) -> dict:
    if not isinstance(spec, dict):
        raise ValueError(f"{where}: expected {{title, lines, seconds, image | qr}}")
    unknown = sorted(set(spec) - {"title", "lines", "seconds", "image", "qr"})
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}")
    card = {"title": _text(spec.get("title"), f"{where}.title", TITLE_LIMIT)}
    lines = spec.get("lines") or []
    if isinstance(lines, str):
        lines = [lines]
    if not isinstance(lines, list) or len(lines) > MAX_CARD_LINES:
        raise ValueError(f"{where}.lines: at most {MAX_CARD_LINES} lines")
    card["lines"] = [_text(ln, f"{where}.lines[{i}]", LINE_LIMIT) for i, ln in enumerate(lines)]
    visual = card_visual(spec, where, base)
    seconds = spec.get("seconds", DEFAULT_CARD_SECONDS if visual else 3.0)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not (
            CARD_SECONDS_RANGE[0] <= seconds <= CARD_SECONDS_RANGE[1]):
        raise ValueError(f"{where}.seconds: expected {CARD_SECONDS_RANGE[0]:g}..{CARD_SECONDS_RANGE[1]:g}")
    card["seconds"] = float(seconds)
    card.update(visual)
    return card


def parse_cards(value, where: str, base: Path | None) -> tuple[dict, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CLIP_CARDS:
        raise ValueError(f"{where}: expected a list of 1-{MAX_CLIP_CARDS} cards")
    return tuple(parse_card(c, f"{where}[{i}]", base) for i, c in enumerate(value))


def parse_overlays(value, where: str, span: tuple[float, float] | None = None) -> tuple[dict, ...]:
    """Overlays in source seconds. With `span` (the moment), each must overlap it."""
    if not isinstance(value, list) or len(value) > MAX_OVERLAYS:
        raise ValueError(f"{where}: expected a list of at most {MAX_OVERLAYS} overlays")
    out = []
    for i, ov in enumerate(value):
        w = f"{where}[{i}]"
        if not isinstance(ov, dict) or set(ov) - {"start", "end", "text", "position"}:
            raise ValueError(f"{w}: expected {{start, end, text, position}}")
        try:
            start, end = parse_clock(ov["start"]), parse_clock(ov["end"])
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"{w}: needs start and end (seconds or h:mm:ss): {e}") from e
        if end <= start:
            raise ValueError(f"{w}: end must be after start")
        if span and (end <= span[0] or start >= span[1]):
            raise ValueError(f"{w}: {start:g}-{end:g} s is outside the moment ({span[0]:g}-{span[1]:g} s)")
        position = ov.get("position", "top")
        if position not in ("top", "bottom"):
            raise ValueError(f"{w}.position: top or bottom")
        out.append({"start": round(start, 3), "end": round(end, 3),
                    "text": _text(ov.get("text"), f"{w}.text", OVERLAY_LIMIT), "position": position})
    return tuple(out)


def _box(value, where: str) -> list[int]:
    if (not isinstance(value, list) or len(value) != 4
            or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in value)
            or value[2] <= 0 or value[3] <= 0):
        raise ValueError(f"{where}: expected [x, y, w, h] in source pixels")
    return list(value)


def parse_layout(value, where: str, default: dict | None = None) -> dict | None:
    """"full" -> None (the plain frame); "pip" -> `default` (framing's regions);
    an object -> validated {kind, screen, speaker, corner}."""
    if value is None:
        return default
    if value == "full":
        return None
    if value == "pip":
        if not default:
            raise ValueError(f'{where}: "pip" needs framing.layout with the screen and speaker regions')
        return default
    if not isinstance(value, dict) or set(value) - {"kind", "screen", "speaker", "corner"}:
        raise ValueError(f"{where}: expected \"full\", \"pip\" or {{kind, screen, speaker, corner}}")
    kind = value.get("kind", "pip")
    if kind == "full":
        return None
    if kind != "pip":
        raise ValueError(f"{where}.kind: full or pip")
    out = {"kind": "pip", "screen": _box(value.get("screen"), f"{where}.screen")}
    if value.get("speaker") is not None:
        out["speaker"] = _box(value["speaker"], f"{where}.speaker")
    corner = value.get("corner", "bottom-right")
    if corner not in CORNERS:
        raise ValueError(f"{where}.corner: one of {', '.join(CORNERS)}")
    out["corner"] = corner
    return out


def clip_overlays(overlays: tuple[dict, ...], segments: list[tuple[float, float]]) -> list[dict]:
    """Overlays that touch a kept segment (the renderer shows only kept frames anyway;
    dropping the rest keeps the plan honest about what the viewer sees)."""
    return [dict(o) for o in overlays if any(o["start"] < b and o["end"] > a for a, b in segments)]
