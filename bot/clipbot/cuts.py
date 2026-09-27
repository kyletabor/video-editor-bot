"""Where a cut may land: sentence boundaries and speech-free spans.

Kyle's review of the first reel: "You awkwardly cut off some of the segments
when someone was still talking" and "cut out ums, ahs, and cut out the long
silence segments". Two mechanisms answer that, both pure functions over word
timings (words.py) and silence lists so they are testable with synthetic data:

1. `snap_outward` moves a requested [start, end] to the sentence that contains
   each edge: start becomes the start of the sentence the requested start falls
   in, end the end of the sentence the requested end falls in. An edge is never
   moved inward, so a moment can only grow. `pad` then adds `lead` seconds before
   the first word and `tail` after the last, because ASR word edges are a little
   early and a fade needs room.

2. `tighten` turns one padded moment into a keep-list of segments: filler
   words ("um", "uh", an isolated "like" / "you know") are dropped and pauses
   longer than `max_silence` are shortened to `max_silence` (a 12 s wait for an
   answer becomes a 0.7 s beat, not a jump cut). Guardrails, in order: a cut
   never lands inside a word (word timings are the unit; silences are clipped to
   word edges); each kept span keeps at least `breath` seconds of air on both
   sides; no kept fragment is shorter than `min_fragment` (the neighbouring cut
   is cancelled instead); and if the cuts would remove more than `max_removed`
   of the moment, the moment is left intact with a note. Filler removal needs
   real word timings; with only caption cues it is skipped and the caller says so.

Sentences come from punctuation (".", "!", "?"), a speaker change, or a pause
longer than `SENTENCE_GAP`; an unpunctuated run (raw ASR) is capped at
`MAX_SENTENCE` seconds so a cut can never be dragged across half a minute.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .words import Word

LEAD_SECONDS = 0.15  # before the first word of a moment
TAIL_SECONDS = 0.3  # after its last word
BREATH_SECONDS = 0.15  # air kept on each side of every internal cut
MAX_SILENCE = 0.7  # pauses longer than this are shortened
MIN_FRAGMENT = 1.5  # a kept span shorter than this is merged with a neighbour
MAX_REMOVED = 0.40  # never take more than this share out of one moment
MIN_CUT = 0.2  # a filler cut shorter than this is not worth a join
MIN_PAUSE_CUT = 0.4  # a pause is only shortened when that saves this much (a join is a jump cut)
MAX_SEGMENTS = 20  # contract: clips[].segments maxItems
SENTENCE_GAP = 1.5
MAX_SENTENCE = 20.0
MAX_SNAP = 12.0  # an edge never moves further than this (snap_outward)
ISOLATION_GAP = 0.25  # a pause this long on both sides makes "like" a filler

_TERMINAL = re.compile(r"[.!?]+[\"'”’)\]]*$")
_CLAUSE_END = (",", ".", "?", "!", ";", ":", "…")
_HARD_FILLERS = {"um", "umm", "uh", "uhh", "uhm", "ah", "er", "erm", "hmm", "hm", "mm", "mhm"}
_SOFT_FILLERS = {"like"}  # only when set off by punctuation or pauses


@dataclass(frozen=True)
class Span:
    """A sentence. `start`/`end` are safe cut points (word edges, or cue edges when
    the words are estimated); `first`/`last` are when its speech begins and ends,
    used to decide which sentence a time falls in."""

    start: float
    end: float
    first: float
    last: float
    text: str

    def contains_start(self, t: float) -> bool:
        return self.first <= t < self.last

    def contains_end(self, t: float) -> bool:
        return self.first < t <= self.last


def _same_speaker(a: Word, b: Word) -> bool:
    return a.speaker == b.speaker or not a.speaker or not b.speaker


def sentence_spans(words: list[Word]) -> list[Span]:
    """Group words into sentences (module docstring). Empty input -> []."""
    out: list[Span] = []
    buf: list[Word] = []

    def flush() -> None:
        if buf:
            out.append(Span(buf[0].safe_start, buf[-1].safe_end, buf[0].start, buf[-1].end,
                            " ".join(w.text for w in buf)))
            buf.clear()

    for i, w in enumerate(words):
        buf.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if (
            _TERMINAL.search(w.text)
            or nxt is None
            or not _same_speaker(w, nxt)
            or nxt.start - w.end > SENTENCE_GAP
            or nxt.end - buf[0].start > MAX_SENTENCE
        ):
            flush()
    flush()
    return out


class Snapper:
    """Snap times to the sentence spans (see `snap_outward`). Build once per
    transcript: it indexes the safe edges so a time that already sits on one is
    recognised in O(1)."""

    def __init__(self, spans: list[Span], max_snap: float = MAX_SNAP):
        self.spans = spans
        self.max_snap = max_snap
        self._starts = {round(sp.start, 3) for sp in spans}
        self._ends = {round(sp.end, 3) for sp in spans}

    def start(self, t: float) -> float:
        if round(t, 3) in self._starts:
            return t  # already a safe boundary: a fixed point, so moments.json round-trips
        for sp in self.spans:
            if sp.contains_start(t):
                if t - sp.start <= self.max_snap:
                    return sp.start
                if t - sp.first <= self.max_snap:
                    return sp.first
                break
        return t

    def end(self, t: float) -> float:
        if round(t, 3) in self._ends:
            return t
        for sp in self.spans:
            if sp.contains_end(t):
                if sp.end - t <= self.max_snap:
                    return sp.end
                if sp.last - t <= self.max_snap:
                    return sp.last
                break
        return t

    def snap(self, start: float, end: float) -> tuple[float, float]:
        return self.start(start), self.end(end)


def snap_outward(start: float, end: float, spans: list[Span], max_snap: float = MAX_SNAP) -> tuple[float, float]:
    """Sentence edges for a requested span. Times already on a boundary, or in a
    gap between sentences, are left alone; nothing ever moves inward.

    An edge moves at most `max_snap` seconds. Whisper emits 20-40 s cues over
    near-silence ("That's a thank you" across 21 s of nothing, talk2 at 15:01);
    a sentence that starts in one has a safe cut point 20 s away, which is not a
    sentence boundary but an ASR artifact. Then the estimated speech edge is used
    if it is within reach, else the requested time stays.

    A time equal to a sentence's safe edge is left alone even when, at caption-cue
    granularity, the next sentence's estimated speech already overlaps it: the
    cue edge is the best cut available there, and it makes snapping idempotent,
    so a moments.json written by one run re-snaps to the same moments."""
    return Snapper(spans, max_snap).snap(start, end)


def pad(start: float, end: float, lead: float = LEAD_SECONDS, tail: float = TAIL_SECONDS,
        duration: float | None = None) -> tuple[float, float]:
    """Air before the first word and after the last, clamped to the source."""
    start = max(0.0, start - lead)
    end = end + tail
    if duration is not None:
        end = min(end, duration)
    return start, end


def _norm(text: str) -> str:
    return re.sub(r"[^a-z']", "", text.lower())


def _set_off(prev: Word | None, w: Word, nxt: Word | None, last: Word | None = None) -> bool:
    """`w` (or the phrase w..last) is isolated: punctuation or a pause on both sides."""
    last = last or w
    before = prev is None or prev.text.rstrip().endswith(_CLAUSE_END) or w.start - prev.end >= ISOLATION_GAP
    after = nxt is None or last.text.rstrip().endswith(_CLAUSE_END) or nxt.start - last.end >= ISOLATION_GAP
    return before and after


def filler_mask(words: list[Word]) -> list[bool]:
    """True for each word that is a filler: "um"/"uh"/... always; "like" and the
    phrase "you know" only when set off by punctuation or pauses, because
    "I like this" and "you know the answer" are content."""
    mask = [False] * len(words)
    i = 0
    while i < len(words):
        w = words[i]
        prev = words[i - 1] if i else None
        nxt = words[i + 1] if i + 1 < len(words) else None
        norm = _norm(w.text)
        if norm in _HARD_FILLERS:
            mask[i] = True
        elif norm in _SOFT_FILLERS and _set_off(prev, w, nxt):
            mask[i] = True
        elif norm == "you" and nxt is not None and _norm(nxt.text) == "know":
            after = words[i + 2] if i + 2 < len(words) else None
            if _set_off(prev, w, after, last=nxt):
                mask[i] = mask[i + 1] = True
                i += 2
                continue
        i += 1
    return mask


@dataclass(frozen=True)
class CutReport:
    segments: tuple[tuple[float, float], ...]
    fillers: int = 0  # filler words removed
    filler_seconds: float = 0.0
    silence_seconds: float = 0.0
    intact: bool = False  # a guardrail kept the moment whole
    note: str = ""

    @property
    def removed_seconds(self) -> float:
        return self.filler_seconds + self.silence_seconds


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _merge_kinds(raw: list[tuple[float, float, bool]]) -> list[tuple[float, float, bool]]:
    """Merge overlapping (a, b, has_filler) intervals; a merged group is a filler
    cut if any part of it was."""
    out: list[tuple[float, float, bool]] = []
    for a, b, f in sorted(raw):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b), out[-1][2] or f)
        else:
            out.append((a, b, f))
    return out


def _fragments(start: float, end: float, removals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    frags: list[tuple[float, float]] = []
    cur = start
    for a, b in removals:
        if a > cur:
            frags.append((cur, a))
        cur = max(cur, b)
    if cur < end:
        frags.append((cur, end))
    return frags


def tighten(
    start: float,
    end: float,
    words: list[Word],
    silences: list[tuple[float, float]],
    *,
    fillers: bool = True,
    max_silence: float = MAX_SILENCE,
    breath: float = BREATH_SECONDS,
    min_fragment: float = MIN_FRAGMENT,
    max_removed: float = MAX_REMOVED,
) -> CutReport:
    """Keep-list for one moment [start, end] (module docstring, point 2).

    `words` may be empty or estimated (`timed=False`): then only `silences`,
    which come from the audio itself (silence.py), are cut. `silences` are
    absolute (start, end) pairs; those shorter than `max_silence` are ignored.
    """
    whole = ((start, end),)
    if end <= start:
        return CutReport(whole)
    inside = [w for w in words if w.timed and w.start >= start and w.end <= end]
    mask = filler_mask(inside) if fillers and inside else [False] * len(inside)
    kept = [w for w, f in zip(inside, mask) if not f]

    raw: list[tuple[float, float, bool]] = []  # (a, b, has_filler)
    filler_count = 0
    filler_seconds = 0.0
    i = 0
    while i < len(inside):  # each run of fillers -> the whole gap between its kept neighbours
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(inside) and mask[j + 1]:
            j += 1
        a = inside[i - 1].end if i > 0 else start
        b = inside[j + 1].start if j + 1 < len(inside) else end
        raw.append((a, b, True))
        filler_count += j - i + 1
        filler_seconds += sum(w.duration for w in inside[i:j + 1])
        i = j + 1
    for s, e in silences:
        s, e = max(s, start), min(e, end)
        pieces = [(s, e)]
        for w in kept:  # never cut inside a word: carve the words out of the silence
            if w.end <= s or w.start >= e:
                continue
            carved: list[tuple[float, float]] = []
            for p, q in pieces:
                if w.end <= p or w.start >= q:
                    carved.append((p, q))
                    continue
                if w.start > p:
                    carved.append((p, w.start))
                if w.end < q:
                    carved.append((w.end, q))
            pieces = carved
        raw.extend((p, q, False) for p, q in pieces if q - p > max_silence)

    pause_air = max(breath, max_silence / 2)  # a shortened pause is still `max_silence` long

    def breathe(intervals: list[tuple[float, float, bool]]) -> list[tuple[float, float]]:
        out = []
        for a, b, has_filler in intervals:
            air = breath if has_filler else pause_air
            a2 = a if a <= start else a + air  # no air needed at the moment's own edges
            b2 = b if b >= end else b - air
            if b2 - a2 >= (MIN_CUT if has_filler else MIN_PAUSE_CUT):
                out.append((a2, b2))
        return out

    removals = breathe(_merge_kinds(raw))
    if not removals:
        return CutReport(whole)

    def has_speech(frag: tuple[float, float]) -> bool:
        return any(w.start < frag[1] and w.end > frag[0] for w in kept)

    while removals:
        frags = _fragments(start, end, removals)
        short = [f for f in frags if f[1] - f[0] < min_fragment]
        if not short:
            break
        f = min(short, key=lambda x: x[1] - x[0])
        left = next((r for r in removals if abs(r[1] - f[0]) < 1e-9), None)
        right = next((r for r in removals if abs(r[0] - f[1]) < 1e-9), None)
        if not has_speech(f) and (left or right):  # only air in there: absorb it into the cut
            keep = [r for r in removals if r is not left and r is not right]
            a = left[0] if left else f[0]
            b = right[1] if right else f[1]
            removals = _merge(keep + [(a, b)])
        else:  # speech: cancel the shorter neighbouring cut so the fragment can grow
            victim = min((r for r in (left, right) if r), key=lambda r: r[1] - r[0], default=None)
            if victim is None:
                break
            removals = [r for r in removals if r is not victim]
    while len(_fragments(start, end, removals)) > MAX_SEGMENTS:
        removals.remove(min(removals, key=lambda r: r[1] - r[0]))
    if not removals:
        return CutReport(whole)

    removed = sum(b - a for a, b in removals)
    share = removed / (end - start)
    if share > max_removed:
        return CutReport(whole, intact=True,
                         note=f"cuts would remove {share:.0%} of the moment (limit {max_removed:.0%}); kept intact "
                              "(long dead air or a quiet speaker: shorten the moment or check the audio)")
    segments = tuple(_fragments(start, end, removals))
    filler_seconds = min(filler_seconds, removed)
    return CutReport(segments, filler_count, filler_seconds, removed - filler_seconds)
