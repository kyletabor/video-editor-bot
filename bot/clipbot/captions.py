"""Get a timed transcript: embedded subtitle stream -> SRT -> cues.

Meet recordings carry a mov_text stream with 4-second cues and a leading
"(Speaker Name)" line. That is enough for v1; whisper is a later adapter.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})")
_SPEAKER = re.compile(r"^\((.+?)\)\s*$")


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str
    speaker: str | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start


def extract_embedded_srt(source: str | Path, stream_index: int = 0) -> str:
    """Pull subtitle stream N out of the container as SRT text."""
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin", "-i", str(source),
        "-map", f"0:s:{stream_index}", "-f", "srt", "-",
    ]
    try:
        # ffmpeg writes UTF-8; never let the OS locale (cp1252 on Windows) decode it.
        return subprocess.run(
            cmd, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
        ).stdout
    except FileNotFoundError as e:
        raise RuntimeError("ffmpeg not found on PATH") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg subtitle extraction failed: {e.stderr.strip()}") from e


def _ts(m: re.Match) -> float:
    h, mi, s, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[Cue]:
    """Parse SRT into cues. Tolerates CRLF, BOM, missing indexes, '(Speaker)' lines."""
    cues: list[Cue] = []
    blocks = re.split(r"\r?\n\s*\r?\n", text.lstrip("﻿").strip())
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        # find the timing line
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ti is None:
            continue
        times = _TIME.findall(lines[ti])
        if len(times) < 2:
            continue
        start = _ts(_TIME.search(lines[ti]))
        end = _ts(list(_TIME.finditer(lines[ti]))[1])
        body = lines[ti + 1:]
        speaker = None
        if body and (m := _SPEAKER.match(body[0])):
            speaker = m.group(1)
            body = body[1:]
        content = " ".join(body).strip()
        if not content or end <= start:
            continue
        cues.append(Cue(start=start, end=end, text=content, speaker=speaker))
    return cues


def cues_to_srt(cues: list[Cue]) -> str:
    """Write cues back out (used for sidecar SRT when the source has no embedded stream)."""
    def fmt(t: float) -> str:
        ms = int(round(t * 1000))
        h, rem = divmod(ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{fmt(c.start)} --> {fmt(c.end)}\n{c.text}\n")
    return "\n".join(out)
