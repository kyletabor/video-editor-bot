"""Find pauses in the source audio with ffmpeg's silencedetect.

Why ffmpeg and not the transcript: a gap between two caption cues is often not
a pause at all (the ASR simply broke a sentence there), and whisper's segments
run into each other with no gap even across a five-second silence. The audio is
the ground truth. One ffmpeg run per moment, seeking with ``-ss`` before ``-i``
so a moment near the end of a 79-minute file costs a fraction of a second, all
runs in parallel; ``-vn`` drops the video before any decoding.

What counts as one pause: the stereo track of a Meet recording carries clicks
and ticks that the mono, 16 kHz file a verifier listens to does not. On talk2 a
1.3 s pause showed up as two 0.6 s silences around a 30 ms blip, so a detector
asked for silences of at least 0.7 s reported nothing and the pause survived
into the reel. So the audio is downmixed to mono first (`aformat`, present in
every ffmpeg since 0.9), ffmpeg is asked for every silence of at least
`DETECT_SECONDS`, and `merge_silences` joins silences separated by less than
`BLIP_SECONDS` of sound before dropping runs shorter than what the caller asked
for. A 30 ms click inside a pause is still a pause; nothing that short is a word.

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
DETECT_SECONDS = 0.2  # ffmpeg reports every silence at least this long; merge_silences does the rest
BLIP_SECONDS = 0.1  # sound shorter than this between two silences does not end the pause
WORKERS = 4

_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def silencedetect_args(source: str | Path, start: float, duration: float, *,
                       noise_db: float = NOISE_DB, min_seconds: float = MIN_SECONDS,
                       detect_seconds: float = DETECT_SECONDS) -> list[str]:
    """ffmpeg argv: seek, decode the audio only, downmix to mono, report every
    silence of at least `min(detect_seconds, min_seconds)` (module docstring)."""
    d = min(detect_seconds, min_seconds)
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "info",
        "-ss", f"{max(start, 0.0):.3f}", "-t", f"{max(duration, 0.0):.3f}", "-i", str(source),
        "-vn", "-sn", "-dn", "-af", f"aformat=channel_layouts=mono,silencedetect=noise={noise_db:g}dB:d={d:g}",
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


def merge_silences(found: list[tuple[float, float]], min_seconds: float,
                   blip: float = BLIP_SECONDS) -> list[tuple[float, float]]:
    """One pause per run of silences separated by less than `blip` of sound,
    keeping the runs of at least `min_seconds` (module docstring)."""
    runs: list[tuple[float, float]] = []
    for s, e in sorted(found):
        if runs and s - runs[-1][1] < blip:
            runs[-1] = (runs[-1][0], max(runs[-1][1], e))
        else:
            runs.append((s, e))
    return [(s, e) for s, e in runs if e - s >= min_seconds - 1e-9]


def detect_silences(
    source: str | Path,
    spans: list[tuple[float, float]],
    *,
    noise_db: float = NOISE_DB,
    min_seconds: float = MIN_SECONDS,
    workers: int = WORKERS,
) -> list[list[tuple[float, float]]]:
    """Silences of at least `min_seconds` inside each span (blips bridged, see
    `merge_silences`), one list per span, in the spans' order.

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
        return merge_silences(parse_silencedetect(proc.stderr, start, end), min_seconds)

    if not spans:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(spans)))) as pool:
        return list(pool.map(one, spans))
