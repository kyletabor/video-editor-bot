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


# Caption lines that carry no speech, as Google Meet and Zoom embed them: a speaker tag
# "(Kyle Tabor)" (or the anonymous "()") introduces the speech on the line after it, and a
# lone "-" closes the speech on the line before it where another voice took over.
_ANNOTATION = re.compile(r"^\s*(?:\([^)]*\)|-+)\s*$")
_WORD = re.compile(r"\S+")

MIN_KEPT_FRACTION = 0.25
"""Share of a cue's duration a cut must leave for the cue to survive (strictly more than this)."""


def _trim_words(text: str, kept_from: float, kept_to: float) -> str:
    """Keep the words spoken during the fraction ``[kept_from, kept_to]`` of a cue.

    Caption cues carry no per-word timing, so the words are taken to be evenly spaced
    over the cue: word ``i`` of ``n`` is kept when its centre ``(i + 0.5) / n`` lies in
    the kept fraction. That keeps one run of words from the side the cut did not
    touch, at word boundaries, with the original spacing between them. Annotation
    lines hold no words and follow the speech they belong to: a speaker tag stays
    with the next content line, a cut marker with the previous one, so a trimmed
    sidecar cue still names its speaker. Text that has no words at all (annotations
    only) is returned unchanged; text whose words are all cut becomes empty.
    """
    lines = text.split("\n")
    found = [None if _ANNOTATION.match(line) else list(_WORD.finditer(line)) for line in lines]
    total = sum(len(words) for words in found if words is not None)
    if total == 0:
        return text
    kept: list[str | None] = []
    first = 0
    for line, words in zip(lines, found):
        if words is None:
            kept.append(None)
            continue
        inside = [
            match
            for index, match in enumerate(words, first)
            if kept_from <= (index + 0.5) / total <= kept_to
        ]
        first += len(words)
        kept.append(line[inside[0].start() : inside[-1].end()] if inside else "")
    result = []
    for position, (line, remaining) in enumerate(zip(lines, kept)):
        if remaining is None:
            step = 1 if line.lstrip().startswith("(") else -1
            neighbour = position + step
            while 0 <= neighbour < len(lines) and kept[neighbour] is None:
                neighbour += step
            if 0 <= neighbour < len(lines) and kept[neighbour]:
                result.append(line)
        elif remaining:
            result.append(remaining)
    return "\n".join(result)


def retime(cues: Iterable[Cue], segments: Iterable[Mapping[str, float]]) -> list[Cue]:
    """Intersect cues with half-open segments and join in the supplied order.

    Each segment refers to the original source timeline. Repeated and reordered
    segments therefore repeat and reorder captions, too. A cue spanning an edit
    is split at that boundary, and cues that only touch a boundary are excluded.

    A cue that a segment edge cuts through keeps only the words spoken in its kept
    part (``_trim_words``). Meeting recorders emit cues of about four seconds, so
    a clip that starts partway through one would otherwise open on a caption full
    of words the viewer never hears: the first Talk #2 reel opened on "Normally
    what you would do" under the caption "I don't know. I don't know. I so, okay,
    normally what you would do". A cue that keeps ``MIN_KEPT_FRACTION`` of its
    duration or less, or no whole word, is dropped rather than shown as a
    fragment. A cue wholly inside a segment keeps its text byte for byte, so
    sidecar files are unchanged wherever no cut passes through a cue.
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
            if kept_start >= kept_end:
                continue
            text = cue.text
            if kept_start > cue.start or kept_end < cue.end:
                span = cue.end - cue.start
                if (kept_end - kept_start) / span <= MIN_KEPT_FRACTION:
                    continue
                text = _trim_words(
                    cue.text, (kept_start - cue.start) / span, (kept_end - cue.start) / span
                )
                if not text.strip():
                    continue
            output.append(Cue(offset + (kept_start - start), offset + (kept_end - start), text))
        offset += duration
    return output


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
        lines = [line for line in cue.text.splitlines() if not _ANNOTATION.match(line)]
        text = "\n".join(line.strip() for line in lines if line.strip())
        if text:
            tidy.append(Cue(cue.start, cue.end, text))
    return tidy
