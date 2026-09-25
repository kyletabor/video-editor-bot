"""Transcript + executive summary markdown (veb-t2b.6, Claudia's ask on the call).

Extractive v1: no model call. Sentences are scored on content density and
"decision" cues; the top few become the executive summary. When an agent is
driving (the UI is the AI), it can rewrite the summary section by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .captions import Cue
from .select import _FILLER, _STOP, _WORD, keyword_hits

_DECISION = re.compile(
    r"\b(we should|we need|the goal|decid|agree|requirement|question|problem|"
    r"idea|plan|next step|takeaway|lesson|because|so that)\b",
    re.I,
)


@dataclass(frozen=True)
class Sentence:
    start: float
    text: str
    speaker: str | None


def sentences(cues: list[Cue]) -> list[Sentence]:
    """Join cues into sentences; each sentence keeps the start time of the cue it began in."""
    out: list[Sentence] = []
    buf: list[str] = []
    buf_start = 0.0
    buf_speaker: str | None = None
    for c in cues:
        text = _FILLER.sub("", c.text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        if buf and c.speaker != buf_speaker:
            # Speaker changed mid-sentence (interruption / unpunctuated turn):
            # flush so the other person's words are never credited to the first.
            out.append(Sentence(buf_start, " ".join(buf).rstrip(".") + ".", buf_speaker))
            buf = []
        if not buf:
            buf_start, buf_speaker = c.start, c.speaker
        parts = re.split(r"(?<=[.!?])\s+", text)
        for k, p in enumerate(parts):
            p = p.strip(" ,")
            if not p:
                continue
            buf.append(p)
            if p.endswith((".", "!", "?")) and (k < len(parts) - 1 or True):
                out.append(Sentence(buf_start, " ".join(buf), buf_speaker))
                buf = []
                buf_start, buf_speaker = c.start, c.speaker
    if buf:
        out.append(Sentence(buf_start, " ".join(buf) + ".", buf_speaker))
    return out


def score_sentence(s: Sentence, focus: set[str] | None = None) -> float:
    words = _WORD.findall(s.text.lower())
    content = [w for w in words if w not in _STOP and len(w) > 2]
    if len(words) < 5:
        return 0.0
    score = len(content) / len(words) * 2.0 + min(len(content), 12) * 0.2
    score += 1.0 * len(_DECISION.findall(s.text))
    if focus:
        score += 1.5 * keyword_hits(Cue(0, 1, s.text), focus)
    return score


def executive_summary(cues: list[Cue], n: int = 4, focus: set[str] | None = None) -> list[Sentence]:
    sents = sentences(cues)
    ranked = sorted(sents, key=lambda s: score_sentence(s, focus), reverse=True)
    picked: list[Sentence] = []
    for s in ranked:
        if len(picked) >= n:
            break
        if score_sentence(s, focus) <= 0:
            break
        if any(_similar(s.text, p.text) for p in picked):
            continue
        picked.append(s)
    picked.sort(key=lambda s: s.start)
    return picked


def _similar(a: str, b: str) -> bool:
    wa = {w for w in _WORD.findall(a.lower()) if w not in _STOP}
    wb = {w for w in _WORD.findall(b.lower()) if w not in _STOP}
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) > 0.6


def _ts(t: float) -> str:
    m, s = divmod(int(t), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def to_markdown(
    title: str,
    source_path: str,
    duration: float,
    cues: list[Cue],
    plan: dict | None = None,
    focus: set[str] | None = None,
) -> str:
    lines = [f"# {title}", "", f"Source: `{source_path}` · {_ts(duration)} long · {len(cues)} caption cues", ""]
    lines += ["## Executive summary", ""]
    summary = executive_summary(cues, focus=focus)
    if summary:
        for s in summary:
            who = f"{s.speaker}: " if s.speaker else ""
            lines.append(f"- [{_ts(s.start)}] {who}{s.text}")
    else:
        lines.append("- (no summarizable speech found)")
    lines.append("")
    reel = (plan or {}).get("output", {}).get("reel") if plan else None
    if reel and plan.get("clips"):
        # A reel's clips are its chapters: list them as the viewer will meet them,
        # with source timestamps so a reader can jump into the full recording.
        lines += ["## Reel", ""]
        intro = reel.get("intro")
        if intro:
            lines.append(f"Intro: **{intro['title']}** — {' · '.join(intro.get('lines', []))}")
            lines.append("")
        for n, c in enumerate(plan["clips"], 1):
            card = c.get("card") or {}
            spans = " + ".join(f"{_ts(s['start'])}–{_ts(s['end'])}" for s in c["segments"])
            why = "; ".join(card.get("lines", []))
            tail = f" — {why}" if why else ""
            lines.append(f"{n}. [{spans}] **{card.get('title') or c['takeaway']}**{tail} (`{c['id']}`)")
        lines.append("")
    elif plan and plan.get("clips"):
        lines += ["## Clips", ""]
        for c in plan["clips"]:
            spans = " + ".join(f"{_ts(s['start'])}–{_ts(s['end'])}" for s in c["segments"])
            total = sum(s["end"] - s["start"] for s in c["segments"])
            lines.append(f"- **{c['id']}** [{spans}] ({_ts(total)}) — {c['takeaway']}")
        lines.append("")
    lines += ["## Full transcript", ""]
    last_speaker: str | None = None
    for c in cues:
        if c.speaker != last_speaker:
            lines.append("")
            lines.append(f"**{c.speaker or 'Speaker'}**")
            last_speaker = c.speaker
        lines.append(f"- [{_ts(c.start)}] {c.text}")
    lines.append("")
    return "\n".join(lines)
