"""ffprobe wrapper: the few facts the bot needs about a source file."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceInfo:
    path: str
    duration_seconds: float
    has_video: bool
    has_audio: bool
    subtitle_stream_index: int | None  # index among subtitle streams (0 = first), None if none


def probe(path: str | Path) -> SourceInfo:
    """Run ffprobe once and summarize. Raises FileNotFoundError / RuntimeError."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=index,codec_type",
        "-of", "json", str(p),
    ]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    except FileNotFoundError as e:  # ffprobe missing
        raise RuntimeError("ffprobe not found on PATH") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffprobe failed: {e.stderr.strip()}") from e
    return summarize(json.loads(out), str(p))


def summarize(ffprobe_json: dict, path: str) -> SourceInfo:
    """Pure function over ffprobe JSON, so it can be unit-tested without ffprobe."""
    streams = ffprobe_json.get("streams", [])
    types = [s.get("codec_type") for s in streams]
    sub_idx = None
    n_sub = 0
    for s in streams:
        if s.get("codec_type") == "subtitle":
            if sub_idx is None:
                sub_idx = n_sub
            n_sub += 1
    duration = float(ffprobe_json.get("format", {}).get("duration", 0) or 0)
    if duration <= 0:
        raise RuntimeError(f"could not determine duration of {path}")
    return SourceInfo(
        path=path,
        duration_seconds=duration,
        has_video="video" in types,
        has_audio="audio" in types,
        subtitle_stream_index=sub_idx,
    )
