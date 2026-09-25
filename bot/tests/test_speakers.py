from clipbot.captions import Cue
from clipbot.speakers import Utterance, align, parse_clock, parse_gemini

GEMINI = """✍️ Quick notes
Pipeline AI Talk #2: Build a clip bot, live
Kyle Tabor: this line is before the first anchor and must be ignored

📖 Transcript
Sep 25, 2026
Pipeline AI Talk #2: Build a clip bot, live - Transcript
00:06:20


Ramsey Jamoul: Hey. Oh, happy Friday everyone.
Kyle Tabor: Happy Friday. We should ship the renderer tonight, that is the plan.
00:07:20
Ramsey Jamoul's Presentation: the slides say beads is a git backed tracker.
Kyle Tabor: I just sent you the invite, so accept that.




Transcription ended after 01:40:53
"""


def test_parse_clock_forms():
    assert parse_clock("0:22:00") == 1320
    assert parse_clock("12:30") == 750
    assert parse_clock("45") == 45
    assert parse_clock(5) == 5.0
    assert parse_clock("-0:05") == -5


def test_parse_gemini_anchors_and_interpolation():
    utts = parse_gemini(GEMINI)
    assert [u.speaker for u in utts] == ["Ramsey Jamoul", "Kyle Tabor", "Ramsey Jamoul's Presentation", "Kyle Tabor"]
    assert utts[0].time == 380  # 00:06:20
    assert 380 < utts[1].time < 440  # interpolated inside the 60 s block by text length
    assert utts[2].time == 440  # 00:07:20
    assert utts[3].time > 440
    assert not any("before the first anchor" in u.text for u in utts)


def test_align_uses_words_and_offset():
    utts = parse_gemini(GEMINI)
    # The video started 6 minutes after the notes' clock: 00:06:20 on the notes = 0:00:20 in the video.
    cues = [
        Cue(20, 24, "Hey, oh, happy Friday everyone."),
        Cue(40, 44, "We should ship the renderer tonight."),
        Cue(84, 88, "I just sent you the invite so accept that."),
        Cue(200, 204, "Nothing in the notes says this."),
    ]
    out, n = align(cues, utts, offset=360)
    assert [c.speaker for c in out] == ["Ramsey Jamoul", "Kyle Tabor", "Kyle Tabor", None]
    assert n == 3


def test_align_fills_short_cues_from_agreeing_neighbours():
    utts = [Utterance(10, "Kyle Tabor", "the plan is to ship the renderer tonight"),
            Utterance(20, "Kyle Tabor", "and then we test it on the meeting video")]
    cues = [Cue(8, 12, "The plan is to ship the renderer tonight."), Cue(12, 14, "Okay."),
            Cue(18, 22, "And then we test it on the meeting video.")]
    out, n = align(cues, utts)
    assert [c.speaker for c in out] == ["Kyle Tabor"] * 3 and n == 3


def test_align_keeps_existing_labels_and_tolerates_nothing():
    cues = [Cue(0, 4, "hello there friend", "Ramsey Jamoul")]
    out, n = align(cues, [Utterance(0, "Kyle Tabor", "hello there friend")])
    assert out[0].speaker == "Ramsey Jamoul" and n == 0
    assert align(cues, []) == (cues, 0)
    assert align([], [Utterance(0, "K", "x")]) == ([], 0)
    assert parse_gemini("") == []
