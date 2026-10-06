"""Framing: the words around the moments, from one JSON file (``--framing``).

Why a file: the third reel's intro card said "10 moments · 4:50" while the reel
ran 4:55. The intro/opening/closing/outro text and the card timings had been
patched into plan.json by hand after ``clipbot reel`` ran, and the runtime line
had been computed before the patch. With a framing file every card is known
when the plan is built, so `reel.build_reel_plan` can compute the runtime line
last and it is right by construction; nothing needs post-editing. The existing
flags (``--title``, ``--date``, ``--takeaways``, ``--transition``) keep working;
the framing file overrides them wherever it says something.

    {
      "title": "Pipeline AI Talk #2: Build a clip bot, live",   -> intro title
      "date": "2026-09-25",                                     -> "Recorded ..." line
      "what_you_will_learn": ["...", "..."],   -> opening card lines (<= 4)
      "takeaways": ["...", "..."],             -> closing cards, 4 lines each (<= 12)
      "outro": {"title": "...", "lines": ["..."]},
      "opening_seconds": 7, "closing_seconds": 7, "outro_seconds": 6,   -> per card, 1-10
      "music_gain_db": -14, "music_fade_seconds": 1.0,   -> output.reel.music (needs --music or a style)
      "music_at": "ends",                      -> a cues style plays only at the open and the close,
                                                  not a sting on every chapter card ("cards", the default)
      "audio_fade_seconds": 0.2,               -> clip audio fade at every cut, 0-1
      "transition": "dip"  |  {"kind": "dissolve", "seconds": 0.5},
      "style": "pipeline",                     -> the style pack (styles.py); --style overrides it
      "layout": {"kind": "pip", "screen": [0, 240, 1440, 600], "speaker": [1440, 270, 480, 270]}
                                               -> every clip's frame (story.py, contract v1.4): the
                                                  shared screen large, the speaker tile in a corner
    }

Everything is optional; unknown keys, wrong types and text over the contract's
limits (titles <= 80, lines <= 120, 4 lines per card) are errors rather than
silent clipping, because a framing file is written by hand and a line that
comes out with an ellipsis on the card is a bug the author wants to hear about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .lessons import LINE_LIMIT, MAX_CARD_LINES, TITLE_LIMIT, card_seconds

TRANSITIONS = ("cut", "dip", "dissolve")
CARD_SECONDS_RANGE = (1.0, 10.0)  # contract: card.seconds
MUSIC_GAIN_RANGE = (-40.0, 0.0)  # contract: music.gain_db
MUSIC_FADE_RANGE = (0.0, 5.0)  # contract: music.fade_seconds
AUDIO_FADE_RANGE = (0.0, 1.0)  # contract: audio_fade_seconds
TRANSITION_RANGE = (0.1, 1.5)  # contract: transition.seconds
MAX_CLOSING_CARDS = 3  # contract: closing maxItems
KEYS = (
    "title", "date", "what_you_will_learn", "takeaways", "outro", "opening_seconds", "closing_seconds",
    "outro_seconds", "music_gain_db", "music_fade_seconds", "transition", "style",
    "audio_fade_seconds", "layout", "music_at",
)


@dataclass(frozen=True)
class Framing:
    title: str | None = None
    date: str | None = None
    what_you_will_learn: tuple[str, ...] = ()
    takeaways: tuple[str, ...] = ()
    outro_title: str | None = None
    outro_lines: tuple[str, ...] = ()
    opening_seconds: float | None = None
    closing_seconds: float | None = None
    outro_seconds: float | None = None
    music_gain_db: float | None = None
    music_fade_seconds: float | None = None
    transition_kind: str | None = None
    transition_seconds: float | None = None
    style: str | None = None
    audio_fade_seconds: float | None = None
    layout: dict | None = None  # default clip.layout (story.parse_layout)
    music_at: str | None = None  # score.MUSIC_AT

    def outro_card(self) -> dict | None:
        """The outro card the file asks for, or None when it says nothing about it."""
        if not self.outro_title:
            return None
        lines = list(self.outro_lines)
        return {"title": self.outro_title, "lines": lines,
                "seconds": self.outro_seconds if self.outro_seconds is not None else card_seconds(lines)}


def _text(value, where: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: expected non-empty text")
    value = " ".join(value.split())
    if len(value) > limit:
        raise ValueError(f"{where}: {len(value)} characters, the card holds {limit}: {value[:40]}...")
    return value


def _lines(value, where: str, max_lines: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where}: expected a list of lines")
    lines = tuple(_text(ln, f"{where}[{i}]", LINE_LIMIT) for i, ln in enumerate(value) if isinstance(ln, str) and ln.strip())
    if len(lines) > max_lines:
        raise ValueError(f"{where}: {len(lines)} lines, at most {max_lines} fit")
    return lines


def _number(value, where: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: expected a number")
    if not lo <= float(value) <= hi:
        raise ValueError(f"{where}: {value} is outside {lo:g}..{hi:g} (contract)")
    return float(value)


def parse_framing(data, where: str = "framing") -> Framing:
    """Validate one framing object (module docstring). Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError(f"{where}: expected a JSON object")
    unknown = sorted(set(data) - set(KEYS))
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}; known: {', '.join(KEYS)}")
    data = {k: v for k, v in data.items() if v is not None}
    out: dict = {}
    if "title" in data:
        out["title"] = _text(data["title"], f"{where}.title", TITLE_LIMIT)
    if "date" in data:
        out["date"] = _text(data["date"], f"{where}.date", LINE_LIMIT)
    if "what_you_will_learn" in data:
        out["what_you_will_learn"] = _lines(data["what_you_will_learn"], f"{where}.what_you_will_learn", MAX_CARD_LINES)
    if "takeaways" in data:
        out["takeaways"] = _lines(data["takeaways"], f"{where}.takeaways", MAX_CARD_LINES * MAX_CLOSING_CARDS)
    if "outro" in data:
        outro = data["outro"]
        if not isinstance(outro, dict) or "title" not in outro:
            raise ValueError(f"{where}.outro: expected {{title, lines}}")
        extra = sorted(set(outro) - {"title", "lines"})
        if extra:
            raise ValueError(f"{where}.outro: unknown key(s) {', '.join(extra)}")
        out["outro_title"] = _text(outro["title"], f"{where}.outro.title", TITLE_LIMIT)
        out["outro_lines"] = _lines(outro.get("lines") or [], f"{where}.outro.lines", MAX_CARD_LINES)
    for key in ("opening_seconds", "closing_seconds", "outro_seconds"):
        if key in data:
            out[key] = _number(data[key], f"{where}.{key}", *CARD_SECONDS_RANGE)
    if "music_gain_db" in data:
        out["music_gain_db"] = _number(data["music_gain_db"], f"{where}.music_gain_db", *MUSIC_GAIN_RANGE)
    if "music_fade_seconds" in data:
        out["music_fade_seconds"] = _number(data["music_fade_seconds"], f"{where}.music_fade_seconds", *MUSIC_FADE_RANGE)
    if "transition" in data:
        tr = data["transition"]
        if isinstance(tr, str):
            tr = {"kind": tr}
        if not isinstance(tr, dict) or set(tr) - {"kind", "seconds"} or "kind" not in tr:
            raise ValueError(f"{where}.transition: expected one of {', '.join(TRANSITIONS)} or {{kind, seconds}}")
        if tr["kind"] not in TRANSITIONS:
            raise ValueError(f"{where}.transition.kind: {tr['kind']!r} is not one of {', '.join(TRANSITIONS)}")
        out["transition_kind"] = tr["kind"]
        if tr.get("seconds") is not None:
            out["transition_seconds"] = _number(tr["seconds"], f"{where}.transition.seconds", *TRANSITION_RANGE)
    if "audio_fade_seconds" in data:
        out["audio_fade_seconds"] = _number(data["audio_fade_seconds"], f"{where}.audio_fade_seconds", *AUDIO_FADE_RANGE)
    if "style" in data:
        out["style"] = _text(data["style"], f"{where}.style", TITLE_LIMIT)
    if "music_at" in data:
        if data["music_at"] not in ("cards", "ends"):
            raise ValueError(f'{where}.music_at: "cards" or "ends"')
        out["music_at"] = data["music_at"]
    if "layout" in data:
        from .story import parse_layout  # story imports lessons, as this module does; no cycle at load time

        out["layout"] = parse_layout(data["layout"], f"{where}.layout")
    return Framing(**out)


def load_framing(path: str | Path) -> Framing:
    """Read and validate a framing file. Raises ValueError with the path in the message."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: not valid JSON ({e})") from e
    return parse_framing(data, where=str(path))
