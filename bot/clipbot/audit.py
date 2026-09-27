"""Measure the audio on both sides of every segment edge in a plan.

Why this exists: the fourth talk2 reel had no cut inside a whisper word span
and still cut through voiced audio at 11 of 86 edges. Whisper's timing for an
"uh" is off by 50-150 ms, so a cut placed at its word edge lands inside the
filler or inside the neighbouring word. The word list cannot see that; the
waveform can. This module reads the 40 ms (`WINDOW`) before and after each edge
straight from a PCM WAV and reports the peak level of each side in dBFS. An
edge with speech above `SPEECH_DB` on BOTH sides is an offender: the cut goes
through sound. One quiet side is fine (a clip may start on a word onset).

It is the independent check the cutting rules (cuts.py) are held to, so it
shares nothing with them: no silence lists, no word timings. `--words` only
labels an offender with the word whisper puts there.

The WAV is read with the standard library (`wave`), so the audit needs no
ffmpeg when the caller has a 16-bit PCM file such as talk2-16k.wav. Any other
audio (an .mp4, a 24-bit WAV) is first decoded to a temporary 16 kHz mono
16-bit WAV with ffmpeg (options every ffmpeg since 4.4 has).
"""

from __future__ import annotations

import math
import subprocess
import sys
import tempfile
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

from .words import Word

WINDOW = 0.04  # seconds measured on each side of an edge
SPEECH_DB = -25.0  # peak above this on both sides = the cut runs through sound
FLOOR_DB = -120.0  # digital silence
_SAMPLE_CODES = {1: "b", 2: "h", 4: "i"}  # bytes per sample -> array typecode


@dataclass(frozen=True)
class EdgeReading:
    clip: str
    segment: int  # 1-based, within the clip
    kind: str  # "start" | "end"
    time: float  # source seconds
    before_db: float
    after_db: float
    words: tuple[str, ...] = ()  # whisper words spanning the edge, when a word list was given

    def voiced(self, threshold_db: float = SPEECH_DB) -> bool:
        return self.before_db > threshold_db and self.after_db > threshold_db

    def line(self, threshold_db: float = SPEECH_DB) -> str:
        flag = "VOICED" if self.voiced(threshold_db) else "ok"
        text = f" {' '.join(repr(w) for w in self.words)}" if self.words else ""
        return (f"{self.clip}\tseg {self.segment} {self.kind:5s}\t{self.time:9.3f}\t"
                f"{self.before_db:6.1f} / {self.after_db:6.1f} dB\t{flag}{text}")


class PcmAudio:
    """Peak levels over windows of a PCM WAV, read on demand (a 79-minute file is
    150 MB; the audit needs 172 windows of 40 ms)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._wav = wave.open(str(self.path), "rb")
        self.rate = self._wav.getframerate()
        self.channels = self._wav.getnchannels()
        self.width = self._wav.getsampwidth()
        self.frames = self._wav.getnframes()
        if self.width not in _SAMPLE_CODES:
            raise ValueError(f"{self.path}: {self.width * 8}-bit PCM is not supported; decode it first")
        self._full_scale = float(1 << (8 * self.width - 1))

    def close(self) -> None:
        self._wav.close()

    def __enter__(self) -> PcmAudio:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def duration(self) -> float:
        return self.frames / self.rate

    def peak_db(self, t0: float, t1: float) -> float:
        """Peak of |sample| in [t0, t1] (any channel) in dBFS; FLOOR_DB when the
        window is silent or lies outside the file."""
        a = max(0, int(round(t0 * self.rate)))
        b = min(self.frames, int(round(t1 * self.rate)))
        if b <= a:
            return FLOOR_DB
        self._wav.setpos(a)
        raw = self._wav.readframes(b - a)
        samples = array(_SAMPLE_CODES[self.width])
        samples.frombytes(raw)
        if sys.byteorder == "big":
            samples.byteswap()  # WAV data is little-endian
        if self.width == 1:
            peak = max(abs(s - 128) for s in samples)  # 8-bit WAV is unsigned
        else:
            peak = max(abs(s) for s in samples)
        if peak <= 0:
            return FLOOR_DB
        return max(FLOOR_DB, 20 * math.log10(peak / self._full_scale))


def is_pcm_wav(path: str | Path) -> bool:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getsampwidth() in _SAMPLE_CODES
    except (wave.Error, EOFError, OSError):
        return False


def decode_to_wav(source: str | Path, out: str | Path, *, rate: int = 16000) -> Path:
    """ffmpeg: any audio -> 16-bit mono PCM WAV at `rate` (argument list, never a shell string)."""
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(source), "-vn", "-sn", "-dn",
            "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", str(out)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise RuntimeError("ffmpeg not found on PATH (needed to decode audio that is not a 16-bit WAV)") from e
    if proc.returncode:
        tail = proc.stderr.strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"ffmpeg could not decode {source}: {tail[0]}")
    return Path(out)


def plan_edges(plan: dict) -> list[tuple[str, int, str, float]]:
    """(clip id, segment number, kind, time) for both edges of every segment."""
    out: list[tuple[str, int, str, float]] = []
    for clip in plan.get("clips", []):
        for n, seg in enumerate(clip.get("segments", []), 1):
            out.append((clip["id"], n, "start", float(seg["start"])))
            out.append((clip["id"], n, "end", float(seg["end"])))
    return out


def words_at(t: float, words: list[Word], slack: float = 1e-3) -> tuple[str, ...]:
    return tuple(w.text for w in words if w.start - slack <= t <= w.end + slack)


def audit_edges(plan: dict, audio: PcmAudio, *, window: float = WINDOW,
                words: list[Word] | None = None) -> list[EdgeReading]:
    """Peak levels `window` seconds before and after every segment edge."""
    out: list[EdgeReading] = []
    for clip, n, kind, t in plan_edges(plan):
        before = audio.peak_db(t - window, t)
        after = audio.peak_db(t, t + window)
        out.append(EdgeReading(clip, n, kind, t, before, after, words_at(t, words) if words else ()))
    return out


def offenders(readings: list[EdgeReading], threshold_db: float = SPEECH_DB) -> list[EdgeReading]:
    return [r for r in readings if r.voiced(threshold_db)]


def audit_plan(plan: dict, audio_path: str | Path, *, window: float = WINDOW,
               words: list[Word] | None = None) -> list[EdgeReading]:
    """`audit_edges` over `audio_path`, decoding it with ffmpeg first unless it is
    already a PCM WAV. The temporary decode lives only for the call."""
    if is_pcm_wav(audio_path):
        with PcmAudio(audio_path) as audio:
            return audit_edges(plan, audio, window=window, words=words)
    with tempfile.TemporaryDirectory(prefix="clipbot-audit-") as tmp:
        wav = decode_to_wav(audio_path, Path(tmp) / "audio.wav")
        with PcmAudio(wav) as audio:
            return audit_edges(plan, audio, window=window, words=words)


def report(readings: list[EdgeReading], *, threshold_db: float = SPEECH_DB, window: float = WINDOW,
           show_all: bool = False) -> str:
    """One line per offender (or per edge with `show_all`), then the count line
    the tests and the PR body quote: `edges: N, voiced on both sides: M`."""
    bad = offenders(readings, threshold_db)
    lines = [r.line(threshold_db) for r in (readings if show_all else bad)]
    lines.append(f"edges: {len(readings)}, voiced on both sides (> {threshold_db:g} dB within "
                 f"{int(round(window * 1000))} ms): {len(bad)}")
    return "\n".join(lines)
