"""Where a cut may land: word edges, sentence boundaries and shortened pauses.

Kyle's review of the first reel: "You awkwardly cut off some of the segments
when someone was still talking" and "cut out ums, ahs, and cut out the long
silence segments". An independent check of the second reel found both faults
again in a subtler form: clip ends decided from caption cues landed inside a
word ("...before it merges. [1.6 s] So it|" cut the "it"), and 17 pauses over
0.9 s survived because a pause was only cut when the cut saved enough and left
1.5 s of speech on both sides. Everything here is a pure function over word
timings (words.py), so it is testable with synthetic data:

1. Boundaries. With timed words (`WordSnapper`) a moment's END is the end of a
   word, preferably a sentence end: it extends forward to the first word that
   ends with . ? ! or is followed by a pause of `SENTENCE_PAUSE`, at most
   `MAX_EXTEND_END` later; past that cap it settles on the last word end that is
   followed by a breath (`BREATH_PAUSE`). A START is a word start, preferably
   the start of the sentence, searched back at most `MAX_EXTEND_START`. An edge
   inside a word moves outward to include the word; an edge in the pause after
   a sentence moves back across silence to the word edge. Speech is never lost:
   the result contains every word the request touched. `WordSnapper.pad` adds
   the lead/tail air but stops `WORD_MARGIN` short of the neighbouring word,
   which is what keeps the "So it" out. With caption cues only (`sentence_spans`,
   `Snapper`, `snap_outward`) sentences are estimated from the cue text and the
   cue edges stay the only safe cut points; that path is unchanged.

2. Pauses and fillers. `tighten` turns one padded moment into a keep-list.
   Filler words ("um", "uh", an isolated "like" / "you know") are dropped with
   `breath` of air on each side, but never when that leaves a fragment shorter
   than `min_fragment`: a stutter of joins is worse than an "um". A pause is
   where the AUDIO is silent (ffmpeg's silencedetect, silence.py), not where
   whisper has no word: every silent stretch longer than `max_silence` between
   two pieces of speech is shortened to `keep_pause` by cutting its middle, and
   at the moment's own edges silence beyond `lead` / `tail` of air is trimmed.
   That removes no speech, so it needs no fragment rule. Both sources are
   needed: whisper emits zero-length words (talk2 has 69; an "Okay," at 50:50
   sits in a 0.78 s gap the audio shows is not silent at all) and misses
   speech, so a word gap alone can hold speech; and the detector alone cannot
   tell a soft word edge from a pause, so a cut stays `keep_pause / 2` inside
   the silence. What is neither word nor silence (a laugh, applause,
   cross-talk, a breath) stays. Without silence data (`silences=None`) the
   word gaps are trusted on their own. If the cuts that could hold speech
   (filler cuts, and pause cuts made without silence data) would remove more
   than `max_removed` of the moment it is left intact with a note: the guard
   against a stretch whisper did not transcribe; a pause the audio confirmed is
   exempt, since dead air is what the cut is for. Without timed words the pauses come
   from the silences alone and are shortened to `max_silence` with more air on
   each side, because a silence edge is less trustworthy than a word edge.

3. Why a cut may land inside a whisper word span (`speech_spans`). Whisper's
   span is an estimate; the silence detector is the ground truth for where
   there is sound. The third reel kept ten pauses over 0.9 s although no word
   gap was left uncut: whisper had stretched "very" over 1.9 s with 0.84 s of
   dead air inside, ended "for" 1.1 s after the sound stopped (so the splice
   "for | us" held 1.39 s of silence), and put "But well," 0.8 s early, inside
   -80 dB silence, so the pause before it looked like speech. The unit is a
   run of words that abut (whisper splits continuous speech into words at
   arbitrary points, so a run, not a word, is the span whose edges are in
   question). A run gives up a silence inside it when, on every side where the
   run continues past that silence, it still has at least `VOICED_MARGIN` of
   sound: then the silence is a pause the alignment papered over, or a
   stretched edge, and it is shortened like any other. A run that lies wholly
   (or all but a sliver) in silence is left alone: nobody knows where its words
   are (a quiet speaker, a hallucination), and protecting it costs one pause.
   Voiced audio is never cut: every cut lies inside a detected silence with
   `keep_pause / 2` (or `lead` / `tail` at the moment's edges) of that silence
   kept on each side, which covers a word edge that fades below the detector's
   threshold. Filler cuts measure their breath from the same acoustic edges.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from .words import Word

LEAD_SECONDS = 0.15  # before the first word of a moment
TAIL_SECONDS = 0.3  # after its last word
BREATH_SECONDS = 0.15  # air kept on each side of a filler cut
MAX_SILENCE = 0.7  # pauses longer than this are shortened
KEEP_PAUSE = 0.35  # ...to this: a beat, not a hole
MIN_FRAGMENT = 1.5  # a kept span shorter than this is merged with a neighbour (filler cuts)
MAX_REMOVED = 0.40  # never take more than this share out of one moment
MIN_CUT = 0.2  # a filler cut shorter than this is not worth a join
MIN_PAUSE_CUT = 0.4  # silence path only: a pause is shortened when that saves this much
MAX_SEGMENTS = 20  # contract: clips[].segments maxItems
SENTENCE_GAP = 1.5  # cue-estimated words: a longer gap ends a sentence
MAX_SENTENCE = 20.0
MAX_SNAP = 12.0  # an edge never moves further than this (snap_outward)
ISOLATION_GAP = 0.25  # a pause this long on both sides makes "like" a filler
# Word-level snapping (WordSnapper). Real word timings resolve pauses finely enough that a
# 0.45 s gap is a sentence boundary for cutting purposes even without punctuation.
SENTENCE_PAUSE = 0.45
BREATH_PAUSE = 0.25  # fallback cut point when no sentence end is within reach
MAX_EXTEND_END = 8.0  # an end moves at most this far forward to find a sentence end
MAX_EXTEND_START = 6.0  # a start moves at most this far back to find a sentence start
WORD_MARGIN = 0.05  # lead/tail air stops this far short of a neighbouring word
# Acoustic speech edges (speech_spans). Words closer than RUN_GAP are one whisper span; a run
# must keep VOICED_MARGIN of sound beside a silence for that silence to count as a pause.
RUN_GAP = 0.05
VOICED_MARGIN = 0.12
_EPS = 1e-6

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


class WordSnapper:
    """Snap times to word edges, preferring sentence edges (module docstring, 1).

    Only timed words count; estimated ones (words.words_from_cues) belong to
    `Snapper`. Built once per transcript; every lookup is a bisect. The same
    interface as `Snapper` (`start`, `end`, `snap`) so reel.py can use either.

    Fixed points, so a moments.json written by one run re-snaps to itself: a
    start that is a sentence start and an end that is a sentence end stay put.
    An end that came from the cap fallback (a breath, no sentence end within
    reach) may move on to a sentence end on the next run when one lies within
    reach of the new point; that is one step and then it is stable.
    """

    def __init__(
        self,
        words: list[Word],
        *,
        max_end: float = MAX_EXTEND_END,
        max_start: float = MAX_EXTEND_START,
        sentence_pause: float = SENTENCE_PAUSE,
        breath: float = BREATH_PAUSE,
    ):
        self.words = sorted((w for w in words if w.timed), key=lambda w: (w.start, w.end))
        self._starts = [w.start for w in self.words]
        self.max_end = max_end
        self.max_start = max_start
        self.sentence_pause = sentence_pause
        self.breath = breath

    # -- sentence structure -------------------------------------------------
    def _gap_after(self, k: int) -> float:
        ws = self.words
        return float("inf") if k + 1 >= len(ws) else ws[k + 1].start - ws[k].end

    def _gap_before(self, k: int) -> float:
        ws = self.words
        return float("inf") if k == 0 else ws[k].start - ws[k - 1].end

    def _ends_sentence(self, k: int) -> bool:
        ws = self.words
        if k + 1 >= len(ws) or _TERMINAL.search(ws[k].text):
            return True
        return self._gap_after(k) >= self.sentence_pause - _EPS or not _same_speaker(ws[k], ws[k + 1])

    def _starts_sentence(self, k: int) -> bool:
        return k == 0 or self._ends_sentence(k - 1)

    # -- edges ---------------------------------------------------------------
    def end(self, t: float) -> float:
        """A word end at or after the speech `t` touches, preferably a sentence end.

        Inside a word: that word and on to its sentence end. At a word end, or in
        the pause after a sentence: back to that word end (across silence only).
        At a word end mid-sentence, or in a short gap: on to the sentence end.
        """
        ws = self.words
        if not ws:
            return t
        i = bisect_right(self._starts, t) - 1  # last word starting at or before t
        if i >= 0 and ws[i].start >= t - _EPS:  # exactly a word start: the end of what came before
            i -= 1
        if i >= 0 and ws[i].end > t + _EPS:  # inside word i
            return self._extend(i, t)
        if i >= 0 and self._ends_sentence(i):  # at, or in the pause after, a sentence end
            return ws[i].end
        j = i + 1  # mid-sentence (i < 0: before the first word)
        return self._extend(j, t) if j < len(ws) else t

    def _extend(self, j: int, t: float) -> float:
        ws = self.words
        limit = t + self.max_end
        if ws[j].start > limit:  # only before the first word: nothing to end on within reach
            return t
        breath = None
        k = j
        while k < len(ws) and ws[k].end <= limit + _EPS:
            if self._ends_sentence(k):
                return ws[k].end
            if self._gap_after(k) >= self.breath - _EPS:
                breath = ws[k].end
            k += 1
        if breath is not None:
            return breath
        return ws[max(j, k - 1)].end  # the last word end within the cap; never inside a word

    def start(self, t: float) -> float:
        """A word start at or before the speech `t` touches, preferably a sentence start.

        Inside a word, or at a word end mid-sentence: back to the start of that
        sentence. In the pause after a sentence, or before the first word: on to
        the next word (across silence only).
        """
        ws = self.words
        if not ws:
            return t
        i = bisect_right(self._starts, t) - 1  # last word starting at or before t
        if i >= 0 and (ws[i].end > t + _EPS or not self._ends_sentence(i)):
            return self._retreat(i, t)
        j = i + 1
        return ws[j].start if j < len(ws) else t

    def _retreat(self, j: int, t: float) -> float:
        ws = self.words
        floor = t - self.max_start
        breath = None
        k = j
        while not self._starts_sentence(k):
            if self._gap_before(k) >= self.breath - _EPS:
                breath = ws[k].start
            if ws[k - 1].start < floor - _EPS:  # the sentence start is out of reach
                return breath if breath is not None else ws[j].start
            k -= 1
        return ws[k].start

    def snap(self, start: float, end: float) -> tuple[float, float]:
        return self.start(start), self.end(end)

    # -- around the edges ----------------------------------------------------
    def speech_edges(self, start: float, end: float) -> tuple[float, float]:
        """The first and last word edges inside [start, end]. A word partly
        inside counts whole; silence at the edges is dropped. Unchanged when no
        word overlaps the span (a moment whisper has nothing for)."""
        ws = self.words
        i = bisect_right(self._starts, start) - 1
        if i < 0 or ws[i].end <= start + _EPS:
            i += 1
        j = bisect_left(self._starts, end) - 1  # last word starting before end
        if i > j:
            return start, end
        return ws[i].start, ws[j].end

    def neighbours(self, start: float, end: float) -> tuple[Word | None, Word | None]:
        """The last word ending at or before `start` and the first starting at or after `end`."""
        ws = self.words
        i = bisect_right(self._starts, start + _EPS) - 1
        while i >= 0 and ws[i].end > start + _EPS:
            i -= 1
        j = bisect_left(self._starts, end - _EPS)
        return (ws[i] if i >= 0 else None), (ws[j] if j < len(ws) else None)

    def pad(self, start: float, end: float, lead: float = LEAD_SECONDS, tail: float = TAIL_SECONDS,
            duration: float | None = None) -> tuple[float, float]:
        """`pad`, but the air stops `WORD_MARGIN` short of the neighbouring words,
        so a tail never runs into the next word and a lead never clips the end of
        the previous one. Words that abut leave no room: the cut is the word edge."""
        prev, nxt = self.neighbours(start, end)
        s, e = pad(start, end, lead, tail, duration)
        if prev is not None:
            s = min(start, max(s, prev.end + WORD_MARGIN))
        if nxt is not None:
            e = max(end, min(e, nxt.start - WORD_MARGIN))
        return s, e


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
    silence_seconds: float = 0.0  # pauses shortened (word gaps, or detected silence without words)
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


def _overlap_seconds(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(e, y) - max(s, x)) for s, e in a for x, y in b)


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


def _runs(inside: list[Word]) -> list[tuple[float, float]]:
    """Maximal spans of words that abut (gap <= RUN_GAP), in order."""
    out: list[tuple[float, float]] = []
    for w in inside:
        if out and w.start - out[-1][1] <= RUN_GAP + _EPS:
            out[-1] = (out[-1][0], max(out[-1][1], w.end))
        else:
            out.append((w.start, w.end))
    return out


def _voiced(a: float, b: float, silences: list[tuple[float, float]]) -> float:
    """Seconds of [a, b] that no silence covers."""
    covered = sum(max(0.0, min(e, b) - max(s, a)) for s, e in silences)
    return max(0.0, b - a - covered)


def speech_spans(inside: list[Word], silences: list[tuple[float, float]] | None,
                 margin: float = VOICED_MARGIN) -> list[tuple[float, float]]:
    """Where the speech is (module docstring, 3): the runs of abutting words, less
    the detected silences inside a run that the run can give up. `inside` is
    sorted by start. `silences=None` (audio not checked) or empty: the runs as
    whisper timed them."""
    runs = _runs(inside)
    if not silences:
        return runs
    sil = _merge(list(silences))
    out: list[tuple[float, float]] = []
    for a, b in runs:
        holes: list[tuple[float, float]] = []
        for s, e in sil:
            s, e = max(s, a), min(e, b)
            if e - s <= _EPS:
                continue
            if s <= a + _EPS and e >= b - _EPS:
                continue  # the whole run lies in silence: whisper put words where the audio has none
            left_ok = s <= a + _EPS or _voiced(a, s, sil) >= margin - _EPS
            right_ok = e >= b - _EPS or _voiced(e, b, sil) >= margin - _EPS
            if left_ok and right_ok:
                holes.append((s, e))
        out.extend(_fragments(a, b, _merge(holes)))
    return out


def _acoustic_edges(inside: list[Word], speech: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Per word, the outer edges of its span that `speech` still covers; a word
    that has no speech left (a run whisper misplaced, see `speech_spans`) keeps
    its own span, so a filler cut beside it measures its breath the old way."""
    out: list[tuple[float, float]] = []
    for w in inside:
        hits = [(max(s, w.start), min(e, w.end)) for s, e in speech if min(e, w.end) - max(s, w.start) > _EPS]
        out.append((min(h[0] for h in hits), max(h[1] for h in hits)) if hits else (w.start, w.end))
    return out


def _filler_cuts(inside: list[Word], mask: list[bool], start: float, end: float, breath: float,
                 edges: list[tuple[float, float]] | None = None) -> list[tuple[float, float]]:
    """Each run of fillers becomes the whole gap between its kept neighbours, less
    `breath` of air on each side; no air is needed at the moment's own edges.
    `edges` (from `_acoustic_edges`) are the neighbours' speech edges; without
    them the word spans are used."""
    edges = edges or [(w.start, w.end) for w in inside]
    raw: list[tuple[float, float]] = []
    i = 0
    while i < len(inside):
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(inside) and mask[j + 1]:
            j += 1
        a = edges[i - 1][1] if i > 0 else start
        b = edges[j + 1][0] if j + 1 < len(inside) else end
        a = a if a <= start else a + breath
        b = b if b >= end else b - breath
        if b - a >= MIN_CUT:
            raw.append((a, b))
        i = j + 1
    return _merge(raw)


def _silence_cuts(silences: list[tuple[float, float]], start: float, end: float, max_silence: float,
                  breath: float) -> list[tuple[float, float]]:
    """Silence path (no timed words): a detected silence longer than `max_silence` is
    shortened to `max_silence`, split as air on both sides, when that saves MIN_PAUSE_CUT."""
    air = max(breath, max_silence / 2)
    out: list[tuple[float, float]] = []
    for s, e in silences:
        s, e = max(s, start), min(e, end)
        if e - s <= max_silence:
            continue
        a = s if s <= start else s + air
        b = e if e >= end else e - air
        if b - a >= MIN_PAUSE_CUT:
            out.append((a, b))
    return _merge(out)


def _touches(cut: tuple[float, float], frag: tuple[float, float]) -> bool:
    return abs(cut[1] - frag[0]) < 1e-9 or abs(cut[0] - frag[1]) < 1e-9


def _absorb(cuts: list[tuple[float, float]], frag: tuple[float, float]) -> list[tuple[float, float]]:
    """Extend the cuts that border `frag` across it."""
    return [
        (c[0], frag[1]) if abs(c[1] - frag[0]) < 1e-9 else (frag[0], c[1]) if abs(c[0] - frag[1]) < 1e-9 else c
        for c in cuts
    ]


def _protect_fragments(start: float, end: float, free: list[tuple[float, float]], fixed: list[tuple[float, float]],
                       speech: list[tuple[float, float]], min_fragment: float) -> list[tuple[float, float]]:
    """No kept fragment shorter than `min_fragment` where a cut can be helped.

    A fragment of pure air between cuts is absorbed into them. One with `speech`
    (intervals) grows by cancelling the shorter adjacent `free` cut (a filler
    cut), but only when that lengthens it by more than a hair: `fixed` cuts
    (shortened pauses) are never cancelled because they remove no speech, and a
    filler cut that a pause cut backs would, if cancelled, leave the filler
    standing as an island between two joins, which is worse than either cut
    alone."""

    def has_speech(frag: tuple[float, float]) -> bool:
        return any(s < frag[1] and e > frag[0] for s, e in speech)

    free = list(free)
    fixed = _merge(list(fixed))
    accepted: list[tuple[float, float]] = []  # short fragments nothing can be done about
    while True:
        removals = _merge(free + fixed)
        frags = _fragments(start, end, removals)
        short = sorted((f for f in frags if f[1] - f[0] < min_fragment and f not in accepted), key=lambda x: x[1] - x[0])
        changed = False
        for f in short:
            if not has_speech(f):
                if any(_touches(r, f) for r in removals):
                    free, fixed = _absorb(free, f), _merge(_absorb(fixed, f))
                    changed = True
                    break
            else:
                adjacent = [c for c in free if _touches(c, f)]
                if adjacent:
                    victim = min(adjacent, key=lambda c: c[1] - c[0])
                    trial = _fragments(start, end, _merge([c for c in free if c is not victim] + fixed))
                    grown = any(g[0] <= f[0] + 1e-9 and g[1] >= f[1] - 1e-9 and g[1] - g[0] > f[1] - f[0] + MIN_CUT / 2
                                for g in trial)
                    if grown:
                        free.remove(victim)
                        changed = True
                        break
            accepted.append(f)
        if not changed:
            return _merge(free + fixed)


def _pause_cuts(speech: list[tuple[float, float]], silences: list[tuple[float, float]] | None, start: float,
                end: float, max_silence: float, keep_pause: float, lead: float, tail: float) -> list[tuple[float, float]]:
    """Word path: the middle of every silent stretch longer than `max_silence` between
    two pieces of `speech`, leaving `keep_pause`; at the moment's own edges, the
    silence beyond `lead` (before the first speech) or `tail` (after the last). A
    stretch is the whole gap when the audio was not checked (`silences is None`),
    else the gap's overlap with each detected silence, so a cut never crosses
    sound and never reaches into speech (module docstring, 2 and 3)."""
    half = keep_pause / 2
    out: list[tuple[float, float]] = []
    for g0, g1 in _fragments(start, end, speech):
        if silences is None:
            stretches = [(g0, g1)]
        else:
            stretches = [(max(s, g0), min(e, g1)) for s, e in silences if min(e, g1) > max(s, g0)]
        for s, e in stretches:
            at_head, at_tail = s <= start + _EPS, e >= end - _EPS
            if at_head and at_tail:
                continue  # the whole moment is silent: the removal cap's problem, not ours
            if at_head:  # trim the opening to `lead` before the speech (or `half` before other sound)
                a, b = start, e - (lead if e >= g1 - _EPS else half)
            elif at_tail:
                a, b = s + (tail if s <= g0 + _EPS else half), end
            elif e - s > max_silence + _EPS:
                a, b = s + half, e - half
            else:
                continue
            if b - a > _EPS:
                out.append((a, b))
    return _merge(out)


def tighten(
    start: float,
    end: float,
    words: list[Word],
    silences: list[tuple[float, float]] | None,
    *,
    fillers: bool = True,
    pauses: bool = True,
    max_silence: float = MAX_SILENCE,
    keep_pause: float = KEEP_PAUSE,
    breath: float = BREATH_SECONDS,
    min_fragment: float = MIN_FRAGMENT,
    max_removed: float = MAX_REMOVED,
    lead: float = LEAD_SECONDS,
    tail: float = TAIL_SECONDS,
) -> CutReport:
    """Keep-list for one padded moment [start, end] (module docstring, 2 and 3).

    `silences` are absolute (start, end) pairs from silence.py for this moment;
    `None` means the audio was not checked. With timed words inside the moment
    the speech is where the words are, less the silences the audio finds inside
    them (`speech_spans`); a pause is a silence between two pieces of speech
    (or the bare word gap when there is no silence data), and `lead` / `tail`
    say how much silence the moment's own edges keep. Without timed words the
    silences are the only pauses known and are cut the old way. `fillers=False`
    keeps every word; `pauses=False` keeps every gap.
    """
    whole = ((start, end),)
    if end <= start:
        return CutReport(whole)
    inside = sorted((w for w in words if w.timed and w.start >= start - _EPS and w.end <= end + _EPS),
                    key=lambda w: (w.start, w.end))
    mask = filler_mask(inside) if fillers and inside else [False] * len(inside)
    speech = speech_spans(inside, silences if pauses else None)
    edges = _acoustic_edges(inside, speech)
    kept = [edges[i] for i, f in enumerate(mask) if not f]

    free = _filler_cuts(inside, mask, start, end, breath, edges) if fillers else []  # may be cancelled for a fragment
    gap_cuts: list[tuple[float, float]] = []  # never cancelled: no speech in them
    if pauses and inside:
        gap_cuts = _pause_cuts(speech, silences, start, end, max_silence, keep_pause, lead, tail)
    elif pauses:
        free += _silence_cuts(silences or [], start, end, max_silence, breath)
    removals = _protect_fragments(start, end, free, gap_cuts, kept, min_fragment)
    if not removals:
        return CutReport(whole)
    while len(_fragments(start, end, removals)) > MAX_SEGMENTS:
        removals.remove(min(removals, key=lambda r: r[1] - r[0]))

    removed = sum(b - a for a, b in removals)
    # The cap guards speech: a filler cut, or a pause cut made on word gaps alone, may hold a stretch
    # whisper did not transcribe; a pause cut the audio confirmed holds nothing but silence and is exempt.
    risky = removed if silences is None else _overlap_seconds(removals, _merge(free))
    share = risky / (end - start)
    if share > max_removed:
        return CutReport(whole, intact=True,
                         note=f"cuts would remove {share:.0%} of the moment (limit {max_removed:.0%}); kept intact "
                              "(long dead air, or speech whisper did not transcribe: shorten the moment or check the audio)")
    gone = [w for w, f in zip(inside, mask) if f and any(a < (w.start + w.end) / 2 < b for a, b in removals)]
    filler_seconds = min(sum(w.duration for w in gone), removed)
    return CutReport(tuple(_fragments(start, end, removals)), len(gone), filler_seconds, removed - filler_seconds)
