import pytest

from clipbot.captions import Cue
from clipbot.cuts import (
    KEEP_PAUSE,
    LEAD_SECONDS,
    MAX_SEGMENTS,
    MAX_SENTENCE,
    TAIL_SECONDS,
    Span,
    filler_mask,
    pad,
    sentence_spans,
    snap_outward,
    speech_spans,
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


def test_tighten_removes_a_filler_between_two_pauses_and_never_cuts_inside_a_word():
    """A filler is the voiced blob between the pause before it and the pause after it (cuts.py
    docstring, 4). Here the audio has "so [0.2 s] um [0.3 s] the": the cut runs from
    KEEP_PAUSE/2 into the first pause to KEEP_PAUSE/2 before the end of the second, so the join is
    a 0.35 s pause and never a splice of sound. Whisper's "um" edges (10.0-10.4) are 0.05 s off the
    pauses and play no part."""
    ws = words([(7.0, 7.5, "Okay"), (7.6, 9.4, "everyone"), (9.5, 9.8, "so"), (10.0, 10.4, "um"), (10.7, 10.9, "the"),
                (11.0, 11.5, "plan."), (11.6, 13.0, "Right"), (13.0, 14.0, "then.")])
    rep = tighten(6.5, 14.5, ws, [(9.8, 10.0), (10.4, 10.7)])
    assert rep.fillers == 1 and rep.filler_seconds == pytest.approx(0.4) and rep.embedded == 0
    assert segs(rep) == [(6.5, 9.975), (10.525, 14.5)]
    assert rep.removed_seconds == pytest.approx(0.55) and not rep.intact
    # a pause shorter than KEEP_PAUSE/2 is kept whole: the cut starts where the pause ends
    assert segs(tighten(6.5, 14.5, ws, [(9.85, 10.0), (10.4, 10.7)])) == [(6.5, 10.0), (10.525, 14.5)]
    # the audio was checked and found no pause on either side ("so um the" in one breath): the "um"
    # stays and is reported as embedded, because a cut through a word is worse than an "um"
    rep = tighten(6.5, 14.5, ws, [])
    assert rep.segments == ((6.5, 14.5),) and rep.fillers == 0 and rep.embedded == 1
    assert tighten(6.5, 14.5, ws, [(9.8, 10.0)]).embedded == 1  # a pause on one side only
    # without silence data (ffmpeg unavailable) whisper's edges are all there is: word edge + breath
    # (and the word gaps at the edges are trusted too, so the head and tail are trimmed to lead/tail)
    rep = tighten(6.5, 14.5, ws, None)
    assert segs(rep) == [(6.85, 9.95), (10.55, 14.3)] and rep.fillers == 1 and rep.embedded == 0


def test_tighten_shortens_silent_word_gaps_to_keep_pause_and_measures_the_pause_on_the_audio():
    ws = words([(0.0, 1.0, "Alpha"), (1.0, 2.0, "beta."), (5.0, 6.0, "Gamma"), (6.0, 7.0, "delta."), (7.5, 8.5, "Eps"), (8.5, 9.5, "end.")])
    # ffmpeg's silence 1.8-5.2 overlaps "beta." and "Gamma" by 0.2 s each: whisper stretched both words
    # over the pause, so the pause is the silence 1.8-5.2, cut down the middle to KEEP_PAUSE (0.175 s of
    # silence each side of the join). The 0.5 s pause at 7.0 is under max_silence and stays.
    rep = tighten(0.0, 9.5, ws, [(1.8, 5.2), (7.0, 7.5)])
    assert segs(rep) == [(0.0, 1.975), (5.025, 9.5)]
    assert rep.fillers == 0 and rep.silence_seconds == pytest.approx(3.05)


def test_speech_spans_gives_up_silence_inside_a_run_but_never_a_run_inside_silence():
    """talk2 655 s: whisper's "content," spans 1.66 s with 1.25 s of dead air inside (cuts.py docstring, 3)."""
    ws = words([(654.57, 655.21, "digestible"), (655.21, 656.87, "content,"), (657.13, 657.23, "right?")])
    assert speech_spans(ws, [(655.38, 656.63)]) == [(654.57, 655.38), (656.63, 656.87), (657.13, 657.23)]
    # a run whisper placed inside silence (a hallucination, or a speaker under the detector's threshold) stays whole
    assert speech_spans(ws, [(654.5, 657.0)]) == [(654.57, 656.87), (657.13, 657.23)]
    # a sliver of sound (< VOICED_MARGIN) beside the silence is not enough to trust the detector over whisper
    assert speech_spans(ws, [(654.57, 656.80)]) == [(654.57, 656.87), (657.13, 657.23)]
    assert speech_spans(ws, None) == speech_spans(ws, []) == [(654.57, 656.87), (657.13, 657.23)]
    assert speech_spans([], [(1.0, 2.0)]) == []


def test_tighten_shortens_a_pause_whisper_hid_inside_a_word():
    """Case 1 of the third reel's verifier: "content," 655.21-656.87 held a 1.25 s silence; the cut
    lands inside the whisper span, KEEP_PAUSE/2 inside the silence on each side."""
    ws = words([(653.87, 654.57, "easily"), (654.57, 655.21, "digestible"), (655.21, 656.87, "content,"), (657.13, 657.23, "right?")])
    rep = tighten(653.72, 657.53, ws, [(655.38, 656.63)])
    assert segs(rep) == [(653.72, 655.555), (656.455, 657.53)]
    assert rep.silence_seconds == pytest.approx(1.25 - 0.35) and rep.fillers == 0
    # "flipped" 950.03-952.09 with silence 950.86-952.02: only 0.07 s of the WORD follows the silence, but the
    # run goes on ("transitions where"), so the silence is a pause inside the run
    ws = words([(949.07, 950.03, "having"), (950.03, 952.09, "flipped"), (952.09, 952.81, "transitions"), (952.81, 953.87, "where")])
    assert segs(tighten(948.92, 954.17, ws, [(950.86, 952.02)])) == [(948.92, 951.035), (951.845, 954.17)]
    # were "transitions" not abutting, the run would end 0.07 s after the silence: too little sound to
    # trust, the word is left whole and the pause stays (the conservative failure)
    apart = words([(949.07, 950.03, "having"), (950.03, 952.09, "flipped"), (952.2, 952.81, "transitions"), (952.81, 953.87, "where")])
    assert tighten(948.92, 954.17, apart, [(950.86, 952.02)]).segments == ((948.92, 954.17),)


def test_tighten_trims_a_stretched_word_end_so_the_splice_holds_only_keep_pause():
    """Case 2: "for" 3949.96-3951.16 ended 1.1 s after the sound stopped; the old cut trusted the word
    end and the splice "for | us" held 1.39 s of silence."""
    ws = words([(3948.66, 3949.02, "your"), (3949.02, 3949.46, "agent"), (3949.46, 3949.68, "real"), (3949.68, 3949.96, "quick"),
                (3949.96, 3951.16, "for"), (3952.44, 3952.70, "us"), (3952.70, 3952.9, "to"), (3952.9, 3953.8, "do that.")])
    silence = (3950.12, 3952.30)
    rep = tighten(3948.51, 3954.1, ws, [silence])
    assert segs(rep) == [(3948.51, 3950.295), (3952.125, 3954.1)]
    (_, cut_a), (cut_b, _) = rep.segments
    assert (cut_a - silence[0]) + (silence[1] - cut_b) == pytest.approx(KEEP_PAUSE)  # what the viewer hears
    assert rep.silence_seconds == pytest.approx(2.18 - KEEP_PAUSE)


def test_tighten_trims_stretched_silence_at_the_moments_own_edges_to_lead_and_tail():
    """Case 3: the last word "while." 3979.03-3979.99 stopped sounding at 3979.23; with 0.3 s of tail
    air the clip ended on 1.06 s of nothing. The tail is measured from where the sound stops."""
    ws = words([(3976.99, 3978.15, "not"), (3978.15, 3978.43, "touch"), (3978.43, 3978.67, "anything"), (3978.67, 3978.91, "for"),
                (3978.91, 3979.03, "a"), (3979.03, 3979.99, "while.")])
    end = 3979.99 + TAIL_SECONDS
    rep = tighten(3976.99 - LEAD_SECONDS, end, ws, [(3979.23, end)])
    assert segs(rep) == [(3976.84, 3979.23 + TAIL_SECONDS)] and rep.silence_seconds == pytest.approx(0.76)
    # the mirror image at the start: whisper's first word begins 0.6 s before the sound does
    ws = words([(10.0, 11.0, "So"), (11.0, 11.5, "then"), (11.5, 14.0, "we shipped it.")])
    rep = tighten(10.0 - LEAD_SECONDS, 14.3, ws, [(9.85, 10.6)])
    assert segs(rep) == [(10.6 - LEAD_SECONDS, 14.3)]
    # air that is not silent (a breath before the first word) is not trimmed
    assert tighten(10.0 - LEAD_SECONDS, 14.3, ws, []).segments == ((9.85, 14.3),)
    # the moment's own lead/tail can be set (cli --lead-seconds/--tail-seconds)
    assert segs(tighten(9.85, 14.3, ws, [(9.85, 10.6)], lead=0.3)) == [(10.3, 14.3)]


def test_tighten_measures_a_pause_across_words_whisper_shifted_into_dead_air():
    """Case 4: "But well," 138.26-139.22 sits in -80 dB silence; the sound starts at 139.05. The word
    gap before it (135.84-138.26) held two silences of which only 0.65 s lay in the gap, so nothing
    was cut and 1.44 s of nothing reached the reel. The run keeps 0.17 s of sound, so the audio wins."""
    ws = words([(134.64, 134.74, "do"), (134.74, 134.84, "the"), (134.84, 135.04, "full"), (135.04, 135.28, "scope"), (135.28, 135.52, "all"),
                (135.52, 135.84, "itself."), (138.26, 138.74, "But"), (138.74, 139.22, "well,"), (139.40, 139.74, "that's"),
                (139.74, 139.98, "no"), (139.98, 140.22, "fun"), (140.22, 140.40, "to"), (140.40, 141.5, "work with.")])
    rep = tighten(133.0, 141.8, ws, [(135.87, 137.17), (137.61, 139.05)])
    # both silences are cut; the 0.44 s of untranscribed sound between them is too short to stand
    # between two joins and goes with the pause (one cut), as a cough does
    assert segs(rep) == [(133.0, 136.045), (138.875, 141.8)]
    # "alive. [0.94 s] Yeah," where whisper's "Yeah," starts 0.33 s early: the same rule, one word
    ws = words([(2017.55, 2017.75, "it's"), (2017.75, 2017.97, "not"), (2017.97, 2018.21, "made"), (2018.21, 2018.33, "to"),
                (2018.33, 2018.45, "be"), (2018.45, 2018.91, "alive."), (2019.51, 2019.99, "Yeah,"), (2020.13, 2020.33, "okay."),
                (2020.5, 2022.0, "Let's move on.")])
    assert segs(tighten(2017.4, 2022.3, ws, [(2018.90, 2019.84)])) == [(2017.4, 2019.075), (2019.665, 2022.3)]


def test_filler_cut_is_anchored_to_the_pauses_not_to_whispers_filler_edges():
    """"so" is stretched 0.7 s past its sound and the "um" follows the pause. The cut is measured
    on the two pauses: it used to start 0.15 s after whisper's word end and leave 0.85 s of nothing
    before the join; and the fourth reel's joins sat inside "Uh," at -1 dB because whisper's edges
    for a filler are 50-150 ms off. Without audio whisper's edges are all there is."""
    ws = words([(7.0, 9.5, "And then we said"), (9.5, 10.6, "so"), (10.6, 10.9, "um"), (11.15, 11.4, "the"), (11.4, 14.0, "plan is set.")])
    rep = tighten(6.85, 14.3, ws, [(9.9, 10.6), (10.95, 11.1)])
    assert segs(rep) == [(6.85, 10.075), (10.95, 14.3)] and rep.fillers == 1 and rep.embedded == 0
    assert segs(tighten(6.85, 14.3, ws, None)) == [(6.85, 10.75), (11.0, 14.3)]  # no audio: whisper's edge
    # the pause after must begin within FILLER_REACH of the filler's end: one 0.4 s later belongs to
    # whatever follows ("um" ran into "the"), so the "um" is embedded and stays
    assert tighten(6.85, 14.3, ws, [(9.9, 10.6), (11.3, 11.5)]).embedded == 1
    # a filler whisper stretched over the pause before it (talk2 4405.18-4406.90 "uh," of which
    # 1.5 s is the pause) is measured from its acoustic edges, so it still finds that pause
    ws = words([(4404.46, 4405.08, "learned,"), (4405.18, 4406.90, "uh,"), (4407.14, 4407.48, "bots"), (4407.48, 4408.38, "do"),
                (4408.38, 4408.56, "not"), (4408.56, 4410.0, "communicate.")])
    rep = tighten(4404.31, 4410.3, ws, [(4405.109, 4406.667), (4407.059, 4407.195), (4407.766, 4408.218)])
    assert segs(rep) == [(4404.31, 4405.284), (4407.059, 4410.3)] and rep.fillers == 1
    # a pause that begins deeper than ONSET_SLOP into the next word is a stop closure in that word
    # ("uh, a-b-out"): cutting from it would take the "a", so the "uh," is embedded instead
    rep = tighten(4404.31, 4410.3, ws, [(4405.109, 4406.667), (4407.25, 4407.40)])
    assert rep.fillers == 0 and rep.embedded == 1
    # ...and the mirror image before it: a closure inside "learned," is not the pause before the filler
    rep = tighten(4404.31, 4410.3, ws, [(4404.7, 4404.85), (4407.059, 4407.195)])
    assert rep.fillers == 0 and rep.embedded == 1
    # a blob that also holds a kept word is not a filler blob: "Uh, yeah," in one breath (talk2 3960)
    ws = words([(3958.0, 3959.22, "you want me to do that?"), (3960.10, 3960.54, "Uh,"), (3960.64, 3960.78, "yeah,"), (3960.92, 3961.8, "for the beads,")])
    rep = tighten(3957.85, 3962.1, ws, [(3959.212, 3960.315), (3960.855, 3961.010)])
    assert rep.embedded == 1 and rep.fillers == 0
    assert segs(rep) == [(3957.85, 3959.387), (3960.14, 3962.1)]  # the pause before it is still shortened


def test_removal_cap_counts_cuts_that_could_hold_speech_not_audio_confirmed_silence():
    """The cap guards against cutting a stretch whisper did not transcribe. A pause cut the audio
    confirmed holds nothing but silence, so a moment that is half dead air is tightened, not kept
    whole (talk2's moment 6 gives up 13.8 s of its 36 s); without silence data the same cuts might
    be untranscribed speech and the cap holds."""
    ws = words([(0, 2, "a."), (18, 20, "b.")])
    rep = tighten(0.0, 20.0, ws, [(2.0, 18.0)])
    assert segs(rep) == [(0.0, 2.175), (17.825, 20.0)] and not rep.intact
    assert tighten(0.0, 20.0, ws, None).intact
    # the voiced part of a filler cut always counts: whisper's "um" may be a mislabelled word
    ws = words([(0, 3.0, "So the plan is"), (3.2, 5.2, "um"), (5.2, 7.2, "uh"), (7.2, 9.2, "um"), (9.4, 12.0, "go on.")])
    rep = tighten(0.0, 12.0, ws, [(3.0, 3.2), (9.2, 9.4)])
    assert rep.intact and "50%" in rep.note  # 6.0 s of sound out of 12
    assert "51%" in tighten(0.0, 12.0, ws, None).note


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


def test_tighten_leading_filler_moves_the_start_to_lead_before_the_pause_after_it():
    """A moment that opens on "Um," starts on the word after it: `lead` of air before the end of the
    pause that follows the filler, not before whisper's timestamp for that word (the fourth reel
    opened one clip on the tail of an "Um," at -3 dB, its cut 0.15 s before whisper's next word)."""
    ws = words([(10.0, 10.4, "Um,"), (10.6, 12.5, "so the plan"), (12.5, 14.0, "is set.")])
    rep = tighten(9.85, 14.3, ws, [(9.4, 9.98), (10.4, 10.65)])
    assert segs(rep) == [(10.5, 14.3)] and rep.fillers == 1
    assert segs(tighten(9.85, 14.3, ws, None)) == [(10.45, 14.3)]  # no audio: breath after whisper's word end
    # "Um, so" in one breath: the "Um," stays; the silence before it is trimmed to `lead` as always
    rep = tighten(9.85, 14.3, ws, [(9.4, 10.05)])
    assert segs(rep) == [(9.9, 14.3)] and rep.fillers == 0 and rep.embedded == 1
    # the mirror image: a trailing filler leaves `tail` of air after the pause before it
    ws = words([(10.0, 12.0, "The plan is set,"), (12.3, 12.6, "um."), (14.0, 15.0, "Next.")])
    assert segs(tighten(9.85, 12.9, ws, [(12.05, 12.25), (12.6, 12.9)])) == [(9.85, 12.25)]


def test_tighten_without_word_timings_only_cuts_silence():
    est = words_from_cues([Cue(0, 4, "um so the plan"), Cue(4, 8, "is to ship")])  # estimated, never a filler cut
    rep = tighten(0.0, 8.0, est, [(2.0, 3.5)])
    assert rep.fillers == 0 and segs(rep) == [(0.0, 2.35), (3.15, 8.0)]
    assert tighten(0.0, 8.0, [], []).segments == ((0.0, 8.0),)
    assert tighten(5.0, 5.0, [], [(5, 6)]).segments == ((5.0, 5.0),)


def test_tighten_can_be_told_to_keep_fillers():
    ws = words([(0.0, 2.0, "Alpha"), (2.1, 2.5, "um"), (2.7, 5.0, "beta.")])
    sil = [(2.0, 2.15), (2.5, 2.7)]
    rep = tighten(0.0, 5.0, ws, sil, fillers=False)
    assert rep.segments == ((0.0, 5.0),) and rep.embedded == 0
    assert tighten(0.0, 5.0, ws, sil).fillers == 1


def test_tighten_caps_segments_at_the_contract_limit():
    ws = words([(i * 2.5, i * 2.5 + 1.0, f"w{i}.") for i in range(30)])  # 1 s words, 1.5 s pauses
    sil = [(i * 2.5 + 1.0, i * 2.5 + 2.5) for i in range(29)]
    rep = tighten(0.0, 74.0, ws, sil)
    assert len(rep.segments) == MAX_SEGMENTS and not rep.intact
    for (a, b), (c, d) in zip(rep.segments, rep.segments[1:]):
        assert a < b <= c < d
