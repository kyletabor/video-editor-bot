import subprocess

import pytest

from clipbot.silence import detect_silences, parse_silencedetect, silencedetect_args


def test_silencedetect_args_are_a_list_with_input_seeking_and_the_filter():
    args = silencedetect_args("in file.mp4", 120.5, 42.25, noise_db=-35, min_seconds=0.7)
    assert args[0] == "ffmpeg" and "in file.mp4" in args and args[-1] == "-"  # one argv entry, never a shell string
    assert args[args.index("-ss") + 1] == "120.500" and args[args.index("-t") + 1] == "42.250"
    assert args.index("-ss") < args.index("-i")  # input seeking: a moment at 73 min costs a fraction of a second
    assert args[args.index("-af") + 1] == "silencedetect=noise=-35dB:d=0.7"
    assert "-vn" in args and "-nostdin" in args


def test_parse_silencedetect_adds_the_seek_offset_and_closes_an_open_silence():
    err = (
        "[silencedetect @ 0x1] silence_start: 2.01333\n"
        "[silencedetect @ 0x1] silence_end: 5.02133 | silence_duration: 3.008\n"
        "[silencedetect @ 0x1] silence_start: 6.5\n"
    )
    found = parse_silencedetect(err, offset=3.0, span_end=10.0)
    assert found == [(pytest.approx(5.01333), pytest.approx(8.02133)), (pytest.approx(9.5), 10.0)]
    err = "[silencedetect] silence_start: -0.001\n[silencedetect] silence_end: 1.0 | silence_duration: 1.0\n"
    assert parse_silencedetect(err, 100.0, 110.0) == [(100.0, 101.0)]
    assert parse_silencedetect("", 0, 5) == []
    assert parse_silencedetect("[silencedetect] silence_end: 1.0 | silence_duration: 1.0\n", 0, 5) == []  # end without start


def test_detect_silences_reports_missing_or_failing_ffmpeg(monkeypatch):
    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="ffmpeg not found"):
        detect_silences("x.mp4", [(0, 1)])

    def fail(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "boom\nlast line")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="last line"):
        detect_silences("x.mp4", [(0, 1)])
    assert detect_silences("x.mp4", []) == []


def test_detect_silences_runs_one_ffmpeg_per_span_and_keeps_order(monkeypatch):
    seen = []

    def fake(cmd, **kw):
        seen.append(cmd)
        assert kw.get("shell") is None and kw.get("encoding") == "utf-8"
        start = float(cmd[cmd.index("-ss") + 1])
        err = "" if start > 100 else "[silencedetect] silence_start: 1.0\n[silencedetect] silence_end: 2.5 | silence_duration: 1.5\n"
        return subprocess.CompletedProcess(cmd, 0, "", err)

    monkeypatch.setattr(subprocess, "run", fake)
    out = detect_silences("x.mp4", [(10.0, 20.0), (200.0, 210.0), (0.0, 5.0), (7.0, 7.0)])
    assert out == [[(11.0, 12.5)], [], [(1.0, 2.5)], []]
    assert len(seen) == 3  # the empty span is not analysed


def _ffmpeg_present() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ffmpeg_present(), reason="needs ffmpeg")
def test_detect_silences_on_a_generated_tone(tmp_path):
    """Offset semantics of `-ss` before `-i` on the ffmpeg that is on PATH: a 12 s tone
    muted from 5 to 8 s, analysed from 3 to 10 s, must come back in source time."""
    tone = tmp_path / "tone.m4a"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
         "-af", "volume=enable='between(t,5,8)':volume=0", "-c:a", "aac", str(tone)],
        check=True, capture_output=True,
    )
    [found] = detect_silences(tone, [(3.0, 10.0)])
    assert len(found) == 1
    (s, e), = found
    assert abs(s - 5.0) < 0.15 and abs(e - 8.0) < 0.15
