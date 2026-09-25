"""Pick the moments of a long session and assemble a contract v1.1 reel plan.

Why a reel and not one clip: the product goal is a 2.5-5 minute SUMMARY of a
1-2 hour recording: several key moments in chronological order, opened by an
intro slide, each introduced by a short explainer card (contract/README.md,
"Reel"). This module owns the "which moments" question; render/ owns pixels.

Two ways in, one way out:

- `pick_moments(cues, minutes)` is heuristic v2. It splits the session into K
  buckets so the picks SPREAD across the whole recording (a summary that only
  covers the first ten minutes is not a summary), takes the densest
  sentence-aligned window in each bucket, drops near-duplicates, then adds or
  drops windows until the runtime, cards included, lands within +-20 % of the
  target.
- `moments_from_specs(specs, cues)` takes {start, end, title, ...} records
  from a human (`clipbot outline` skeleton) or a model (llm.py) and snaps them
  to caption-cue boundaries so clips open and close on caption edges.

`build_reel_plan` turns either list of `Moment`s into the plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .captions import Cue
from .plan import PRESET_BOUNDS, build_plan
from .select import _STOP, _WORD, Window, clean_text, score_cue
from .speakers import parse_clock
from .summarize import _DECISION, _similar, _ts, score_sentence, sentences

INTRO_SECONDS = 4
CARD_SECONDS = 3
SECONDS_PER_MOMENT = 35.0  # ~30 s of speech + its 3 s card + slack
MIN_MOMENTS = 2  # one clip is a clip, not a reel
TOLERANCE = 0.20  # contract/README.md: runtime within +-20 % of the target
MIN_WINDOW = 15.0
MAX_WINDOW = 60.0
MAX_GAP = 5.0  # a longer silence inside a window ends it
DUPLICATE_JACCARD = 0.5
TITLE_LIMIT = 80
TAKEAWAY_LIMIT = 200
LINE_LIMIT = 120

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
    start: float
    end: float
    takeaway: str  # one sentence, <= 200 chars (clip.takeaway)
    title: str  # <= 80 chars (card title)
    why: str = ""  # one line for the card: "Decision · Kyle Tabor"
    lines: tuple[str, ...] = ()
    score: float = 0.0
    speaker: str | None = None
    cue_indexes: tuple[int, ...] = ()

    @property
    def duration(self) -> float:
        return self.end - self.start

    def card(self) -> dict:
        lines = [clip_text(ln, LINE_LIMIT) for ln in (self.lines or ((self.why,) if self.why else ()))]
        return {"title": self.title, "lines": [ln for ln in lines if ln][:4], "seconds": CARD_SECONDS}

    def spec(self) -> dict:
        """This moment as a --moments record (moments.json round-trip)."""
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "title": self.title,
            "takeaway": self.takeaway,
            "lines": list(self.lines),
            "why": self.why,
        }


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


def clip_text(text: str, limit: int) -> str:
    """Clean and cut at a word boundary; the contract caps titles at 80 and lines at 120."""
    text = clean_text(text)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + "…"


def runtime(moments: list[Moment]) -> float:
    """Reel length including the intro and one card per clip (chapter_cards = all)."""
    return INTRO_SECONDS + sum(CARD_SECONDS + m.duration for m in moments)


def target_band(minutes: float) -> tuple[float, float, float]:
    t = minutes * 60
    return t * (1 - TOLERANCE), t, t * (1 + TOLERANCE)


def content_score(cue: Cue) -> float:
    """Request-less cue score: word density and questions (select.score_cue with no
    keywords) plus decision language (summarize._DECISION)."""
    return score_cue(cue, set()) + 0.75 * len(_DECISION.findall(cue.text))


def _ends_sentence(c: Cue) -> bool:
    return c.text.rstrip().endswith((".", "!", "?"))


def candidate_windows(cues: list[Cue], lo: float, hi: float, target_len: float) -> list[Window]:
    """The best window starting at each cue.

    A window is a run of consecutive cues with lo <= duration <= hi and no
    silence longer than MAX_GAP. Score = sum(content) / max(duration, target_len):
    adding a cue helps until the window reaches the target length, after that
    only if it raises the per-second average, so windows stop where the good
    part ends instead of always running to `hi`. Edges on a sentence boundary
    earn a bonus so clips open and close on a sentence (contract obligation 5).
    """
    base = [content_score(c) + 0.5 for c in cues]  # +0.5: speech has value even when plain
    out: list[Window] = []
    n = len(cues)
    for i in range(n):
        starts_clean = i == 0 or _ends_sentence(cues[i - 1]) or cues[i - 1].speaker != cues[i].speaker
        total = 0.0
        best: Window | None = None
        for j in range(i, n):
            if j > i and (cues[j].start < cues[j - 1].start or cues[j].start - cues[j - 1].end > MAX_GAP):
                break
            dur = cues[j].end - cues[i].start
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
            if best is None or s > best.score:
                best = Window(cues[i].start, cues[j].end, s, "", tuple(range(i, j + 1)))
        if best is not None:
            out.append(best)
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
    return INTRO_SECONDS + sum(CARD_SECONDS + (w.end - w.start) for w in ws)


def pick_moments(cues: list[Cue], minutes: float, preset: str = "internal") -> list[Moment]:
    """Heuristic v2 (module docstring). Chronological result, +-20 % of `minutes` when possible."""
    if not cues:
        return []
    low, target, high = target_band(minutes)
    k = max(MIN_MOMENTS, round(target / SECONDS_PER_MOMENT))
    budget = max(target - INTRO_SECONDS - CARD_SECONDS * k, 0.0)
    hi = min(MAX_WINDOW, float(PRESET_BOUNDS.get(preset, (15, 120))[1]))
    lo = min(MIN_WINDOW, max(8.0, budget / k))  # tiny targets (demo: 0.5 min) cannot fit K x 15 s
    target_len = min(hi, max(lo, budget / k))
    cands = candidate_windows(cues, lo, hi, target_len)
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
    while _runtime_w(picked) > high and len(picked) > MIN_MOMENTS:
        picked.remove(min(picked, key=lambda w: w.score))
    picked.sort(key=lambda w: w.start)
    return [
        moment_from_cues(w.start, w.end, [cues[i] for i in w.cue_indexes], score=w.score, cue_indexes=w.cue_indexes)
        for w in picked
    ]


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
) -> Moment:
    takeaway = clip_text(tidy_sentence(takeaway or title or best_sentence(cues) or f"Moment at {_ts(start)}"), TAKEAWAY_LIMIT)
    if not takeaway.endswith((".", "!", "?", "…")):
        takeaway += "."
    title = clip_text(tidy_sentence(title) if title else takeaway, TITLE_LIMIT)
    speaker = dominant_speaker(cues)
    if why is None:
        why = " · ".join(x for x in (classify(cues), speaker) if x) or "Key moment"
    return Moment(
        start, end, takeaway, title, clip_text(why, LINE_LIMIT), tuple(lines), score, speaker, tuple(cue_indexes)
    )


def moments_from_specs(specs: list[dict], cues: list[Cue]) -> list[Moment]:
    """Snap human/LLM records to cue boundaries; keep the given order.

    start moves back to the start of the first cue it touches, end forward to
    the end of the last one, so the clip never opens or closes mid-caption.
    A span with no cues at all (silence, or no transcript there) is kept as is.
    """
    out: list[Moment] = []
    for k, spec in enumerate(specs, 1):
        try:
            start, end = parse_clock(spec["start"]), parse_clock(spec["end"])
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"moment {k}: needs start and end (seconds or h:mm:ss): {e}") from e
        if end <= start:
            raise ValueError(f"moment {k}: end must be after start")
        idx = tuple(i for i, c in enumerate(cues) if c.end > start and c.start < end)
        if idx:
            start, end = min(cues[i].start for i in idx), max(cues[i].end for i in idx)
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
            )
        )
    return out


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
) -> dict:
    """A v1 plan plus output.reel and one card per clip (contract v1.1)."""
    windows = [Window(m.start, m.end, m.score, m.takeaway, m.cue_indexes) for m in moments]
    plan = build_plan(
        source, windows, out_dir, preset=preset, captions_kind=captions_kind,
        srt_path=srt_path, summary_path=summary_path,
    )
    for clip, m in zip(plan["clips"], moments):
        clip["card"] = m.card()
    total = runtime(moments)
    plan["output"]["reel"] = {
        "filename": "reel.mp4",
        "intro": {
            "title": clip_text(title, TITLE_LIMIT) or "Summary",
            "lines": [f"Recorded {date}", f"{len(moments)} moments · {_ts(total)}"],
            "seconds": INTRO_SECONDS,
        },
        "chapter_cards": "all",
    }
    return plan
