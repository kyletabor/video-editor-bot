"""Parse, trim, and join SRT cues using source-time segment boundaries."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


_TIMESTAMP = r"([0-9]{2,}):([0-9]{2}):([0-9]{2}),([0-9]{3})"
_TIMING = re.compile(rf"^{_TIMESTAMP}\s+-->\s+{_TIMESTAMP}$")


def _seconds(parts: tuple[str, ...], context: str) -> float:
    hours, minutes, seconds, milliseconds = map(int, parts)
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"{context}: timestamp minutes and seconds must be below 60")
    try:
        result = hours * 3600 + minutes * 60 + seconds + milliseconds / 1000
    except OverflowError as exc:
        raise ValueError(f"{context}: timestamp must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{context}: timestamp must be finite")
    return result


def _interval(start: float, end: float, context: str) -> tuple[float, float]:
    if isinstance(start, bool) or isinstance(end, bool):
        # Keep one public validation exception for all malformed caption intervals.
        raise ValueError(f"{context}: start and end must be finite numbers")  # noqa: TRY004
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{context}: start and end must be finite numbers") from exc
    if not math.isfinite(start) or not math.isfinite(end):
        raise ValueError(f"{context}: start and end must be finite numbers")
    if start < 0 or end <= start:
        raise ValueError(f"{context}: require 0 <= start < end (got {start}, {end})")
    return start, end


def _validated(cue: Cue, context: str) -> Cue:
    start, end = _interval(cue.start, cue.end, context)
    if not isinstance(cue.text, str) or not cue.text.strip():
        raise ValueError(f"{context}: subtitle text must not be empty")
    return Cue(start, end, cue.text)


def _starts_cue(lines: list[str]) -> bool:
    return (
        len(lines) >= 2
        and re.fullmatch(r"[0-9]+", lines[0].strip()) is not None
        and _TIMING.fullmatch(lines[1].strip()) is not None
    )


def parse_srt(text: str) -> list[Cue]:
    """Read numbered SRT blocks, preserving multiline text and subtitle markup.

    Empty SRT content represents no cues. Invalid blocks report their one-based
    position rather than being silently skipped. Cue indices need not be
    sequential; timestamps, rather than indices, determine their source times.

    A block that carries neither an index nor a timing line continues the
    previous cue: meeting recorders (Zoom) embed captions whose text holds blank
    lines between speakers, and SRT has no way to escape a blank line, so
    FFmpeg's extraction and hand-written files alike present that text as
    separate blocks. The blank line itself is dropped from the cue text so the
    file written for the burn-in filter stays well-formed.
    """
    text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return []
    cues = []
    for position, block in enumerate(re.split(r"\n[ \t]*\n", text.strip()), 1):
        if not block.strip():
            continue
        lines = block.splitlines()
        context = f"SRT block {position}"
        if cues and not _starts_cue(lines):
            previous = cues[-1]
            cues[-1] = Cue(previous.start, previous.end, previous.text + "\n" + "\n".join(lines))
            continue
        if len(lines) < 3 or not re.fullmatch(r"[0-9]+", lines[0].strip()):
            raise ValueError(f"{context}: expected a numeric index, timing line, and subtitle text")
        match = _TIMING.fullmatch(lines[1].strip())
        if match is None:
            raise ValueError(
                f"{context}: expected finite timestamps in HH:MM:SS,mmm --> HH:MM:SS,mmm format"
            )
        cue = Cue(
            _seconds(match.groups()[:4], context),
            _seconds(match.groups()[4:], context),
            "\n".join(lines[2:]),
        )
        cues.append(_validated(cue, context))
    return cues


def _timestamp(milliseconds: int) -> str:
    seconds, milliseconds = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def format_srt(cues: Iterable[Cue]) -> str:
    """Write canonical SRT with millisecond timestamps and sequential indices.

    Fragments that round to an empty interval cannot be represented in SRT and
    are omitted. Other cues preserve their text and order exactly.
    """
    blocks = []
    for position, original in enumerate(cues, 1):
        cue = _validated(original, f"Cue {position}")
        if not math.isfinite(cue.end * 1000):
            raise ValueError(f"Cue {position}: timestamp is too large for millisecond precision")
        start = round(cue.start * 1000)
        end = round(cue.end * 1000)
        if end <= start:
            continue
        blocks.append(f"{len(blocks) + 1}\n{_timestamp(start)} --> {_timestamp(end)}\n{cue.text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def retime(cues: Iterable[Cue], segments: Iterable[Mapping[str, float]]) -> list[Cue]:
    """Intersect cues with half-open segments and join in the supplied order.

    Each segment refers to the original source timeline. Repeated and reordered
    segments therefore repeat and reorder captions, too. A cue spanning an edit
    is split at that boundary, and cues that only touch a boundary are excluded.
    """
    source = sorted(
        (_validated(cue, f"Cue {position}") for position, cue in enumerate(cues, 1)),
        key=lambda cue: cue.start,
    )
    output = []
    offset = 0.0
    for position, segment in enumerate(segments, 1):
        context = f"Segment {position}"
        try:
            start, end = _interval(segment["start"], segment["end"], context)
        except KeyError as exc:
            raise ValueError(f"{context}: missing {exc.args[0]!r} time") from exc
        duration = end - start
        if not math.isfinite(offset + duration):
            raise ValueError(f"{context}: combined duration must be finite")
        for cue in source:
            if cue.start >= end:
                break
            kept_start = max(start, cue.start)
            kept_end = min(end, cue.end)
            if kept_start < kept_end:
                output.append(
                    Cue(offset + (kept_start - start), offset + (kept_end - start), cue.text)
                )
        offset += duration
    return output


_BURN_NOISE = re.compile(r"^\s*(?:\([^)]*\)|-+|\(\))\s*$")


def tidy_for_burn(cues: Iterable[Cue]) -> list[Cue]:
    """Drop caption lines that only make sense in a sidecar file.

    Google Meet's embedded captions put the speaker on its own line, "(Kyle Tabor)",
    and leave a lone "-" or "()" where a second voice was cut. Burned into the
    picture those read as stray fragments (seen on the first Talk #2 reel), so the
    burn-in path removes such lines and skips cues with nothing left. Sidecar SRT
    keeps the original text so speaker names survive for readers.
    """
    tidy = []
    for cue in cues:
        lines = [line for line in cue.text.splitlines() if not _BURN_NOISE.match(line)]
        text = "\n".join(line.strip() for line in lines if line.strip())
        if text:
            tidy.append(Cue(cue.start, cue.end, text))
    return tidy

