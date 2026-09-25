from clipbot.captions import Cue, cues_to_srt, parse_srt

SRT = """﻿1
00:00:00,000 --> 00:00:04,000
(Kyle Tabor)
Okay, this is a

2
00:00:04,000 --> 00:00:08,000
(Kyle Tabor)
recording. Let's see how this works.

3
00:00:08,000 --> 00:00:08,000
(Kyle Tabor)
zero length, dropped

4
00:01:04,500 --> 00:01:08,250
no speaker line here
"""


def test_parse_basic_and_speaker():
    cues = parse_srt(SRT)
    assert [c.start for c in cues] == [0.0, 4.0, 64.5]
    assert cues[0].speaker == "Kyle Tabor"
    assert cues[0].text == "Okay, this is a"
    assert cues[2].speaker is None
    assert cues[2].end == 68.25


def test_parse_tolerates_crlf_and_missing_index():
    text = "00:00:01,000 --> 00:00:02,000\r\nhello\r\n\r\n00:00:02,000 --> 00:00:03,000\r\nworld\r\n"
    cues = parse_srt(text)
    assert [c.text for c in cues] == ["hello", "world"]


def test_parse_non_ascii():
    cues = parse_srt("1\n00:00:00,000 --> 00:00:02,000\nMāori café: the project is ready.\n")
    assert cues[0].text == "Māori café: the project is ready."


def test_extract_forces_utf8_decoding(monkeypatch):
    """Regression (PR #10 review): ffmpeg writes UTF-8; Windows locale must not decode it."""
    import subprocess

    from clipbot import captions

    seen: dict = {}

    class R:
        stdout = "1\n00:00:00,000 --> 00:00:02,000\nMāori café\n"

    def fake_run(cmd, **kw):
        seen.update(kw)
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = captions.extract_embedded_srt("x.mp4")
    assert seen.get("encoding") == "utf-8"
    assert "Māori" in out


def test_roundtrip():
    cues = [Cue(0.0, 1.5, "a"), Cue(1.5, 3.0, "b")]
    again = parse_srt(cues_to_srt(cues))
    assert [(c.start, c.end, c.text) for c in again] == [(0.0, 1.5, "a"), (1.5, 3.0, "b")]
