"""Word-level timing: the unit every cut is measured in.

Why words: caption cues are 4 s blocks (Meet) or whole sentences (whisper). A
cut placed at a cue edge can land inside a word, which is what Kyle heard in the
first reel ("you awkwardly cut off some of the segments when someone was still
talking"). faster-whisper can time every word (transcribe.py,
``word_timestamps=True``); this module stores that as ``<name>.words.json`` next
to the SRT and loads it back, and ``--words FILE`` lets an ``--srt`` user point
at one.

When no word timing exists, `words_from_cues` estimates one from the cues by
character position so the sentence logic (cuts.py) can still decide WHICH
sentence a time falls in. Those words are marked ``timed=False`` and keep the cue
edges as their only safe cut points (`safe_start` / `safe_end`): nothing is ever
cut at an estimated time, and filler removal, which needs real word edges, is
skipped for them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .captions import Cue


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str
    speaker: str | None = None
    timed: bool = True  # False: start/end are estimated inside a caption cue
    cut_start: float | None = None  # safe cut points when not timed (the cue's edges)
    cut_end: float | None = None

    @property
    def safe_start(self) -> float:
        return self.start if self.cut_start is None else self.cut_start

    @property
    def safe_end(self) -> float:
        return self.end if self.cut_end is None else self.cut_end

    @property
    def duration(self) -> float:
        return self.end - self.start


def words_path(srt_path: str | Path) -> Path:
    """`talk.srt` -> `talk.words.json`, next to it."""
    return Path(srt_path).with_suffix(".words.json")


def save_words(path: str | Path, words: list[Word]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.text} for w in words]
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def load_words(path: str | Path) -> list[Word]:
    """Read `[{start, end, word}]`; rows with bad numbers or empty text are skipped.

    Sorted by start so callers can rely on order. Raises ValueError when the
    file is not a JSON list at all (a wrong `--words` argument should be loud)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of {{start, end, word}}")
    out: list[Word] = []
    for row in data:
        try:
            start, end = float(row["start"]), float(row["end"])
            text = str(row.get("word") or row.get("text") or "").strip()
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if text and end > start:
            out.append(Word(start, end, text))
    out.sort(key=lambda w: (w.start, w.end))
    return out


def words_from_cues(cues: list[Cue]) -> list[Word]:
    """Estimate a word timeline from caption cues by character position.

    Each token gets a slice of its cue proportional to its length (plus the
    space after it), so a sentence boundary that falls mid-cue lands roughly
    where it was spoken. The estimate is only ever used to decide which sentence
    a time belongs to; `safe_start`/`safe_end` stay at the cue edges.
    """
    out: list[Word] = []
    for c in cues:
        tokens = c.text.split()
        if not tokens or c.end <= c.start:
            continue
        total = sum(len(t) + 1 for t in tokens)
        before = 0
        for t in tokens:
            share = len(t) + 1
            start = c.start + c.duration * before / total
            end = c.start + c.duration * (before + share) / total
            out.append(Word(start, end, t, c.speaker, timed=False, cut_start=c.start, cut_end=c.end))
            before += share
    return out
