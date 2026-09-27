"""Optional faster-whisper adapter: source audio -> SRT -> cues (`--transcribe`).

Why an adapter and not a dependency: whisper models are a 100 MB+ download and
CTranslate2 wheels are platform specific, while `clipbot plan` on a Meet
recording with embedded captions must keep working with nothing but jsonschema
installed. So the import is lazy and the extra is opt-in:

    uv run --project bot --extra whisper clipbot reel --transcribe ...

Measured on Kyle's Pi (ARM, 4 threads): model="base", compute_type="int8" runs
at about 5.7x realtime, so a 79-minute talk transcribes in roughly 14 minutes.
Audio is extracted to a mono 16 kHz WAV first (what whisper wants; ffmpeg does
the decoding so we never depend on faster-whisper's optional audio backends).

Word timestamps (``word_timestamps=True``) are always on and land in
``<name>.words.json`` next to the SRT (words.py): cuts.py needs word edges to
snap cuts to sentences precisely and to remove fillers without clipping a word.
Whisper is trained on transcripts that omit "um" and "uh"; the first talk2 pass
kept 5 of the ~270 fillers Meet's captions show. `DISFLUENCY_PROMPT` is the
standard counter-measure: an initial prompt written in the style we want, so the
decoder keeps disfluencies instead of cleaning them up.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from .captions import Cue, cues_to_srt
from .words import Word, save_words, words_path

INSTALL_HINT = "faster-whisper is not installed; install with: uv run --project bot --extra whisper clipbot ..."
PROGRESS_EVERY = 300.0  # seconds of audio between progress lines
# Style prompt that keeps "um"/"uh"/"like" in the transcript (module docstring). English
# only: it is passed when the language is English or still to be detected.
DISFLUENCY_PROMPT = "Um, so, uh, I think we should, like, you know, hmm, try that. Uh, okay."


def wav_args(source: str | Path, wav: str | Path) -> list[str]:
    """ffmpeg argument list (no shell) for a mono 16 kHz PCM WAV. Works on ffmpeg 4.4 and 7."""
    return [
        "ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(wav),
    ]


def extract_wav(source: str | Path, wav: str | Path) -> None:
    try:
        subprocess.run(wav_args(source, wav), check=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise RuntimeError("ffmpeg not found on PATH") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffmpeg audio extraction failed: {e.stderr.strip()}") from e


def load_model(model: str = "base", factory: Callable[[str], object] | None = None, cpu_threads: int = 4):
    """Build the whisper model; `factory` lets tests inject a fake without the package."""
    if factory is not None:
        return factory(model)
    try:
        from faster_whisper import WhisperModel  # heavy, optional
    except ImportError as e:
        raise RuntimeError(INSTALL_HINT) from e
    return WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=cpu_threads)


def transcribe(
    source: str | Path,
    out_srt: str | Path,
    model: str = "base",
    language: str | None = None,
    *,
    model_factory: Callable[[str], object] | None = None,
    log: Callable[[str], None] | None = None,
    words_out: str | Path | None = None,
    keep_disfluencies: bool = True,
) -> list[Cue]:
    """Transcribe `source`, write SRT to `out_srt` and word timings to
    `words_out` (default: `<out_srt>.words.json`, see words.words_path), return the cues.

    Progress goes to `log` (stderr by default) every ~5 minutes of audio because
    a long recording is silent for a quarter of an hour otherwise."""
    log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
    out_srt = Path(out_srt)
    words_out = Path(words_out) if words_out else words_path(out_srt)
    cues: list[Cue] = []
    words: list[Word] = []
    prompt = DISFLUENCY_PROMPT if keep_disfluencies and language in (None, "en") else None
    with tempfile.TemporaryDirectory(prefix="clipbot-whisper-") as tmp:
        wav = Path(tmp) / "audio.wav"
        extract_wav(source, wav)
        whisper = load_model(model, model_factory)
        segments, info = whisper.transcribe(
            str(wav), beam_size=1, vad_filter=True, language=language,
            word_timestamps=True, initial_prompt=prompt,
        )
        total_min = float(getattr(info, "duration", 0.0) or 0.0) / 60
        next_mark = PROGRESS_EVERY
        for seg in segments:  # a generator: inference happens while we iterate
            text = seg.text.strip()
            if text and seg.end > seg.start:
                cues.append(Cue(float(seg.start), float(seg.end), text))
            for w in getattr(seg, "words", None) or ():
                wtext = str(getattr(w, "word", "")).strip()
                if wtext and float(w.end) > float(w.start):
                    words.append(Word(float(w.start), float(w.end), wtext))
            if seg.end >= next_mark:
                log(f"transcribe: {seg.end / 60:.0f} of {total_min:.0f} min, {len(cues)} cues")
                next_mark += PROGRESS_EVERY
    out_srt.parent.mkdir(parents=True, exist_ok=True)
    out_srt.write_text(cues_to_srt(cues), encoding="utf-8")
    save_words(words_out, words)
    log(f"transcribe: done, {len(cues)} cues, {len(words)} words -> {out_srt} + {words_out.name}")
    return cues
