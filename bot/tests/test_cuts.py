import pytest

from clipbot.captions import Cue
from clipbot.cuts import (
    MAX_SEGMENTS,
    MAX_SENTENCE,
    TAIL_SECONDS,
    Span,
    filler_mask,
    pad,
    sentence_spans,
    snap_outward,
    tighten,
)
from clipbot.words import Word, words_from_cues
from tests.test_select import DEMO


def words(seq):
    return [Word(float(a), float(b), t) for a, b, t in seq]


def segs(rep):
    return [(pytest.approx(a, abs=1e-6), pytest.approx(b, abs=1e-6)) for a, b in rep.segments]


def test_sentence_spans_break_on_punctuation_gap_speaker_and_cap():
    ws = words([(0, 0.3, "We"), (0.3, 0.6, "ship."), (0.7, 1.0, "Then"), (1.0, 1.4, "test"), (3.5, 3.9, "Okay"), (3.9, 4.2, "go.")])
    sp = sentence_spans(ws)
    assert [(s.start, s.end, s.text) for s in sp] == [(0, 0.6, "We ship."), (0.7, 1.4, "Then test"), (3.5, 4.2, "Okay go.")]
    assert sp[0].first == 0 and sp[0].last == 0.6  # timed words: speech edges are the safe edges
    ws = [Word(0, 0.5, "The", "Kyle"), Word(0.5, 1.0, "step", "Kyle"), Word(1.0, 1.5, "no", "Ramsey"), Word(1.5, 2.0, "way.", "Ramsey")]
    assert [s.text for s in sentence_spans(ws)] == ["The step", "no way."]
    ws = words([(i, i + 1, f"w{i}") for i in range(30)])  # raw ASR: no punctuation for 30 s
    sp = sentence_spans(ws)
    assert len(sp) >= 2 and all(s.end - s.start <= MAX_SENTENCE for s in sp)
    assert sentence_spans([]) == []


def test_cue_derived_spans_keep_cue_edges_as_safe_points_and_estimate_speech():
    sp = sentence_spans(words_from_cues(DEMO))
    doubt = next(s for s in sp if s.text.startswith("Uh, yeah, I'm really questioning"))
    assert (doubt.start, doubt.end) == (36, 44)  # the cues the sentence begins and ends in
    assert 36 < doubt.first < 37 and 42 < doubt.last < 44  # "else?" takes the first bit of cue 36-40


def test_snap_outward_moves_to_sentence_edges_never_inward():
    ws = words([(10.0, 10.4, "We"), (10.4, 10.9, "decided."), (11.2, 11.5, "Ship"), (11.5, 11.9, "it"), (11.9, 12.6, "tonight."),
                (14.0, 14.5, "Next.")])
    spans = sentence_spans(ws)
    assert snap_outward(10.6, 11.7, spans) == (10.0, 12.6)  # mid-sentence both ends -> whole sentences
    assert snap_outward(10.0, 12.6, spans) == (10.0, 12.6)  # already on boundaries
    assert snap_outward(13.0, 13.5, spans) == (13.0, 13.5)  # in the gap between sentences
    assert snap_outward(14.2, 14.3, spans) == (14.0, 14.5)
    assert snap_outward(11.0, 11.0, spans) == (11.0, 11.0)  # 11.0 is the gap after "decided."


def test_snap_outward_is_capped_and_falls_back_to_the_speech_edge():
    # a 30 s "sentence" whose safe start sits 25 s before the request: an ASR artifact
    spans = [Span(start=0.0, end=30.0, first=19.0, last=30.0, text="garbage")]
    assert snap_outward(25.0, 26.0, spans, max_snap=12) == (19.0, 30.0)
    spans = [Span(0.0, 30.0, 5.0, 30.0, "garbage")]
    assert snap_outward(25.0, 26.0, spans, max_snap=12) == (25.0, 30.0)  # even the speech edge is too far


def test_snap_outward_is_idempotent_on_cue_derived_sentences():
    """moments.json (speech edges) fed back must re-snap to the same moments."""
    spans = sentence_spans(words_from_cues(DEMO))
    s, e = snap_outward(41, 50.5, spans)
    assert (s, e) == (36, 52)
    assert snap_outward(s, e, spans) == (s, e)
    assert snap_outward(16, 36, spans) == (16, 36)  # cue edges that end a sentence are fixed points


def test_pad_adds_lead_and_tail_within_the_source():
    assert pad(10.0, 20.0) == (pytest.approx(9.85), pytest.approx(20.3))
    assert pad(0.1, 20.0, duration=20.1) == (0.0, 20.1)
    assert pad(5.0, 6.0, lead=0, tail=TAIL_SECONDS) == (5.0, pytest.approx(6.3))


def test_filler_mask_hard_fillers_and_isolated_soft_ones():
    ws = words([(0, 0.2, "Um,"), (0.3, 0.5, "I"), (0.5, 0.8, "like"), (0.8, 1.1, "this,"), (1.2, 1.5, "like,"), (1.6, 1.9, "really."),
                (2.0, 2.2, "You"), (2.2, 2.5, "know"), (2.5, 2.8, "the"), (2.8, 3.2, "answer,"), (3.3, 3.5, "you"), (3.5, 3.8, "know,"),
                (3.9, 4.2, "uh,"), (4.3, 4.6, "right.")])
    m = filler_mask(ws)
    assert [w.text for w, f in zip(ws, m) if f] == ["Um,", "like,", "you", "know,", "uh,"]
    # pauses on both sides also isolate "like"
    ws = words([(0, 0.5, "It's"), (0.9, 1.2, "like"), (1.6, 2.0, "magic.")])
    assert filler_mask(ws) == [False, True, False]
    assert filler_mask([]) == []


def test_tighten_removes_a_filler_with_breath_and_never_cuts_inside_a_word():
    ws = words([(7.0, 7.5, "Okay"), (7.6, 9.4, "everyone"), (9.5, 9.8, "so"), (10.0, 10.4, "um"), (10.7, 10.9, "the"),
                (11.0, 11.5, "plan."), (11.6, 13.0, "Right"), (13.0, 14.0, "then.")])
    rep = tighten(6.5, 14.5, ws, [])
    assert rep.fillers == 1 and rep.filler_seconds == pytest.approx(0.4)
    assert segs(rep) == [(6.5, 9.95), (10.55, 14.5)]  # 9.8 + 0.15 breath after "so", 10.7 - 0.15 before "the"
    assert rep.removed_seconds == pytest.approx(0.6) and not rep.intact


def test_tighten_shortens_silent_word_gaps_to_keep_pause_and_clips_the_silence_to_the_words():
    ws = words([(0.0, 1.0, "Alpha"), (1.0, 2.0, "beta."), (5.0, 6.0, "Gamma"), (6.0, 7.0, "delta."), (7.5, 8.5, "Eps"), (8.5, 9.5, "end.")])
    # ffmpeg's silence 1.8-5.2 overlaps "beta." and "Gamma": the pause is their gap 2.0-5.0, cut down the
    # middle to KEEP_PAUSE (0.175 s of air each side). The 0.5 s pause at 7.0 is under max_silence and stays.
    rep = tighten(0.0, 9.5, ws, [(1.8, 5.2), (7.0, 7.5)])
    assert segs(rep) == [(0.0, 2.175), (4.825, 9.5)]
    assert rep.fillers == 0 and rep.silence_seconds == pytest.approx(2.65)


def test_tighten_shortens_even_a_one_second_gap_but_the_silence_path_still_needs_a_saving():
    """Before: a 1.0 s pause survived because cutting it to 0.7 s saved only 0.3 s. With word timings
    every silent gap over max_silence is shortened (17 such pauses survived in the second reel). The
    audio-only path (no timed words) keeps the old rule: a silence edge is not exact enough
    to justify a join for 0.3 s."""
    ws = words([(0.0, 3.0, "Alpha."), (4.0, 7.0, "Beta.")])
    assert segs(tighten(0.0, 7.0, ws, [(3.0, 4.0)])) == [(0.0, 3.175), (3.825, 7.0)]
    assert tighten(0.0, 7.0, [], [(3.0, 4.0)]).segments == ((0.0, 7.0),)


def test_tighten_fragment_rule_applies_to_filler_cuts_not_to_pause_cuts():
    # "Right." would be a 1.1 s island between two filler cuts: the shorter cut is cancelled instead
    ws = words([(0.0, 3.0, "Intro"), (3.2, 3.5, "um"), (3.7, 4.5, "Right."), (5.0, 5.3, "uh"), (5.5, 12.0, "closing words.")])
    rep = tighten(0.0, 12.0, ws, None)
    assert segs(rep) == [(0.0, 4.65), (5.35, 12.0)] and rep.fillers == 1
    # between two PAUSE cuts the same island stays: a pause cut removes no speech, so it needs no fragment rule
    ws = words([(0.0, 3.0, "Intro"), (5.0, 5.5, "Right."), (8.0, 12.0, "closing words.")])
    assert segs(tighten(0.0, 12.0, ws, None)) == [(0.0, 3.175), (4.825, 5.675), (7.825, 12.0)]


def test_tighten_respects_the_removal_cap():
    rep = tighten(0.0, 20.0, words([(0, 2, "a."), (18, 20, "b.")]), None)
    assert rep.intact and rep.segments == ((0.0, 20.0),) and "kept intact" in rep.note
    assert rep.removed_seconds == 0


def test_tighten_leading_filler_moves_the_start_without_air_at_the_edge():
    ws = words([(10.0, 10.4, "Um,"), (10.6, 12.5, "so the plan"), (12.5, 14.0, "is set.")])
    rep = tighten(9.85, 14.3, ws, [])
    assert segs(rep) == [(10.45, 14.3)] and rep.fillers == 1


def test_tighten_without_word_timings_only_cuts_silence():
    est = words_from_cues([Cue(0, 4, "um so the plan"), Cue(4, 8, "is to ship")])  # estimated, never a filler cut
    rep = tighten(0.0, 8.0, est, [(2.0, 3.5)])
    assert rep.fillers == 0 and segs(rep) == [(0.0, 2.35), (3.15, 8.0)]
    assert tighten(0.0, 8.0, [], []).segments == ((0.0, 8.0),)
    assert tighten(5.0, 5.0, [], [(5, 6)]).segments == ((5.0, 5.0),)


def test_tighten_can_be_told_to_keep_fillers():
    ws = words([(0.0, 2.0, "Alpha"), (2.1, 2.5, "um"), (2.7, 5.0, "beta.")])
    assert tighten(0.0, 5.0, ws, [], fillers=False).segments == ((0.0, 5.0),)
    assert tighten(0.0, 5.0, ws, []).fillers == 1


def test_tighten_caps_segments_at_the_contract_limit():
    ws = words([(i * 2.5, i * 2.5 + 1.0, f"w{i}.") for i in range(30)])  # 1 s words, 1.5 s pauses
    sil = [(i * 2.5 + 1.0, i * 2.5 + 2.5) for i in range(29)]
    rep = tighten(0.0, 74.0, ws, sil)
    assert len(rep.segments) == MAX_SEGMENTS and not rep.intact
    for (a, b), (c, d) in zip(rep.segments, rep.segments[1:]):
        assert a < b <= c < d
