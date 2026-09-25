"""Pick clip-worthy windows from cues. Heuristic v1 (veb-0rh); veb-0rh.1 refines.

Rules from bot/research/clip-guidelines.md:
- one idea per clip, length within [min, max]
- cues that match the request score high; filler-heavy cues score low
- windows snap to cue boundaries (Meet cues are 4 s, good enough for v1)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .captions import Cue

_STOP = set(
    "a an and are as at be but by for from has have i if in is it its of on or "
    "that the this to was were will with you your we our my me he she they them "
    "so do does did not no yes can could would should about into like just really "
    "what when where which who how part where's there here then than very whether "
    "while because since though although".split()
)
_FILLER = re.compile(r"\b(uh|um|hmm|like|you know)\b", re.I)
_WORD = re.compile(r"[a-z0-9']+")
# Words that describe the *task*, not the *topic*. They never count as evidence
# that a cue is about what the user asked for ("make a clip about X" -> only X matters).
_COMMAND = set(
    "make create cut clip clips clipping video videos short shorts highlight highlights "
    "moment moments part parts section sections segment segments bit bits piece "
    "find give show pull extract grab take get want need please one two three "
    "second seconds minute minutes long around roughly max min best good great "
    "talk talks talking says said say discuss discussing mention mentions".split()
)


@dataclass(frozen=True)
class Window:
    start: float
    end: float
    score: float
    takeaway: str
    cue_indexes: tuple[int, ...]

    @property
    def duration(self) -> float:
        return self.end - self.start


def keywords(request: str) -> set[str]:
    """Topic words from the request: stopwords and command words removed, crude stems added."""
    words = {
        w for w in _WORD.findall(request.lower())
        if w not in _STOP and w not in _COMMAND and len(w) > 2
    }
    # crude stemming so "questioning" matches "question", "clips" matches "clip"
    stems = set()
    for w in words:
        stems.add(w)
        for suf in ("ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                stems.add(w[: -len(suf)])
    return stems


def keyword_hits(cue: Cue, kws: set[str]) -> int:
    hits = 0
    for t in _WORD.findall(cue.text.lower()):
        for k in kws:
            if t == k or t.startswith(k):
                hits += 1
                break
    return hits


def score_cue(cue: Cue, kws: set[str]) -> float:
    text = cue.text.lower()
    tokens = _WORD.findall(text)
    if not tokens:
        return 0.0
    hits = keyword_hits(cue, kws)
    filler = len(_FILLER.findall(text))
    density = len(tokens) / max(cue.duration, 0.5)  # words per second
    s = 3.0 * hits + 0.15 * density - 0.5 * filler
    if "?" in cue.text:
        s += 0.5
    return s


def best_windows(
    cues: list[Cue],
    request: str,
    min_seconds: float,
    max_seconds: float,
    max_clips: int = 1,
) -> list[Window]:
    """Greedy: best-scoring window within bounds, then the next non-overlapping one."""
    if not cues:
        return []
    kws = keywords(request)
    scores = [score_cue(c, kws) for c in cues]
    hits = [keyword_hits(c, kws) for c in cues]
    picked: list[tuple[float, float]] = []  # selected time intervals, not just cue indexes

    def blocked(k: int) -> bool:
        # A cue is unusable if its time span intersects any already-selected interval.
        # Cue indexes alone are not enough: SRT allows simultaneous/overlapping cues.
        return any(cues[k].start < e and cues[k].end > s for s, e in picked)

    out: list[Window] = []
    for _ in range(max_clips):
        best: Window | None = None
        for i in range(len(cues)):
            if blocked(i):
                continue
            total = 0.0
            total_hits = 0
            first_hit = last_hit = -1
            for j in range(i, len(cues)):
                if blocked(j) or cues[j].start < cues[i].start:
                    break
                dur = cues[j].end - cues[i].start
                if dur > max_seconds:
                    break
                total += scores[j]
                total_hits += hits[j]
                if hits[j]:
                    last_hit = j
                    if first_hit < 0:
                        first_hit = j
                if dur < min_seconds:
                    continue
                if kws and total_hits == 0:
                    continue  # a request with keywords must actually match something
                if kws and (hits[i] == 0 or hits[j] == 0):
                    # Padding with unrelated cues is only allowed when the matched
                    # core is itself shorter than the minimum clip length.
                    core = cues[last_hit].end - cues[first_hit].start
                    if core >= min_seconds:
                        continue
                n = j - i + 1
                # Reward concentration: a tight window where most cues match beats a
                # wide one that merely accumulates stray hits. Mild length penalty too.
                concentration = total_hits / n if kws else 1.0
                w_score = total * concentration - 0.02 * dur
                if best is None or w_score > best.score:
                    joined = " ".join(c.text for c in cues[i : j + 1])
                    best = Window(
                        start=cues[i].start,
                        end=cues[j].end,
                        score=w_score,
                        takeaway=_one_sentence(joined, kws),
                        cue_indexes=tuple(range(i, j + 1)),
                    )
        if best is None or (kws and best.score <= 0):
            break
        if any(best.start < e and best.end > s for s, e in picked):
            break  # defensive: never emit overlapping clips
        picked.append((best.start, best.end))
        out.append(best)
    out.sort(key=lambda w: w.start)
    return out


def _one_sentence(text: str, kws: set[str] | None = None, limit: int = 200) -> str:
    """The one sentence in `text` that best matches the request (ties: longer wins)."""
    text = _FILLER.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" ,")
    parts = [p.strip(" ,") for p in re.split(r"(?<=[.!?])\s+", text) if p.strip(" ,")]
    if not parts:
        return "Clip."
    kws = kws or set()

    def rank(p: str) -> tuple[int, int]:
        hits = keyword_hits(Cue(0, 1, p), kws)
        return (hits, min(len(p), limit))

    best = max(parts, key=rank)
    if not best.endswith((".", "!", "?")):
        best += "."
    return best[:limit] if best else "Clip."
