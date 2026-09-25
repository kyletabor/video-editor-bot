"""Timestamped outline of a session plus a moments skeleton (`clipbot outline`).

Why: the heuristic in reel.py is blind; it scores word density, not meaning.
The outline is what a human or a model reads to decide what actually mattered,
and the JSON skeleton at the end is the hand-off format for
`clipbot reel --moments`. Thirty-second blocks keep a 90-minute session to a
few hundred lines: skimmable by a person, and small enough to send to a model
in chunks (llm.py).
"""

from __future__ import annotations

import json

from .captions import Cue
from .select import clean_text

BLOCK_SECONDS = 30

SKELETON = [
    {
        "start": "0:00:00",
        "end": "0:00:30",
        "title": "Card title, <= 80 chars (also the clip takeaway)",
        "lines": ["Optional card line, <= 120 chars, up to 4"],
        "why": "Optional: Decision / Demo / Q&A / who is speaking; the card line when `lines` is empty",
    }
]


def clock(t: float) -> str:
    """Always h:mm:ss so a reader can paste it straight into the skeleton."""
    t = max(0, int(t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def blocks(cues: list[Cue], block_seconds: int = BLOCK_SECONDS) -> list[tuple[float, list[Cue]]]:
    groups: dict[int, list[Cue]] = {}
    for c in cues:
        groups.setdefault(int(c.start // block_seconds), []).append(c)
    return [(k * block_seconds, groups[k]) for k in sorted(groups)]


def format_block(t: float, cues: list[Cue]) -> str:
    """`[h:mm:ss] Speaker: text` with one continuation line per change of speaker."""
    turns: list[tuple[str | None, list[str]]] = []
    for c in cues:
        text = clean_text(c.text)
        if not text:
            continue
        if turns and turns[-1][0] == c.speaker:
            turns[-1][1].append(text)
        else:
            turns.append((c.speaker, [text]))
    if not turns:
        return ""
    stamp = f"[{clock(t)}]"
    lines = []
    for n, (speaker, texts) in enumerate(turns):
        prefix = stamp if n == 0 else " " * len(stamp)
        who = f"{speaker}: " if speaker else ""
        lines.append(f"{prefix} {who}{' '.join(texts)}")
    return "\n".join(lines)


def transcript_text(cues: list[Cue], block_seconds: int = BLOCK_SECONDS) -> str:
    """The block transcript alone (no headers): what llm.py sends to the model."""
    parts = []
    for t, cs in blocks(cues, block_seconds):
        block = format_block(t, cs)
        if block:
            parts.append(block)
    return "\n\n".join(parts)


def skeleton_json() -> str:
    return json.dumps(SKELETON, indent=2)


def outline_markdown(
    title: str,
    source_path: str,
    duration: float,
    cues: list[Cue],
    block_seconds: int = BLOCK_SECONDS,
) -> str:
    speakers = sorted({c.speaker for c in cues if c.speaker})
    lines = [
        f"# {title} — outline",
        "",
        f"Source: `{source_path}` · {clock(duration)} long · {len(cues)} caption cues · "
        f"{block_seconds} s blocks",
    ]
    if speakers:
        lines.append(f"Speakers: {', '.join(speakers)}")
    lines += ["", "## Transcript", "", transcript_text(cues, block_seconds) or "(no speech found)", ""]
    lines += [
        "## Moments skeleton",
        "",
        "Pick the moments worth keeping, fill this list (times in seconds or h:mm:ss as printed "
        "above; `title`, `lines` and `why` are optional) and hand it back:",
        "",
        "    clipbot reel --source <video> --moments moments.json --out out/reel/plan.json",
        "",
        "```json",
        skeleton_json(),
        "```",
        "",
    ]
    return "\n".join(lines)
