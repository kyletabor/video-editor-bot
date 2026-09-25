import os
import subprocess
import sys
from pathlib import Path

import pytest

from clipbot import transcribe as tr
from clipbot.captions import parse_srt

REPO = Path(__file__).resolve().parents[2]
DEMO_MP4 = REPO / "assets" / "demo-clip.mp4"


def test_wav_args_mono_16k_no_shell():
    args = tr.wav_args("in file.mp4", "/tmp/x.wav")
    assert args[0] == "ffmpeg" and args[-1] == "/tmp/x.wav"
    for flag in (("-ac", "1"), ("-ar", "16000"), ("-vn",), ("-c:a", "pcm_s16le"), ("-nostdin",)):
        assert " ".join(flag) in " ".join(args)
    assert "in file.mp4" in args  # one argv entry, spaces intact: never a shell string


class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class _Info:
    duration = 610.0


class _FakeModel:
    calls: list = []

    def __init__(self, name):
        self.name = name

    def transcribe(self, path, **kw):
        self.calls.append((path, kw))
        segs = [_Seg(0.0, 2.5, " Okay, this is a recording. "), _Seg(2.5, 2.5, "zero length"),
                _Seg(2.6, 4.0, "   "), _Seg(299.0, 301.0, "five minutes in."), _Seg(600.0, 604.0, "the end.")]
        return iter(segs), _Info()


def test_transcribe_with_fake_model_writes_srt_and_reports_progress(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        Path(cmd[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    logs: list[str] = []
    out = tmp_path / "nested" / "talk.srt"
    cues = tr.transcribe("talk.mp4", out, model="base", model_factory=_FakeModel, log=logs.append)
    assert seen["cmd"][:2] == ["ffmpeg", "-v"] and seen["cmd"][-1].endswith("audio.wav")
    assert seen["kw"].get("shell") is None
    path, kw = _FakeModel.calls[-1]
    assert path.endswith("audio.wav") and kw["vad_filter"] is True and kw["language"] is None
    assert [(c.start, c.end, c.text) for c in cues] == [
        (0.0, 2.5, "Okay, this is a recording."), (299.0, 301.0, "five minutes in."), (600.0, 604.0, "the end.")
    ]
    again = parse_srt(out.read_text(encoding="utf-8"))
    assert [c.text for c in again] == [c.text for c in cues]
    assert any("5 of 10 min" in line for line in logs) and any("10 of 10 min" in line for line in logs)
    assert logs[-1].startswith("transcribe: done, 3 cues")


def test_missing_package_gives_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # makes `import faster_whisper` raise ImportError
    with pytest.raises(RuntimeError, match="--extra whisper"):
        tr.load_model("base")


def test_ffmpeg_failure_is_a_runtime_error(monkeypatch):
    def fail(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="boom"):
        tr.extract_wav("x.mp4", "y.wav")


def _tools_present() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def _whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("FASTER_WHISPER_TEST") != "1" or not DEMO_MP4.exists() or not _tools_present() or not _whisper_available(),
    reason="set FASTER_WHISPER_TEST=1 and run with --extra whisper",
)
def test_real_whisper_on_demo_clip(tmp_path):
    cues = tr.transcribe(DEMO_MP4, tmp_path / "demo.srt", model="base", language="en")
    assert len(cues) >= 3
    text = " ".join(c.text for c in cues).lower()
    assert "recording" in text or "video" in text
