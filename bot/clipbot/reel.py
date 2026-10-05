"""Pick the moments of a long session and assemble a contract v1.2 reel plan.

Why a reel and not one clip: the product goal is a 2.5-5 minute SUMMARY of a
1-2 hour recording: several key moments in chronological order, opened by an
intro slide, each introduced by a short explainer card, closed by takeaways
(contract/README.md, "Reel"). This module owns the "which moments" question;
render/ owns pixels.

Two ways in, one way out:

- `pick_moments(cues, minutes)` is heuristic v2. It splits the session into K
  buckets so the picks SPREAD across the whole recording (a summary that only
  covers the first ten minutes is not a summary), takes the densest, most
  teachable sentence-aligned window in each bucket, drops near-duplicates, then
  adds or drops windows until the runtime, cards included, lands within +-20 %
  of the target.
- `moments_from_specs(specs, cues)` takes {start, end, title, lesson, ...}
  records from a human (`clipbot outline` skeleton) or a model (llm.py).

Both snap every edge to a sentence boundary (`snapper_for`): with word timings
`cuts.WordSnapper` puts each edge on a word edge, preferably a sentence edge,
and never inside a word; with caption cues only, `cuts.snap_outward` moves
edges outward to the cue-estimated sentence. The first reel cut people off
mid-sentence because edges landed on Meet's 4 s cue grid; the second cut into
words because cue ends of a VAD-segmented whisper file are not word ends. A
Moment's start/end are those speech edges; `cut_moments` turns each into a
keep-list with fillers and long pauses removed and adds the lead/tail air
(stopping short of the neighbouring words), and `build_reel_plan` writes the
plan. moments.json (`Moment.spec`) keeps the speech edges, so feeding it back
re-snaps to the same sentences instead of growing by one sentence per round
trip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from .captions import Cue
from .cuts import (
    EDGE_REACH,
    KEEP_PAUSE,
    LEAD_SECONDS,
    MAX_SILENCE,
    TAIL_SECONDS,
    CutReport,
    Snapper,
    Span,
    WordSnapper,
    anchor_edges,
    pad,
    sentence_spans,
    tighten,
)
from . import story
from .framing import MAX_CLOSING_CARDS, Framing
from .lessons import (  # noqa: F401 - CARD_SECONDS / clip_text / limits are re-exported for callers and tests
    CARD_SECONDS,
    CONTEXT_LIMIT,
    LESSON_LIMIT,
    LINE_LIMIT,
    TITLE_LIMIT,
    clip_text,
    closing_cards,
    opening_card,
    outro_card,
    teachable,
)
from .plan import PRESET_BOUNDS, build_plan
from .select import _STOP, _WORD, Window, clean_text, score_cue
from .speakers import parse_clock
from .summarize import _DECISION, _similar, _ts, executive_summary, score_sentence, sentences
from .words import Word, words_from_cues

INTRO_SECONDS = 4
# What the plan adds around the moments (build_reel_plan): "What you'll learn" (<= 7.5 s),
# "Takeaways" (<= 7.5 s), the outro (3 s), and lead/tail air on every clip. The picker
# budgets for them so the whole reel, not just the clips, lands in the band.
BOOKENDS_SECONDS = 18.0
PAD_SECONDS = LEAD_SECONDS + TAIL_SECONDS
SECONDS_PER_MOMENT = 35.0  # ~30 s of speech + its 3 s card + slack
MIN_MOMENTS = 2  # one clip is a clip, not a reel
TOLERANCE = 0.20  # contract/README.md: runtime within +-20 % of the target
MIN_WINDOW = 15.0
MAX_WINDOW = 60.0
MAX_GAP = 5.0  # a longer silence inside a window ends it
DUPLICATE_JACCARD = 0.5
TAKEAWAY_LIMIT = 200
TEACHABLE_WEIGHT = 0.6  # per teachable() point, on a cue's content score
MIN_MOMENT_AFTER_OVERLAP = 1.0  # a moment swallowed by its neighbour is dropped
TRANSITION_SECONDS = 0.4
AUDIO_FADE_SECONDS = 0.15

# Labels for the card's "why" line. _DECISION (summarize.py) is deliberately broad
# for scoring; the label needs the narrow version or every card says "Decision".
_DECIDE = re.compile(
    r"\b(we should|we need|let's|the goal|decid\w*|agree\w*|next step|the plan|plan is)\b", re.I
)
_DEMO = re.compile(
    r"\b(share my screen|let me show|i'll show|show you|you can see|let's run|watch this|demo|on my screen)\b",
    re.I,
)


@dataclass(frozen=True)
class Moment:
    start: float  # speech edges: sentence start / end (no lead/tail air)
    end: float
    takeaway: str  # one sentence, <= 200 chars (clip.takeaway)
    title: str  # <= 80 chars (card title when there is no lesson)
    why: str = ""  # one line for the card: "Decision · Kyle Tabor"
    lines: tuple[str, ...] = ()
    score: float = 0.0
    speaker: str | None = None
    cue_indexes: tuple[int, ...] = ()
    lesson: str = ""  # <= 80: what a viewer who missed the session learns
    context: str = ""  # <= 120: what the room already knew
    segments: tuple[tuple[float, float], ...] = ()  # keep-list after cut_moments; empty = whole
    requested: tuple[float, float] | None = None  # the span before snapping, for the report
    # contract v1.4 story fields (story.py), all validated already
    cards: tuple[dict, ...] = ()  # slides before the chapter card
    visual: tuple[tuple[str, str], ...] = ()  # (("image", path),) or (("qr", url),) on the chapter card
    overlays: tuple[dict, ...] = ()  # labels in source seconds
    layout: object = None  # None = framing's default, "full", or a pip dict

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def kept(self) -> float:
        """Seconds that reach the screen."""
        return sum(b - a for a, b in self.segments) if self.segments else self.duration

    def card(self) -> dict:
        """Chapter card. With a lesson: title = lesson, lines = [context, why];
        without: title = title, lines = lines or [why]."""
        body = list(self.lines) if self.lines else ([self.why] if self.why else [])
        if self.lesson:
            title, body = self.lesson, [self.context, *body]
        else:
            title = self.title
        lines = [clip_text(ln, LINE_LIMIT) for ln in body if ln]
        card = {"title": clip_text(title, TITLE_LIMIT), "lines": [ln for ln in lines if ln][:4], "seconds": CARD_SECONDS}
        if self.visual:
            card.update(dict(self.visual))
            card["seconds"] = story.DEFAULT_CARD_SECONDS
        return card

    def spec(self) -> dict:
        """This moment as a --moments record (moments.json round-trip)."""
        out = {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "title": self.title,
            "takeaway": self.takeaway,
            "lines": list(self.lines),
            "why": self.why,
        }
        if self.lesson:
            out["lesson"] = self.lesson
        if self.context:
            out["context"] = self.context
        if self.cards:
            out["cards"] = [dict(c) for c in self.cards]
        out.update(dict(self.visual))
        if self.overlays:
            out["overlays"] = [dict(o) for o in self.overlays]
        if self.layout is not None:
            out["layout"] = self.layout
        return out


def _strip_orphan_quotes(text: str) -> str:
    """Drop quotes that do not pair up. A quote after a space opens, one after a
    letter or punctuation closes; `work." And I asked, "Is it` has one of each but
    they are a closer with nothing open and an opener never closed."""
    keep = [True] * len(text)
    open_at: int | None = None
    for i, ch in enumerate(text):
        if ch != '"':
            continue
        before = text[i - 1] if i else " "
        after = text[i + 1] if i + 1 < len(text) else " "
        opening = before in " (" and not after.isspace()
        if opening:
            if open_at is not None:
                keep[open_at] = False  # two openers in a row: the first never closed
            open_at = i
        elif open_at is not None:
            open_at = None  # a proper pair
        else:
            keep[i] = False  # closer with nothing open
    if open_at is not None:
        keep[open_at] = False
    return "".join(ch for ch, k in zip(text, keep) if k)


def tidy_sentence(text: str) -> str:
    """Capitalize and drop orphan quotes. Splitting caption text into sentences often
    leaves `bots do the work." And I asked, "Is it right?` behind, which reads badly
    on a card."""
    text = re.sub(r"\s+", " ", _strip_orphan_quotes(clean_text(text))).strip(" ,;:")
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def runtime(moments: list[Moment]) -> float:
    """Estimated reel length: intro, bookend cards, and per clip its card, its kept
    speech and the lead/tail air. `plan_runtime` measures the real thing once the
    plan exists."""
    return INTRO_SECONDS + BOOKENDS_SECONDS + sum(CARD_SECONDS + PAD_SECONDS + m.kept for m in moments)


def target_band(minutes: float) -> tuple[float, float, float]:
    t = minutes * 60
    return t * (1 - TOLERANCE), t, t * (1 + TOLERANCE)


def content_score(cue: Cue) -> float:
    """Request-less cue score: word density and questions (select.score_cue with no
    keywords), decision language (summarize._DECISION) and teachable phrasing
    (lessons.teachable: explanations and rules up, banter and logistics down)."""
    return score_cue(cue, set()) + 0.75 * len(_DECISION.findall(cue.text)) + TEACHABLE_WEIGHT * teachable(cue.text)


def _ends_sentence(c: Cue) -> bool:
    return c.text.rstrip().endswith((".", "!", "?"))


def spans_for(cues: list[Cue], words: list[Word] | None = None) -> list[Span]:
    """Sentence spans from real word timings when there are any, else estimated
    from the cues (words.words_from_cues)."""
    return sentence_spans(words if words else words_from_cues(cues))


def snapper_for(cues: list[Cue], words: list[Word] | None = None) -> WordSnapper | Snapper:
    """How edges snap for this transcript: on word edges when whisper timed the
    words (cuts.WordSnapper), else on cue-estimated sentences (cuts.Snapper)."""
    if words and any(w.timed for w in words):
        return WordSnapper(words)
    return Snapper(spans_for(cues))


def _snapper(cues: list[Cue], spans: list[Span] | None, snapper: WordSnapper | Snapper | None) -> WordSnapper | Snapper:
    """`snapper` wins; else a cue-based one from `spans` (or from the cues)."""
    if snapper is not None:
        return snapper
    return Snapper(spans if spans is not None else spans_for(cues))


def candidate_windows(
    cues: list[Cue], lo: float, hi: float, target_len: float, spans: list[Span] | None = None,
    *, snapper: WordSnapper | Snapper | None = None,
) -> list[Window]:
    """The best window starting at each cue.

    A window is a run of consecutive cues with lo <= duration <= hi and no
    silence longer than MAX_GAP. Its edges are snapped to sentence boundaries
    first (`snapper`, or a cue-based one from `spans`), so the bounds and the
    score apply to the clip a viewer would actually see. Score =
    sum(content) / max(duration, target_len): adding a cue helps until the
    window reaches the target length, after that only if it raises the
    per-second average, so windows stop where the good part ends instead of
    always running to `hi`. Edges on a sentence boundary earn a bonus so clips
    open and close on a sentence (contract obligation 5).
    """
    base = [content_score(c) + 0.5 for c in cues]  # +0.5: speech has value even when plain
    n = len(cues)
    if snapper is None and spans:
        snapper = Snapper(spans)
    if snapper is not None:
        starts = [snapper.start(c.start) for c in cues]
        ends = [snapper.end(c.end) for c in cues]
    else:
        starts = [c.start for c in cues]
        ends = [c.end for c in cues]
    out: list[Window] = []
    for i in range(n):
        starts_clean = i == 0 or _ends_sentence(cues[i - 1]) or cues[i - 1].speaker != cues[i].speaker
        total = 0.0
        best: Window | None = None
        shortest: Window | None = None
        for j in range(i, n):
            if j > i and (cues[j].start < cues[j - 1].start or cues[j].start - cues[j - 1].end > MAX_GAP):
                break
            dur = ends[j] - starts[i]
            if dur > hi:
                break
            total += base[j]
            if dur < lo:
                continue
            s = total / max(dur, target_len)
            if starts_clean:
                s *= 1.15
            if _ends_sentence(cues[j]) or j == n - 1 or cues[j + 1].speaker != cues[j].speaker:
                s *= 1.15
            w = Window(starts[i], ends[j], s, "", tuple(range(i, j + 1)))
            if shortest is None:
                shortest = w
            if best is None or s > best.score:
                best = w
        if best is not None:
            out.append(best)
            # The shortest valid window from the same start gives the picker a way to
            # fit a tiny target once snapping has widened every window (pick_moments).
            if shortest is not None and shortest.cue_indexes != best.cue_indexes:
                out.append(shortest)
    return out


def _words(cues: list[Cue], idx: tuple[int, ...]) -> set[str]:
    return {w for i in idx for w in _WORD.findall(cues[i].text.lower()) if w not in _STOP and len(w) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def _overlaps(w: Window, picked: list[Window]) -> bool:
    return any(w.start < p.end and w.end > p.start for p in picked)


def _duplicate(w: Window, picked: list[Window], cues: list[Cue], sentences_seen: dict) -> bool:
    """Same point made twice: the windows share most content words (keyword Jaccard),
    or their takeaway sentences do. The second test matters when a repeat is padded
    with other chatter, which dilutes the Jaccard but would still put the same
    title on two cards."""
    ws = _words(cues, w.cue_indexes)

    def sentence(x: Window) -> str:
        if x.cue_indexes not in sentences_seen:
            sentences_seen[x.cue_indexes] = best_sentence([cues[i] for i in x.cue_indexes])
        return sentences_seen[x.cue_indexes]

    for p in picked:
        if _jaccard(ws, _words(cues, p.cue_indexes)) > DUPLICATE_JACCARD:
            return True
        if _similar(sentence(w), sentence(p)):
            return True
    return False


def _runtime_w(ws: list[Window]) -> float:
    return INTRO_SECONDS + BOOKENDS_SECONDS + sum(CARD_SECONDS + PAD_SECONDS + (w.end - w.start) for w in ws)


def pick_moments(
    cues: list[Cue], minutes: float, preset: str = "internal", *, spans: list[Span] | None = None,
    snapper: WordSnapper | Snapper | None = None,
) -> list[Moment]:
    """Heuristic v2 (module docstring). Chronological result, +-20 % of `minutes` when possible."""
    if not cues:
        return []
    snapper = _snapper(cues, spans, snapper)
    low, target, high = target_band(minutes)
    k = max(MIN_MOMENTS, round(target / SECONDS_PER_MOMENT))
    budget = max(target - INTRO_SECONDS - BOOKENDS_SECONDS - (CARD_SECONDS + PAD_SECONDS) * k, 0.0)
    hi = min(MAX_WINDOW, float(PRESET_BOUNDS.get(preset, (15, 120))[1]))
    lo = min(MIN_WINDOW, max(8.0, budget / k))  # tiny targets (demo: 0.5 min) cannot fit K x 15 s
    target_len = min(hi, max(lo, budget / k))
    cands = candidate_windows(cues, lo, hi, target_len, snapper=snapper)
    if not cands:
        return []
    t0 = min(c.start for c in cues)
    span = max(max(c.end for c in cues) - t0, 1e-6)
    picked: list[Window] = []
    for b in range(k):
        b0, b1 = t0 + span * b / k, t0 + span * (b + 1) / k
        pool = [w for w in cands if b0 <= w.start < b1 and not _overlaps(w, picked)]
        if pool:
            picked.append(max(pool, key=lambda w: w.score))
    # Same words in two buckets = the same point made twice; keep the stronger one.
    seen: dict = {}
    kept: list[Window] = []
    for w in sorted(picked, key=lambda w: w.score, reverse=True):
        if not _duplicate(w, kept, cues, seen):
            kept.append(w)
    picked = kept
    ranked = sorted(cands, key=lambda w: w.score, reverse=True)

    def fill() -> None:
        while _runtime_w(picked) < low:
            room = high - _runtime_w(picked)
            extra: Window | None = None
            fallback: Window | None = None
            for w in ranked:
                if _overlaps(w, picked) or _duplicate(w, picked, cues, seen):
                    continue
                if CARD_SECONDS + w.end - w.start <= room:
                    extra = w
                    break
                if fallback is None:
                    fallback = w
            extra = extra or fallback
            if extra is None:
                break
            picked.append(extra)

    fill()
    while _runtime_w(picked) > high and len(picked) > MIN_MOMENTS:
        picked.remove(min(picked, key=lambda w: w.score))
    # Tiny targets (the 72 s demo at 0.5 min): sentence snapping widens every window, so
    # even MIN_MOMENTS picks can overshoot the band. Swap the longest pick for the
    # shortest window from the same start cue, then fill the freed room again.
    shortest: dict[int, Window] = {}
    for w in cands:
        if w.cue_indexes[0] not in shortest or w.duration < shortest[w.cue_indexes[0]].duration:
            shortest[w.cue_indexes[0]] = w
    while _runtime_w(picked) > high:
        swappable = [w for w in picked if shortest[w.cue_indexes[0]].duration < w.duration]
        if not swappable:
            break
        w = max(swappable, key=lambda w: w.duration)
        picked[picked.index(w)] = shortest[w.cue_indexes[0]]
        fill()
    if len(picked) < MIN_MOMENTS:  # a reel has at least two moments, band or no band
        for w in sorted(ranked, key=lambda w: (round(w.duration), -w.score)):
            if len(picked) >= MIN_MOMENTS:
                break
            if not _overlaps(w, picked) and not _duplicate(w, picked, cues, seen):
                picked.append(w)
    picked.sort(key=lambda w: w.start)
    moments = [
        moment_from_cues(
            w.start, w.end, [cues[i] for i in w.cue_indexes], score=w.score, cue_indexes=w.cue_indexes,
            requested=(cues[w.cue_indexes[0]].start, cues[w.cue_indexes[-1]].end),
        )
        for w in picked
    ]
    return resolve_overlaps(moments)[0]


def best_sentence(cues: list[Cue]) -> str:
    """The most substantive sentence in the window (density + decision cues), cleaned."""
    sents = sentences(cues)
    if not sents:
        return ""
    best = max(sents, key=lambda s: (score_sentence(s), len(s.text)))
    return clean_text(best.text)


def classify(cues: list[Cue]) -> str | None:
    text = " ".join(c.text for c in cues)
    speakers = {c.speaker for c in cues if c.speaker}
    if "?" in text and len(speakers) >= 2:
        return "Q&A"
    if _DECIDE.search(text):
        return "Decision"
    if _DEMO.search(text):
        return "Demo"
    if teachable(text) >= 1.5:
        return "Lesson"
    return None


def dominant_speaker(cues: list[Cue]) -> str | None:
    time: dict[str, float] = {}
    for c in cues:
        if c.speaker:
            time[c.speaker] = time.get(c.speaker, 0.0) + c.duration
    return max(time, key=lambda s: time[s]) if time else None


def moment_from_cues(
    start: float,
    end: float,
    cues: list[Cue],
    *,
    title: str | None = None,
    takeaway: str | None = None,
    why: str | None = None,
    lines: tuple[str, ...] = (),
    score: float = 0.0,
    cue_indexes: tuple[int, ...] = (),
    lesson: str | None = None,
    context: str | None = None,
    requested: tuple[float, float] | None = None,
) -> Moment:
    lesson = clip_text(tidy_sentence(lesson), LESSON_LIMIT) if lesson else ""
    context = clip_text(tidy_sentence(context), CONTEXT_LIMIT) if context else ""
    takeaway = clip_text(
        tidy_sentence(takeaway or lesson or title or best_sentence(cues) or f"Moment at {_ts(start)}"), TAKEAWAY_LIMIT
    )
    if not takeaway.endswith((".", "!", "?", "…")):
        takeaway += "."
    title = clip_text(tidy_sentence(title) if title else (lesson or takeaway), TITLE_LIMIT)
    speaker = dominant_speaker(cues)
    if why is None:
        why = " · ".join(x for x in (classify(cues), speaker) if x) or "Key moment"
    return Moment(
        start, end, takeaway, title, clip_text(why, LINE_LIMIT), tuple(lines), score, speaker, tuple(cue_indexes),
        lesson, context, (), requested,
    )


def moments_from_specs(
    specs: list[dict], cues: list[Cue], *, spans: list[Span] | None = None,
    snapper: WordSnapper | Snapper | None = None, base: str | Path | None = None,
) -> list[Moment]:
    """Snap human/LLM records to sentence boundaries; keep the given order.

    start moves to the start of the sentence it falls in, end to the end of
    the sentence it falls in (`snapper`: cuts.WordSnapper with word timings,
    else cuts.Snapper over cue-estimated sentences), so the clip never opens or
    closes mid-sentence or mid-word. With cues only, a time in a gap between
    sentences stays put; with words it lands on the nearest word edge across
    the silence. `lesson`/`context` (<= 80 / <= 120) frame the moment for a
    viewer who was not there; `title`/`lines`/`why` keep working as before.
    `cards`, `image`/`qr`, `overlays` and `layout` (story.py, contract v1.4) are
    checked here; picture paths resolve against `base` (the moments file's folder).
    """
    base = Path(base) if base is not None else None
    snapper = _snapper(cues, spans, snapper)
    out: list[Moment] = []
    for k, spec in enumerate(specs, 1):
        try:
            asked_start, asked_end = parse_clock(spec["start"]), parse_clock(spec["end"])
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"moment {k}: needs start and end (seconds or h:mm:ss): {e}") from e
        if asked_end <= asked_start:
            raise ValueError(f"moment {k}: end must be after start")
        start, end = snapper.snap(asked_start, asked_end)
        if end <= start:  # words only: a request that touches no word collapses; keep what was asked
            start, end = asked_start, asked_end
        idx = tuple(i for i, c in enumerate(cues) if c.end > start and c.start < end)
        lines = spec.get("lines") or ()
        if isinstance(lines, str):
            lines = (lines,)
        out.append(
            moment_from_cues(
                start, end, [cues[i] for i in idx],
                title=spec.get("title") or None,
                takeaway=spec.get("takeaway") or None,
                why=spec.get("why") or None,
                lines=tuple(str(ln) for ln in lines)[:4],
                score=float(spec.get("score") or 0.0),
                cue_indexes=idx,
                lesson=str(spec.get("lesson") or "") or None,
                context=str(spec.get("context") or "") or None,
                requested=(asked_start, asked_end),
            )
        )
        where = f"moment {k}"
        extra = {}
        if spec.get("cards") is not None:
            extra["cards"] = story.parse_cards(spec["cards"], f"{where}.cards", base)
        visual = story.card_visual(spec, where, base)
        if visual:
            extra["visual"] = tuple(visual.items())
        if spec.get("overlays") is not None:
            extra["overlays"] = story.parse_overlays(spec["overlays"], f"{where}.overlays", (asked_start, asked_end))
        if spec.get("layout") is not None:
            lay = spec["layout"]
            extra["layout"] = lay if lay in ("full", "pip") else story.parse_layout(lay, f"{where}.layout")
            if extra["layout"] is None:  # {"kind": "full"}
                extra["layout"] = "full"
        if extra:
            out[-1] = replace(out[-1], **extra)
    return out


def resolve_overlaps(moments: list[Moment], duration: float | None = None) -> tuple[list[Moment], list[str]]:
    """Snapping two adjacent moments can make them share a sentence. The later
    one then starts where the earlier one ends (still a sentence boundary), or is
    dropped when nothing of it is left. With `duration`, a moment past the end of
    the source is dropped and one running over it is clamped (a moments file
    written for the full recording, run against a 10-minute excerpt). Order is
    preserved; notes explain."""
    order = sorted(range(len(moments)), key=lambda i: moments[i].start)
    fixed: dict[int, Moment | None] = {i: m for i, m in enumerate(moments)}
    notes: list[str] = []
    prev: Moment | None = None
    for i in order:
        m = fixed[i]
        assert m is not None
        if duration is not None:
            if m.start >= duration - MIN_MOMENT_AFTER_OVERLAP:
                notes.append(f"moment {i + 1} ({_ts(m.start)}-{_ts(m.end)}) starts after the source ends ({_ts(duration)}); dropped")
                fixed[i] = None
                continue
            if m.end > duration:
                notes.append(f"moment {i + 1} ran past the end of the source; now ends at {_ts(duration)}")
                m = replace(m, end=duration)
                fixed[i] = m
        if prev is not None and m.start < prev.end:
            if m.end - prev.end < MIN_MOMENT_AFTER_OVERLAP:
                notes.append(f"moment {i + 1} ({_ts(m.start)}-{_ts(m.end)}) lies inside its neighbour after snapping; dropped")
                fixed[i] = None
                continue
            notes.append(f"moment {i + 1} overlapped its neighbour after snapping; now starts at {_ts(prev.end)}")
            m = replace(m, start=prev.end)
            fixed[i] = m
        prev = m
    return [fixed[i] for i in range(len(moments)) if fixed[i] is not None], notes


def fit_moments(candidates: list[Moment], minutes: float) -> list[Moment]:
    """Best-scoring, non-overlapping candidates until the runtime reaches the target
    without passing the band's top; chronological. Used for model proposals, which
    arrive unranked per chunk and usually add up to more than the target."""
    _, target, high = target_band(minutes)
    picked: list[Moment] = []
    for m in sorted(candidates, key=lambda m: m.score, reverse=True):
        if runtime(picked) >= target:
            break
        if any(m.start < p.end and m.end > p.start for p in picked):
            continue
        if runtime(picked) + CARD_SECONDS + m.duration <= high:
            picked.append(m)
    picked.sort(key=lambda m: m.start)
    return picked


def detection_spans(
    moments: list[Moment], *, lead: float = LEAD_SECONDS, tail: float = TAIL_SECONDS, duration: float | None = None
) -> list[tuple[float, float]]:
    """Where silence.py should look for each moment: the padded span plus
    cuts.EDGE_REACH and the air on each side, so the pause beside a first or last
    word that whisper timed late (or early) is seen whole (cuts.anchor_edges)."""
    margin = EDGE_REACH + max(lead, tail)
    return [pad(m.start, m.end, lead + margin, tail + margin, duration) for m in moments]


def segments_for(
    m: Moment, *, lead: float = LEAD_SECONDS, tail: float = TAIL_SECONDS, duration: float | None = None
) -> tuple[tuple[float, float], ...]:
    """What goes in the plan: the keep-list from cut_moments, or the whole moment
    with lead/tail air."""
    return m.segments or (pad(m.start, m.end, lead, tail, duration),)


def cut_moments(
    moments: list[Moment],
    words: list[Word],
    silences: list[list[tuple[float, float]] | None] | None,
    *,
    lead: float = LEAD_SECONDS,
    tail: float = TAIL_SECONDS,
    duration: float | None = None,
    fillers: bool = True,
    pauses: bool = True,
    max_silence: float = MAX_SILENCE,
    keep_pause: float = KEEP_PAUSE,
) -> tuple[list[Moment], list[CutReport]]:
    """Lead/tail air, then filler + pause removal for every moment (cuts.tighten);
    `silences[i]` is what silence.py found in and around `moments[i]` (every
    silence of at least cuts.MIN_PAUSE, out to `detection_spans`), `None` (for
    one moment or for all) when the audio was not checked.

    With timed words a moment's speech edges become its first and last word
    edges (a moment that `resolve_overlaps` started in the pause after its
    neighbour now starts on its first word), and the air stops short of the
    neighbouring words (cuts.WordSnapper.pad), so no tail ever runs into the
    next word. With silence data the padded edges then move onto the pauses the
    audio shows beside the first and last word (cuts.anchor_edges): whisper's
    word edge is an estimate and a clip that opened on it opened mid-word.
    Returns the moments with `start`/`end`/`segments` updated and one report
    each. `fillers=False, pauses=False` only pads (and anchors)."""
    snapper = WordSnapper(words) if any(w.timed for w in words) else None
    out: list[Moment] = []
    reports: list[CutReport] = []
    if silences is None:
        silences = [None] * len(moments)
    for m, sil in zip(moments, silences):
        start, end = m.start, m.end
        if snapper is not None:
            start, end = snapper.speech_edges(start, end)
            s, e = snapper.pad(start, end, lead, tail, duration)
            if sil is not None:
                prev, nxt = snapper.neighbours(start, end)
                s, e = anchor_edges(s, e, start, end, sil, prev=prev, nxt=nxt, lead=lead, tail=tail, duration=duration)
        else:
            s, e = pad(start, end, lead, tail, duration)
        rep = tighten(s, e, words, sil, fillers=fillers, pauses=pauses, max_silence=max_silence, keep_pause=keep_pause,
                      lead=lead, tail=tail)
        out.append(replace(m, start=start, end=end, segments=rep.segments))
        reports.append(rep)
    return out, reports


def lesson_lines(moments: list[Moment]) -> list[str]:
    """One line per chapter for the opening card: the lesson, else the title."""
    return [m.lesson or m.title for m in moments]


def takeaway_lines(
    moments: list[Moment], cues: list[Cue], *, given: list[str] | None = None, hand_picked: bool = False
) -> list[str]:
    """Closing-card lines, best source first: `--takeaways` lines > the moments'
    `lesson` fields > (hand-picked or model moments) their titles > the
    extractive executive summary."""
    if given:
        lines = [ln.strip() for ln in given if ln.strip()]
        if lines:
            return lines
    lessons = [m.lesson for m in moments if m.lesson]
    if lessons:
        return lessons
    if hand_picked:
        return [m.title for m in moments]
    return [tidy_sentence(s.text) for s in executive_summary(cues, n=4)]


def plan_runtime(plan: dict) -> float:
    """Seconds on screen: intro + opening + (card + kept) per clip + closing + outro.
    Every card and every kept second counts; the intro line and the `reel:` line
    both show this number, rounded to the second (`_ts` alone floors, which read
    "4:56" against a 5:02 file once)."""
    reel = plan.get("output", {}).get("reel") or {}
    total = 0.0
    for card in (reel.get("intro"), reel.get("outro"), *reel.get("opening", []), *reel.get("closing", [])):
        if card:
            total += float(card.get("seconds", CARD_SECONDS))
    for clip in plan["clips"]:
        for card in (*clip.get("cards", []), clip.get("card")):
            if card:
                total += float(card.get("seconds", CARD_SECONDS))
        total += sum(s["end"] - s["start"] for s in clip["segments"])
    return total


def build_reel_plan(
    source,
    moments: list[Moment],
    out_dir: str,
    *,
    title: str,
    date: str,
    preset: str = "internal",
    captions_kind: str = "embedded",
    srt_path: str | None = None,
    summary_path: str | None = None,
    lead: float = LEAD_SECONDS,
    tail: float = TAIL_SECONDS,
    takeaways: list[str] | None = None,
    transition: str = "dip",
    music: str | None = None,
    framing: Framing | None = None,
) -> dict:
    """A v1 plan plus output.reel (contract v1.2): intro, "What you'll learn",
    one card per clip, "Takeaways" cards, a sign-off outro, `dip` transitions
    and clip audio fades. `music` is a repo-root-relative or absolute path the
    caller has the rights to (contract/README.md, v1.2).

    `framing` (framing.py) overrides whatever it says: title, date, the opening
    lines, the takeaways, the outro, card seconds, music gain and fade, the
    transition. Every card's seconds are final before the runtime line on the
    intro is computed, so the intro never disagrees with the file the renderer
    writes (the third reel said 4:50 and ran 4:55 because the cards were
    edited after the plan was built)."""
    fr = framing or Framing()
    title = fr.title or title
    date = fr.date or date
    duration = float(getattr(source, "duration_seconds", 0.0) or 0.0) or None
    windows = [Window(m.start, m.end, m.score, m.takeaway, m.cue_indexes) for m in moments]
    plan = build_plan(
        source, windows, out_dir, preset=preset, captions_kind=captions_kind,
        srt_path=srt_path, summary_path=summary_path,
    )
    for clip, m in zip(plan["clips"], moments):
        clip["segments"] = [
            {"start": round(a, 3), "end": round(b, 3)} for a, b in segments_for(m, lead=lead, tail=tail, duration=duration)
        ]
        clip["card"] = m.card()
        if m.cards:
            clip["cards"] = [dict(c) for c in m.cards]
        if m.overlays:
            kept = [(s["start"], s["end"]) for s in clip["segments"]]
            overlays = story.clip_overlays(m.overlays, kept)
            if overlays:
                clip["overlays"] = overlays
        if m.layout == "pip":
            layout = fr.layout
            if layout is None:
                raise ValueError(f'moment "{m.title}": layout "pip" needs framing.layout with the screen region')
        elif m.layout == "full":
            layout = None
        else:
            layout = m.layout if m.layout is not None else fr.layout
        if layout:
            clip["layout"] = dict(layout)
    reel: dict = {
        "filename": "reel.mp4",
        "intro": {"title": clip_text(title, TITLE_LIMIT) or "Summary", "lines": [], "seconds": INTRO_SECONDS},
        "chapter_cards": "all",
        "outro": fr.outro_card() or outro_card(),
        "transition": {"kind": fr.transition_kind or transition, "seconds": fr.transition_seconds or TRANSITION_SECONDS},
        "audio_fade_seconds": fr.audio_fade_seconds if fr.audio_fade_seconds is not None else AUDIO_FADE_SECONDS,
    }
    if fr.outro_seconds is not None:
        reel["outro"]["seconds"] = fr.outro_seconds
    opening = opening_card(list(fr.what_you_will_learn) or lesson_lines(moments), title_hint=title)
    if opening:
        if fr.opening_seconds is not None:
            opening["seconds"] = fr.opening_seconds
        reel["opening"] = [opening]
    closing = closing_cards(list(fr.takeaways), max_cards=MAX_CLOSING_CARDS) if fr.takeaways else closing_cards(takeaways or [])
    if closing:
        for card in closing:
            if fr.closing_seconds is not None:
                card["seconds"] = fr.closing_seconds
        reel["closing"] = closing
    if music:
        reel["music"] = {"path": music}
        if fr.music_gain_db is not None:
            reel["music"]["gain_db"] = fr.music_gain_db
        if fr.music_fade_seconds is not None:
            reel["music"]["fade_seconds"] = fr.music_fade_seconds
    plan["output"]["reel"] = reel
    total = plan_runtime(plan)  # every card now has its final seconds; nothing below changes the length
    reel["intro"]["lines"] = [f"Recorded {date}", f"{len(moments)} moments · {_ts(round(total))}"]
    return plan
