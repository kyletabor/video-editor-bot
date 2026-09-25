"""Best-effort speaker labels from a Gemini "Notes by Gemini" transcript.

Why: whisper gives us words but not who said them; Meet's embedded captions
have names but are 4-second fragments. Gemini's transcript has names and whole
utterances but only a coarse clock: a standalone `hh:mm:ss` line every 60-80 s
and no per-line times. So we interpolate a time for every Gemini line between
its anchors (proportional to text length, ~constant speaking rate), shift by
`offset` (the notes' clock may start before the video does), and label each
unlabelled cue with the nearest Gemini line that shares its words. Cues that
match nothing inherit a neighbour's label when both neighbours agree, and
otherwise stay unlabelled. This never raises on bad input: no labels is a
worse outline, not a failed reel.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace

from .captions import Cue
from .select import _STOP, _WORD

_ANCHOR = re.compile(r"^\s*(\d{1,2}):(\d{2}):(\d{2})\s*$")
# "Kyle Tabor: text", "Ramsey Jamoul's Presentation: text". Titles such as
# "Pipeline AI Talk #2: Build a clip bot" fail on the "#", which is what we want.
_LINE = re.compile(r"^([A-Z][A-Za-z.'’\- ]{0,39}):\s+(\S.*)$")
CHARS_PER_SECOND = 15.0  # ~150 words per minute; only used after the last anchor


@dataclass(frozen=True)
class Utterance:
    time: float  # seconds on the notes' clock (before offset)
    speaker: str
    text: str


def parse_clock(value: str | float | int) -> float:
    """Seconds from a number or 'h:mm:ss(.fff)', 'mm:ss', 'ss' (optionally negative)."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    sign = -1.0 if s.startswith("-") else 1.0
    s = s.lstrip("+-")
    parts = s.split(":")
    if not 1 <= len(parts) <= 3 or not all(p.strip() for p in parts):
        raise ValueError(f"not a time: {value!r}")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return sign * total


def parse_gemini(text: str) -> list[Utterance]:
    """Utterances with interpolated times. Lines before the first anchor are ignored
    (Gemini's summary section has no clock)."""
    blocks: list[tuple[float, list[tuple[str, str]]]] = []
    for raw in text.splitlines():
        line = raw.strip()
        m = _ANCHOR.match(line)
        if m:
            h, mi, s = (int(x) for x in m.groups())
            blocks.append((h * 3600 + mi * 60 + s, []))
            continue
        if not blocks:
            continue
        m = _LINE.match(line)
        if m:
            blocks[-1][1].append((m.group(1).strip(), m.group(2).strip()))
    out: list[Utterance] = []
    for n, (t, lines) in enumerate(blocks):
        total_chars = sum(len(tx) for _, tx in lines) or 1
        span = blocks[n + 1][0] - t if n + 1 < len(blocks) else total_chars / CHARS_PER_SECOND
        before = 0
        for speaker, tx in lines:
            out.append(Utterance(t + max(span, 0.0) * before / total_chars, speaker, tx))
            before += len(tx)
    return out


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


def align(
    cues: list[Cue],
    utterances: list[Utterance],
    offset: float = 0.0,
    window: float = 30.0,
    min_overlap: float = 0.5,
) -> tuple[list[Cue], int]:
    """Return (cues with speakers filled in where possible, number labelled).

    `offset` is subtracted from the notes' clock: video_seconds = notes_seconds - offset.
    Cues that already carry a speaker are left alone."""
    if not utterances or not cues:
        return list(cues), 0
    shifted = sorted(((u.time - offset, u) for u in utterances), key=lambda x: x[0])
    times = [t for t, _ in shifted]
    out: list[Cue] = []
    labelled = 0
    for c in cues:
        if c.speaker:
            out.append(c)
            continue
        mid = (c.start + c.end) / 2
        ctoks = _tokens(c.text)
        best: Utterance | None = None
        best_s = -1.0
        if ctoks:
            for t, u in shifted[bisect_left(times, mid - window):bisect_right(times, mid + window)]:
                overlap = len(ctoks & _tokens(u.text)) / len(ctoks)
                s = overlap - 0.005 * abs(t - mid)  # ties go to the nearest in time
                if overlap >= min_overlap and s > best_s:
                    best, best_s = u, s
        if best is not None:
            out.append(replace(c, speaker=best.speaker))
            labelled += 1
        else:
            out.append(c)
    # Second pass: short cues ("Okay.") match nothing; if both labelled neighbours
    # within 10 s agree, they were almost certainly said by the same person.
    for i, c in enumerate(out):
        if c.speaker:
            continue
        prev = next((x for x in reversed(out[:i]) if x.speaker), None)
        nxt = next((x for x in out[i + 1:] if x.speaker), None)
        if prev and nxt and prev.speaker == nxt.speaker and nxt.start - prev.end <= 10.0:
            out[i] = replace(c, speaker=prev.speaker)
            labelled += 1
    return out, labelled
