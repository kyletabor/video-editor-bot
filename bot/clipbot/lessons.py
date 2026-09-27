"""Frame moments as lessons, and build the reel's bookend cards (contract v1.2).

Kyle on the first reel: "I don't feel like the audience would get value from
these clips of the session" and "we need a summary at the end". A clip of a
meeting is only worth a stranger's time when it is framed: what you will learn
(the *lesson*), what the room already knew (the *context*), and a summary at
the end (the *takeaways*). Moments carry `lesson` and `context` (reel.Moment);
this module scores how teachable a passage is for the heuristic selector, and
turns lessons into the opening, closing and outro cards.

Card text limits are the contract's: titles <= 80, lines <= 4 x <= 120.
"""

from __future__ import annotations

import re
from collections import Counter

from .select import _STOP, _WORD, clean_text

TITLE_LIMIT = 80
LINE_LIMIT = 120
LESSON_LIMIT = 80
CONTEXT_LIMIT = 120
CARD_SECONDS = 3
MAX_CARD_LINES = 4
OPENING_TITLE = "What you'll learn"
TAKEAWAYS_TITLE = "Takeaways"
OUTRO_TITLE = "That's the session"

# Teachable language: something a viewer can take home.
_EXPLAINS = re.compile(
    r"\b(because|the reason|which means|that's why|the way to|here's how|here is how|how to|the trick|"
    r"it works by|turns out|in other words|the idea is|the point is|what that means)\b", re.I)
_DECIDES = re.compile(
    r"\b(we should|we need|the rule|requirement|we decided|decision|never|always|must|has to|"
    r"the goal|next step|hard requirement|non-negotiable)\b", re.I)
_HOWTO = re.compile(
    r"\b(you can|you just|instead of|rather than|make sure|the fix|the answer|the key|the lesson|"
    r"lessons? learned|takeaway|what i learned|surprised|impressed|first,|then,|step)\b", re.I)
_NUMBERS = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:%|percent|x|times|minutes?|hours?|seconds?|days?|weeks?|k|m|ms)?\b", re.I)
# Banter and logistics: never a lesson, however dense.
_BANTER = re.compile(
    r"\b(can you (?:hear|see) (?:me|my screen)|share my screen|let me share|hold on|one sec|hang on|"
    r"we(?:'re| are) recording|hadn't been recording|just joined|joined late|hi everyone|hey everyone|"
    r"welcome back|thanks for (?:joining|coming)|good morning|good afternoon|how are you|how's it going|"
    r"lol|haha|mute|unmute|back in a (?:sec|minute)|are you there|can everyone|let me know if)\b", re.I)


def teachable(text: str) -> float:
    """Positive for explanations, decisions, numbers and how-to phrasing;
    negative for banter and logistics. Zero for plain speech."""
    s = 1.0 * len(_EXPLAINS.findall(text))
    s += 1.0 * len(_DECIDES.findall(text))
    s += 0.5 * len(_HOWTO.findall(text))
    s += 0.5 * min(len(_NUMBERS.findall(text)), 3)
    s -= 1.5 * len(_BANTER.findall(text))
    return s


def clip_text(text: str, limit: int) -> str:
    """Clean and cut at a word boundary; the contract caps titles at 80 and lines at 120."""
    text = clean_text(text)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + "…"


def card_seconds(lines: list[str]) -> float:
    """Reading time: 3 s for one line, +1.5 s per extra line, at most 8 s (contract max 10)."""
    return float(min(8.0, 3.0 + 1.5 * max(0, len(lines) - 1)))


def top_topics(lines: list[str], k: int = 3) -> list[str]:
    """Content words that recur across the lines (>= 2 lines), most frequent first."""
    seen: Counter[str] = Counter()
    for ln in lines:
        seen.update({w for w in _WORD.findall(ln.lower()) if w not in _STOP and len(w) > 3})
    return [w for w, n in seen.most_common() if n >= 2][:k]


def opening_card(lessons: list[str], *, title_hint: str = "") -> dict | None:
    """"What you'll learn": one line per lesson; with more than four lessons a
    summary line ("N moments on a, b, c") plus the first three."""
    lines = [clip_text(x, LINE_LIMIT) for x in lessons if x and x.strip()]
    if not lines:
        return None
    if len(lines) > MAX_CARD_LINES:
        topics = top_topics(lines)
        summary = (f"{len(lines)} moments on {', '.join(topics)}" if topics
                   else f"{len(lines)} moments from {title_hint}" if title_hint else f"{len(lines)} moments")
        lines = [clip_text(summary, LINE_LIMIT)] + lines[: MAX_CARD_LINES - 1]
    return {"title": OPENING_TITLE, "lines": lines, "seconds": card_seconds(lines)}


def closing_cards(takeaways: list[str], *, max_cards: int = 2) -> list[dict]:
    """1-2 "Takeaways" cards, four lines each, in the order given."""
    lines = [clip_text(x, LINE_LIMIT) for x in takeaways if x and x.strip()]
    lines = [ln for ln in lines if ln][: MAX_CARD_LINES * max_cards]
    cards = []
    for k in range(0, len(lines), MAX_CARD_LINES):
        chunk = lines[k:k + MAX_CARD_LINES]
        title = TAKEAWAYS_TITLE if k == 0 else f"{TAKEAWAYS_TITLE} (continued)"
        cards.append({"title": title, "lines": chunk, "seconds": card_seconds(chunk)})
    return cards


def outro_card(summary_name: str = "summary.md") -> dict:
    lines = [f"Full transcript and takeaways in {summary_name}"]
    return {"title": OUTRO_TITLE, "lines": lines, "seconds": card_seconds(lines)}
