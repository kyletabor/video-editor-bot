"""Find pauses in the source audio with ffmpeg's silencedetect.

Why ffmpeg and not the transcript: a gap between two caption cues is often not
a pause at all (the ASR simply broke a sentence there), and whisper's segments
run into each other with no gap even across a five-second silence. The audio is
the ground truth. One ffmpeg run per moment, seeking with ``-ss`` before ``-i``
so a moment near the end of a 79-minute file costs a fraction of a second, all
runs in parallel; ``-vn`` drops the video before any decoding.

With input seeking both ffmpeg 4.4 and 7.0 report silencedetect times relative
to the seek point (checked empirically on both builds), so `parse_silencedetect`
adds the moment's start back. Argument lists only, never a shell string
(paths with spaces, Windows).
"""

from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

NOISE_DB = -35.0
MIN_SECONDS = 0.7  # the same default as cuts.MAX_SILENCE
WORKERS = 4

_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def silencedetect_args(source: str | Path, start: float, duration: float, *,
                       noise_db: float = NOISE_DB, min_seconds: float = MIN_SECONDS) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "info",
        "-ss", f"{max(start, 0.0):.3f}", "-t", f"{max(duration, 0.0):.3f}", "-i", str(source),
        "-vn", "-sn", "-dn", "-af", f"silencedetect=noise={noise_db:g}dB:d={min_seconds:g}",
        "-f", "null", "-",
    ]


def parse_silencedetect(stderr: str, offset: float, span_end: float) -> list[tuple[float, float]]:
    """(start, end) pairs in source seconds. A silence still open when the
    analysed span ends has no `silence_end` line and is closed at `span_end`."""
    out: list[tuple[float, float]] = []
    open_at: float | None = None
    for line in stderr.splitlines():
        m = _START.search(line)
        if m:
            open_at = max(offset, offset + float(m.group(1)))
            continue
        m = _END.search(line)
        if m and open_at is not None:
            end = min(span_end, offset + float(m.group(1)))
            if end > open_at:
                out.append((open_at, end))
            open_at = None
    if open_at is not None and span_end > open_at:
        out.append((open_at, span_end))
    return out


def detect_silences(
    source: str | Path,
    spans: list[tuple[float, float]],
    *,
    noise_db: float = NOISE_DB,
    min_seconds: float = MIN_SECONDS,
    workers: int = WORKERS,
) -> list[list[tuple[float, float]]]:
    """Silences inside each span, one list per span, in the spans' order.

    Raises RuntimeError when ffmpeg is missing or fails; the caller decides
    whether that blocks the run (clipbot reel does not: it keeps the moments
    whole and says so)."""

    def one(span: tuple[float, float]) -> list[tuple[float, float]]:
        start, end = span
        if end <= start:
            return []
        args = silencedetect_args(source, start, end - start, noise_db=noise_db, min_seconds=min_seconds)
        try:
            proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        except FileNotFoundError as e:
            raise RuntimeError("ffmpeg not found on PATH") from e
        if proc.returncode:
            tail = proc.stderr.strip().splitlines()[-1:] or ["no output"]
            raise RuntimeError(f"ffmpeg silencedetect failed: {tail[0]}")
        return parse_silencedetect(proc.stderr, start, end)

    if not spans:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(spans)))) as pool:
        return list(pool.map(one, spans))
