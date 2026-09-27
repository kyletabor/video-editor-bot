"""clipbot audit-plan: the 40 ms on each side of every segment edge (clipbot/audit.py).

The fixture is a generated 16 kHz WAV with tone bursts at known times, so the
audit is checked against ground truth rather than against the cutter it audits.
"""

import json
import math
import struct
import wave
from pathlib import Path

import pytest

from clipbot import cli
from clipbot.audit import (
    FLOOR_DB,
    SPEECH_DB,
    WINDOW,
    PcmAudio,
    audit_edges,
    audit_plan,
    is_pcm_wav,
    offenders,
    plan_edges,
    report,
)
from clipbot.words import Word

RATE = 16000
# (start, end, peak dBFS) of the bursts; everything else is digital silence
BURSTS = [(1.0, 2.0, -3.0), (3.0, 3.5, -20.0), (5.0, 6.0, -40.0), (8.0, 8.02, -6.0)]


def write_wav(path: Path, seconds: float = 10.0, bursts=BURSTS, *, channels: int = 1) -> Path:
    n = int(seconds * RATE)
    samples = [0] * n
    for s, e, db in bursts:
        amp = int(32767 * 10 ** (db / 20))
        for i in range(int(s * RATE), min(n, int(e * RATE))):
            samples[i] = int(amp * math.sin(2 * math.pi * 440 * i / RATE))
    if channels == 2:
        samples = [x for x in samples for _ in range(2)]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


def plan_with(segments_by_clip: dict[str, list[tuple[float, float]]]) -> dict:
    return {"clips": [{"id": cid, "segments": [{"start": a, "end": b} for a, b in segs]} for cid, segs in segments_by_clip.items()]}


def test_peak_db_measures_windows_of_the_wav(tmp_path):
    with PcmAudio(write_wav(tmp_path / "t.wav")) as audio:
        assert audio.rate == RATE and audio.duration == pytest.approx(10.0)
        assert audio.peak_db(1.2, 1.24) == pytest.approx(-3.0, abs=0.2)
        assert audio.peak_db(3.1, 3.14) == pytest.approx(-20.0, abs=0.2)
        assert audio.peak_db(5.5, 5.54) == pytest.approx(-40.0, abs=0.3)
        assert audio.peak_db(0.5, 0.54) == FLOOR_DB  # digital silence
        assert audio.peak_db(-1.0, -0.5) == FLOOR_DB and audio.peak_db(10.5, 11.0) == FLOOR_DB  # outside the file
        assert audio.peak_db(0.98, 1.02) == pytest.approx(-3.0, abs=0.3)  # a window straddling an onset sees the burst
    with PcmAudio(write_wav(tmp_path / "s.wav", channels=2)) as stereo:
        assert stereo.channels == 2 and stereo.peak_db(1.2, 1.24) == pytest.approx(-3.0, abs=0.2)
    assert is_pcm_wav(tmp_path / "t.wav") and not is_pcm_wav(tmp_path / "missing.wav")
    (tmp_path / "not.wav").write_bytes(b"RIFF----WAVEjunk")
    assert not is_pcm_wav(tmp_path / "not.wav")


def test_audit_flags_only_edges_with_sound_on_both_sides(tmp_path):
    wav = write_wav(tmp_path / "t.wav")
    plan = plan_with({
        "clip-a": [(0.5, 1.5), (2.98, 3.25), (3.6, 4.0)],  # 1.5 and 3.25 are inside bursts; 2.98 has sound after only
        "clip-b": [(5.5, 7.0), (8.01, 9.0)],  # 5.5 is inside the -40 dB burst (quiet: not speech); 8.01 is inside a 20 ms click
    })
    assert plan_edges(plan) == [
        ("clip-a", 1, "start", 0.5), ("clip-a", 1, "end", 1.5), ("clip-a", 2, "start", 2.98), ("clip-a", 2, "end", 3.25),
        ("clip-a", 3, "start", 3.6), ("clip-a", 3, "end", 4.0), ("clip-b", 1, "start", 5.5), ("clip-b", 1, "end", 7.0),
        ("clip-b", 2, "start", 8.01), ("clip-b", 2, "end", 9.0),
    ]
    with PcmAudio(wav) as audio:
        readings = audit_edges(plan, audio)
    bad = offenders(readings)
    assert [(r.clip, r.segment, r.kind, r.time) for r in bad] == [
        ("clip-a", 1, "end", 1.5), ("clip-a", 2, "end", 3.25), ("clip-b", 2, "start", 8.01),
    ]
    inside = next(r for r in readings if r.time == 1.5)
    assert inside.before_db == pytest.approx(-3.0, abs=0.3) and inside.after_db == pytest.approx(-3.0, abs=0.3)
    onset = next(r for r in readings if r.time == 2.98)
    assert onset.before_db == FLOOR_DB and onset.after_db == pytest.approx(-20.0, abs=0.3) and not onset.voiced()
    quiet = next(r for r in readings if r.time == 5.5)
    assert quiet.before_db == pytest.approx(-40.0, abs=0.5) and not quiet.voiced()
    assert quiet.voiced(threshold_db=-45.0)  # the threshold is a parameter
    assert audit_plan(plan, wav) == readings  # a PCM WAV is read directly
    text = report(readings)
    assert text.splitlines()[-1] == f"edges: 10, voiced on both sides (> {SPEECH_DB:g} dB within {int(WINDOW * 1000)} ms): 3"
    assert text.count("VOICED") == 3 and "clip-a\tseg 1 end" in text
    assert report(readings, show_all=True).count("\n") == 10


def test_audit_names_the_word_at_an_offending_edge(tmp_path):
    wav = write_wav(tmp_path / "t.wav")
    plan = plan_with({"c": [(0.5, 1.5)]})
    ws = [Word(0.9, 1.3, "Uh,"), Word(1.4, 1.9, "it's"), Word(2.5, 3.0, "far")]
    [_, end] = audit_plan(plan, wav, words=ws)
    assert end.words == ("it's",) and "it's" in end.line() and end.line().endswith("VOICED \"it's\"")
    assert audit_plan(plan, wav)[1].words == ()


def test_cli_audit_plan_prints_offenders_and_exits_1(tmp_path, capsys):
    wav = write_wav(tmp_path / "t.wav")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan_with({"c": [(0.5, 1.5), (2.5, 4.0)]})), encoding="utf-8")
    assert cli.main(["audit-plan", str(plan_path), "--audio", str(wav)]) == 1
    out = capsys.readouterr().out
    assert "c\tseg 1 end  \t    1.500\t" in out and "VOICED" in out
    assert out.strip().endswith("voiced on both sides (> -25 dB within 40 ms): 1")
    plan_path.write_text(json.dumps(plan_with({"c": [(0.5, 0.9), (2.5, 4.0)]})), encoding="utf-8")
    assert cli.main(["audit-plan", str(plan_path), "--audio", str(wav)]) == 0
    out = capsys.readouterr().out
    assert out.strip() == "edges: 4, voiced on both sides (> -25 dB within 40 ms): 0"
    # the threshold is a flag: at -50 dB the -40 dB burst counts as sound, and --all lists every edge
    plan_path.write_text(json.dumps(plan_with({"c": [(0.5, 5.5), (7.0, 9.0)]})), encoding="utf-8")
    assert cli.main(["audit-plan", str(plan_path), "--audio", str(wav)]) == 0
    capsys.readouterr()
    assert cli.main(["audit-plan", str(plan_path), "--audio", str(wav), "--all", "--threshold-db", "-50"]) == 1
    out = capsys.readouterr().out
    assert out.count("\n") == 5 and out.count("VOICED") == 1  # every edge, then the count line
    assert out.strip().endswith("voiced on both sides (> -50 dB within 40 ms): 1")


def _ffmpeg_present() -> bool:
    import subprocess

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ffmpeg_present(), reason="needs ffmpeg")
def test_audit_decodes_audio_that_is_not_a_pcm_wav(tmp_path):
    import subprocess

    wav = write_wav(tmp_path / "t.wav")
    m4a = tmp_path / "t.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(wav), "-c:a", "aac", str(m4a)], check=True)
    assert not is_pcm_wav(m4a)
    plan = plan_with({"c": [(0.5, 1.5), (2.5, 4.0)]})
    bad = offenders(audit_plan(plan, m4a))
    assert [(r.kind, r.time) for r in bad] == [("end", 1.5)]
