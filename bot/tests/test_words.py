from pathlib import Path

import pytest

from clipbot.captions import Cue
from clipbot.words import Word, load_words, save_words, words_from_cues, words_path


def test_words_from_cues_estimates_by_character_share_and_keeps_cue_edges_safe():
    cues = [Cue(0, 4, "Okay, this is a", "K"), Cue(4, 8, "recording. Done.", "K"), Cue(8, 8, "zero length"), Cue(9, 10, "   ")]
    ws = words_from_cues(cues)
    assert [w.text for w in ws] == ["Okay,", "this", "is", "a", "recording.", "Done."]
    assert all(not w.timed for w in ws)
    assert ws[0].start == 0 and ws[3].end == 4  # a cue's words tile the cue
    assert ws[0].safe_start == 0 and ws[0].safe_end == 4 and ws[4].safe_start == 4 and ws[5].safe_end == 8
    assert ws[0].end == pytest.approx(1.5)  # "Okay," + space = 6 of 16 characters -> 4 s * 6/16
    assert ws[0].speaker == "K"
    assert [w.start for w in ws] == sorted(w.start for w in ws)


def test_timed_words_are_their_own_safe_cut_points():
    w = Word(1.0, 1.5, "hi")
    assert w.timed and w.safe_start == 1.0 and w.safe_end == 1.5 and w.duration == 0.5


def test_words_round_trip_and_tolerant_load(tmp_path):
    p = tmp_path / "nested" / "t.words.json"
    save_words(p, [Word(0.0, 0.5, "Okay,"), Word(0.6, 0.9, "so")])
    assert [(w.start, w.end, w.text, w.timed) for w in load_words(p)] == [(0.0, 0.5, "Okay,", True), (0.6, 0.9, "so", True)]
    p.write_text(
        '[{"start": 1, "end": 2, "word": "b"}, {"start": 0, "end": 0.5, "word": " a "}, {"start": "x", "end": 1, "word": "bad"},'
        ' {"start": 3, "end": 3, "word": "zero"}, {"start": 4, "end": 5, "word": "  "}, 7, {"start": 6, "end": 7, "text": "c"},'
        ' {"start": 9, "end": 8, "word": "backwards"}]',
        encoding="utf-8",
    )
    # sorted, junk skipped, faster-whisper's `word` or `text`; a zero-length word is a spoken word with a
    # collapsed timestamp and stays (as a point), an end before the start is junk
    assert [w.text for w in load_words(p)] == ["a", "b", "zero", "c"]
    assert [w for w in load_words(p) if w.text == "zero"][0].duration == 0
    p.write_text('{"not": "a list"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_words(p)


def test_words_path_sits_next_to_the_srt():
    assert words_path("out/talk/talk.srt").name == "talk.words.json"
    assert words_path(Path("x/y.z.srt")) == Path("x/y.z.words.json")
