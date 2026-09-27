"""Word-level boundaries and pause shortening (cuts.WordSnapper, cuts.tighten, reel.cut_moments).

The independent check of the second talk2 reel found two kept segments ending
inside a word and 17 pauses over 0.9 s left standing. The fixtures below are the
whisper word timings around those cuts, verbatim, so the tests fail on exactly
what a viewer heard.
"""

import json
import subprocess
from pathlib import Path

import pytest

from clipbot import cli
from clipbot.audit import audit_plan, offenders
from clipbot.captions import Cue
from clipbot.cuts import (
    BREATH_SECONDS,
    EDGE_REACH,
    KEEP_PAUSE,
    LEAD_SECONDS,
    MAX_SILENCE,
    MIN_PAUSE,
    ONSET_SLOP,
    TAIL_SECONDS,
    WORD_MARGIN,
    Snapper,
    WordSnapper,
    anchor_edges,
    filler_mask,
    tighten,
)
from clipbot.plan import REPO_ROOT
from clipbot.probe import SourceInfo
from clipbot.reel import (
    Moment,
    build_reel_plan,
    cut_moments,
    detection_spans,
    moments_from_specs,
    plan_runtime,
    snapper_for,
)
from clipbot.silence import detect_silences
from clipbot.words import Word, load_words


def words(seq):
    return [Word(float(a), float(b), t) for a, b, t in seq]


def segs(rep):
    return [(pytest.approx(a, abs=1e-6), pytest.approx(b, abs=1e-6)) for a, b in rep.segments]


# talk2 57:34-57:46 (clip-08 of the second reel): "...before it merges. [1.62 s] So it must just be..."
# The spec end 3458.11 was a cue end in that pause; +0.3 s of tail landed inside the 2 s "it".
MERGES = words([
    (3454.31, 3454.57, "review"), (3454.57, 3454.77, "it"), (3454.77, 3455.01, "and"), (3455.01, 3455.27, "approve"),
    (3455.27, 3455.51, "it"), (3455.51, 3455.79, "before"), (3455.79, 3456.05, "it"), (3456.05, 3456.53, "merges."),
    (3458.15, 3458.29, "So"), (3458.29, 3460.47, "it"), (3460.47, 3460.67, "must"), (3460.67, 3460.97, "just"),
    (3460.97, 3461.31, "be"), (3461.31, 3461.91, "determining"), (3461.91, 3462.29, "what"), (3462.29, 3462.45, "the"),
    (3462.45, 3462.79, "task"), (3462.79, 3463.27, "is"), (3463.27, 3463.47, "if"), (3463.47, 3463.79, "it's"),
    (3463.79, 3464.83, "which"), (3464.83, 3465.11, "part"), (3465.11, 3465.25, "of"), (3465.25, 3465.37, "the"),
    (3465.37, 3465.67, "stage"), (3465.67, 3465.99, "it's"), (3465.99, 3466.13, "at."),
])
# talk2 1:06:06 (clip-09): "...at the same time. [0.64 s] Hold on for a second". Spec end 3967.79, 0.02 s before "Hold".
HOLD_ON = words([
    (3966.45, 3966.65, "at"), (3966.65, 3966.73, "the"), (3966.73, 3966.93, "same"), (3966.93, 3967.17, "time."),
    (3967.81, 3967.99, "Hold"), (3967.99, 3968.25, "on"), (3968.25, 3968.47, "for"), (3968.47, 3968.59, "a"),
    (3968.59, 3968.79, "second"),
])


def straddled(t, ws):
    return [w for w in ws if w.start + 1e-9 < t < w.end - 1e-9]


def test_end_is_a_word_end_and_prefers_the_sentence_end():
    snap = WordSnapper(MERGES)
    assert snap.end(3455.6) == 3456.53  # inside "before": on to "merges."
    assert snap.end(3459.0) == 3466.13  # inside the 2 s "it": on to "at."
    assert snap.end(3455.01) == 3456.53  # exactly at a word end mid-sentence ("and|approve"): on to the sentence end
    assert snap.end(3456.53) == 3456.53  # a sentence end is a fixed point (moments.json round trip)
    assert snap.end(3458.11) == 3456.53  # the verifier's case: a cue end in the pause after "merges." moves back across silence
    assert snap.end(3458.15) == 3456.53  # exactly at the start of "So": the end of what came before
    assert WordSnapper(HOLD_ON).end(3967.79) == 3967.17  # 0.02 s before "Hold": back to "time."
    for t in (3454.4, 3455.6, 3457.0, 3458.2, 3459.0, 3463.0, 3466.0):
        assert not straddled(snap.end(t), MERGES), t
    assert snap.end(3470.0) == 3466.13  # after the last word: the last sentence end
    assert WordSnapper([]).end(5.0) == 5.0


def test_end_cap_falls_back_to_a_breath_then_to_the_last_word_end():
    # 12 s of unpunctuated speech with one 0.3 s breath after "d"; the period is at 12.3 s
    ws = words([(0, 1, "a"), (1, 2, "b"), (2, 3, "c"), (3, 4, "d"), (4.3, 5.3, "e"), (5.3, 6.3, "f"), (6.3, 7.3, "g"),
                (7.3, 8.3, "h"), (8.3, 9.3, "i"), (9.3, 10.3, "j"), (10.3, 11.3, "k"), (11.3, 12.3, "l.")])
    snap = WordSnapper(ws)
    assert snap.end(0.5) == 4.0  # "l." is 11.8 s away, past the 8 s cap: the breath after "d" is the best cut
    assert snap.end(5.0) == 12.3  # inside "e": the sentence end is within reach
    assert WordSnapper(ws, max_end=2.0).end(0.5) == 2.0  # no breath within reach either: the last word end before the cap
    assert WordSnapper(ws, max_end=0.2).end(0.5) == 1.0  # the word containing t always counts, cap or no cap
    assert snap.end(-3.0) == 4.0  # before the first word: extends from the first word
    assert WordSnapper(ws).end(-20.0) == -20.0  # the first word is beyond the cap: nothing to end on


def test_start_is_a_word_start_and_prefers_the_sentence_start():
    snap = WordSnapper(MERGES)
    assert snap.start(3461.0) == 3458.15  # inside "be": back to "So"
    assert snap.start(3460.47) == 3458.15  # exactly at a word edge mid-sentence ("it|must"): back to the sentence start
    assert snap.start(3457.0) == 3458.15  # in the pause after "merges.": forward to "So" (across silence only)
    assert snap.start(3456.53) == 3458.15  # exactly at a sentence end: the next sentence
    assert snap.start(3458.15) == 3458.15  # a sentence start is a fixed point
    assert snap.start(3450.0) == 3454.31  # before the first word: the first word
    for t in (3454.4, 3455.6, 3457.0, 3458.2, 3459.0, 3463.0, 3466.0):
        assert not straddled(snap.start(t), MERGES), t
    assert snap.start(3470.0) == 3470.0  # after the last word: nothing to start on
    # 6 s reach: a 13 s run-on with 0.1 s gaps and no punctuation until "end."
    ws = words([(i, i + 0.9, f"w{i}") for i in range(12)] + [(12, 12.9, "end.")])
    assert WordSnapper(ws).start(11.5) == 11.0  # no sentence start or breath within 6 s: the word containing t
    ws = words([(i, i + 0.9, f"w{i}") for i in range(7)] + [(7.2, 7.9, "w7")] + [(i, i + 0.9, f"w{i}") for i in range(8, 12)]
               + [(12, 12.9, "end.")])
    assert WordSnapper(ws).start(10.5) == 7.2  # the 0.3 s breath before w7 is the furthest cut within reach


def test_snapping_never_loses_a_word_the_request_touched_and_is_idempotent():
    snap = WordSnapper(MERGES)
    for s, e in [(3455.6, 3458.11), (3459.0, 3462.0), (3454.4, 3465.0), (3457.0, 3457.5), (3456.53, 3458.15)]:
        s1, e1 = snap.snap(s, e)
        assert snap.snap(s1, e1) == (s1, e1), (s, e)
        touched = [w for w in MERGES if w.end > s and w.start < e]
        assert all(s1 <= w.start and w.end <= e1 for w in touched), (s, e, s1, e1)
        assert not straddled(s1, MERGES) and not straddled(e1, MERGES)


def test_pad_stops_short_of_the_neighbouring_words():
    snap = WordSnapper(MERGES)
    # clip-08: "So" starts 1.62 s after "merges.": the full tail fits, the lead too
    assert snap.pad(3454.31, 3456.53) == (pytest.approx(3454.31 - LEAD_SECONDS), pytest.approx(3456.53 + TAIL_SECONDS))
    snap = WordSnapper(HOLD_ON)
    # clip-09: "Hold" starts 0.64 s after "time.": 0.3 s of tail fits; a 1 s tail is clamped to "Hold" - WORD_MARGIN
    assert snap.pad(3966.45, 3967.17)[1] == pytest.approx(3967.17 + TAIL_SECONDS)
    assert snap.pad(3966.45, 3967.17, tail=1.0)[1] == pytest.approx(3967.81 - WORD_MARGIN)
    # abutting words leave no room: the cut is the word edge, never inside the neighbour
    assert snap.pad(3967.81, 3967.99) == (pytest.approx(3967.81 - LEAD_SECONDS), 3967.99)
    assert snap.pad(3967.99, 3968.25) == (3967.99, 3968.25)
    # the source's end still clamps
    assert snap.pad(3968.59, 3968.79, duration=3968.8) == (pytest.approx(3968.59), 3968.8)
    assert snap.pad(0.0, 1.0) == (0.0, pytest.approx(1.3))  # no neighbours: plain pad


def test_tighten_shortens_every_silent_gap_over_max_silence_to_exactly_keep_pause():
    ws = words([(0, 1, "Alpha"), (1.7, 2.5, "beta"), (3.5, 4.5, "gamma."), (6.5, 7.0, "Delta"), (7.4, 8.0, "eps.")])
    rep = tighten(0.0, 8.3, ws, [(0.0, 8.3)])  # the audio says the whole moment is quiet between words
    # gaps: 0.7 (= max_silence, kept), 1.0 -> 0.35, 2.0 -> 0.35, 0.4 (kept)
    assert segs(rep) == [(0.0, 2.675), (3.325, 4.675), (6.325, 8.3)]
    for (_, cut_a), (cut_b, _) in zip(rep.segments, rep.segments[1:]):
        prev = max((w for w in ws if w.end <= cut_a + 1e-9), key=lambda w: w.end)
        nxt = min((w for w in ws if w.start >= cut_b - 1e-9), key=lambda w: w.start)
        assert (cut_a - prev.end) + (nxt.start - cut_b) == pytest.approx(KEEP_PAUSE)  # what the viewer hears
    assert rep.silence_seconds == pytest.approx((1.0 - KEEP_PAUSE) + (2.0 - KEEP_PAUSE)) and rep.fillers == 0
    assert segs(tighten(0.0, 8.3, ws, None)) == segs(rep)  # no silence data: the word gaps are trusted
    assert segs(tighten(0.0, 8.3, ws, None, keep_pause=0.5)) == [(0.0, 2.75), (3.25, 4.75), (6.25, 8.3)]
    assert segs(tighten(0.0, 8.3, ws, None, max_silence=1.5)) == [(0.0, 4.675), (6.325, 8.3)]
    assert tighten(0.0, 8.3, ws, None, pauses=False).segments == ((0.0, 8.3),)
    assert MAX_SILENCE == 0.7 and KEEP_PAUSE == 0.35


def test_a_pause_is_cut_only_where_the_audio_agrees_it_is_silent():
    """talk2 at 50:50: whisper's zero-length "Okay," sits in a 0.78 s gap between "transfer." and
    "so" that the audio shows is not silent at all; and at 33:40 a 0.74 s gap between "okay."
    and "Push" is full of sound. A gap cut on word timing alone removed that "Okay,"."""
    ws = words([(0, 1, "transfer."), (1.78, 2.2, "so"), (5.0, 6.0, "Yes,"), (6.4, 7.0, "it."), (9.5, 11.0, "Push it.")])
    # gap 1 (0.78 s): the audio reports no silence -> kept. gap 2 (2.8 s): silent only 2.6-4.9 (a laugh
    # first) -> that stretch alone is cut to KEEP_PAUSE; the laugh stays. gap 3: the detected silence
    # 6.9-9.6 spills 0.1 s into "it." and "Push": whisper's spans are estimates and the audio is not,
    # so those are the words' silent edges, the pause is the whole silence, and the join keeps
    # KEEP_PAUSE/2 of it on each side (cuts.py docstring, 3).
    rep = tighten(0.0, 11.3, ws, [(2.6, 4.9), (6.9, 9.6)])
    assert segs(rep) == [(0.0, 2.775), (4.725, 7.075), (9.425, 11.3)]
    # a silence shorter than max_silence inside a long gap is not worth a join; nothing else is cut
    assert tighten(0.0, 11.3, ws, [(2.6, 3.2)]).segments == ((0.0, 11.3),)
    # the audio checked and found nothing: no pause cut at all
    assert tighten(0.0, 11.3, ws, []).segments == ((0.0, 11.3),)
    # two silences in one gap separated by a cough: the cough is neither a word nor silence and is
    # too short to stand alone between two joins, so it goes with the pause (one cut)
    ws = words([(0, 1, "a."), (5.0, 12.0, "b c d e.")])
    assert segs(tighten(0.0, 12.3, ws, [(1.0, 2.2), (2.6, 5.0)])) == [(0.0, 1.175), (4.825, 12.3)]
    assert segs(tighten(0.0, 12.3, ws, None)) == [(0.0, 1.175), (4.825, 12.3)]
    # ...but a zero-length word whisper heard there (load_words keeps them) is speech: it stays
    ws = words([(0, 1, "a."), (2.4, 2.4, "Okay,"), (5.0, 12.0, "b c d e.")])
    assert segs(tighten(0.0, 12.3, ws, [(1.0, 2.2), (2.6, 5.0)])) == [(0.0, 1.175), (2.025, 2.775), (4.825, 12.3)]
    # without audio the bare gaps on both sides are cut and the word stands alone between two joins,
    # which is why the audio is consulted whenever it can be
    assert segs(tighten(0.0, 12.3, ws, None)) == [(0.0, 1.175), (2.225, 2.575), (4.825, 12.3)]


def test_a_filler_inside_a_long_pause_collapses_into_one_cut():
    ws = words([(0, 1, "So"), (3.0, 3.4, "um"), (5.0, 6.0, "the"), (6.0, 12.0, "plan is set.")])
    rep = tighten(0.0, 12.3, ws, None)
    # One join with breath on each side; the gaps around "um" go with it. The 1.15 s "So" fragment
    # in front is short, but cancelling the filler cut would not help: the pause cuts stay and would
    # leave "um" standing between two joins, so the filler cut is kept.
    assert segs(rep) == [(0.0, 1.15), (4.85, 12.3)]
    assert rep.fillers == 1 and rep.removed_seconds == pytest.approx(3.7)
    # the same moment, too short for the cap: 3.7 s of 7.3 s would go, so it is kept intact and says why
    rep = tighten(0.0, 7.3, ws[:3] + [Word(6.0, 7.0, "plan.")], None)
    assert rep.intact and "51%" in rep.note


def test_cut_moments_ends_clip_08_after_merges_and_clip_09_after_time():
    """The two cuts the verifier heard: a tail that ran into "So it" and one that ran into "Hold on"."""
    [cut], [rep] = cut_moments([Moment(3454.31, 3458.11, "t.", "t")], MERGES, None)
    assert (cut.start, cut.end) == (3454.31, 3456.53)  # speech edges: the dead air after "merges." is not speech
    assert cut.segments == ((pytest.approx(3454.16), pytest.approx(3456.83)),) and rep.removed_seconds == 0
    [cut], _ = cut_moments([Moment(3966.45, 3967.79, "t.", "t")], HOLD_ON, [[]])
    assert cut.end == 3967.17 and cut.segments == ((pytest.approx(3966.30), pytest.approx(3967.47)),)
    # a later moment that resolve_overlaps started at its neighbour's end (a word end, in a pause)
    # now starts on its own first word; the audio says the pause ends at 3458.1, 0.05 s before
    # whisper's "So", so the lead is measured from there (cuts.anchor_edges), not from the timestamp
    [cut], _ = cut_moments([Moment(3456.53, 3466.13, "t.", "t")], MERGES, [[(3456.6, 3458.1)]])
    assert cut.start == 3458.15 and cut.segments[0][0] == pytest.approx(3457.95)
    [cut], _ = cut_moments([Moment(3456.53, 3466.13, "t.", "t")], MERGES, [[]])  # no pause found: pad as before
    assert cut.segments[0][0] == pytest.approx(3458.0)
    # a moment whisper has no words for is padded the plain way
    [cut], _ = cut_moments([Moment(100.0, 110.0, "t.", "t")], MERGES, [None])
    assert (cut.start, cut.end) == (100.0, 110.0) and cut.segments == ((pytest.approx(99.85), pytest.approx(110.3)),)


# talk2 6:11 (clip-02 of the fourth reel): whisper's zero-length "So" is pinned to "creating"; the
# audio has "Okay." end at 370.79, a 0.44 s pause, then sound from 371.23 (the real "So").
CREATING = words([
    (369.93, 370.25, "communicate"), (370.25, 370.55, "via"), (370.43, 371.19, "Okay."), (371.91, 371.91, "So"),
    (371.91, 372.41, "creating"), (372.41, 372.61, "a"), (372.61, 372.75, "bead"), (372.75, 373.05, "means"),
    (373.05, 373.27, "what?"),
])
CREATING_SILENCES = [(370.794, 371.234), (372.019, 372.148), (373.307, 374.468)]


def test_neighbours_do_not_count_a_zero_length_word_at_the_moments_own_start():
    """The fourth reel opened clip-02 exactly on "So"/"creating" (371.91, -5.8/-5.0 dB) because the
    zero-length "So" ending at the moment's start was taken for the previous word and the lead air
    was clamped to nothing."""
    snap = WordSnapper(CREATING)
    prev, nxt = snap.neighbours(371.91, 373.27)
    assert prev is not None and prev.text == "Okay." and nxt is None
    assert snap.pad(371.91, 373.27)[0] == pytest.approx(371.91 - LEAD_SECONDS)
    # a real previous word that abuts still counts (and still clamps the lead)
    assert WordSnapper(HOLD_ON).neighbours(3967.99, 3968.25)[0].text == "Hold"


def test_anchor_edges_moves_a_start_onto_the_pause_before_the_first_word():
    """The start goes `lead` before the end of the pause nearest the first word: earlier than
    whisper's timestamp when whisper timed the word late (the zero-length "So" is 0.68 s late), so
    `EDGE_REACH` must be more than 0.5 s; later when whisper timed it early."""
    prev = CREATING[2]  # "Okay."
    s, e = anchor_edges(371.76, 373.57, 371.91, 373.27, CREATING_SILENCES, prev=prev, nxt=None)
    assert s == pytest.approx(371.234 - LEAD_SECONDS) and e == pytest.approx(373.307 + TAIL_SECONDS)
    assert EDGE_REACH > 0.68
    # 372.019-372.148 is the "t" closure inside "creating" (0.11 s after the word begins), not the pause
    # before it: with the real pause out of reach the start stays where pad put it rather than opening
    # the clip on "-ting"; the audit cannot see that (both sides of 372.019 are quiet), so this can
    s, _ = anchor_edges(371.76, 373.57, 371.91, 373.27, CREATING_SILENCES, prev=prev, reach=0.5)
    assert s == 371.76
    s, _ = anchor_edges(371.76, 373.57, 371.91, 373.27, [(371.90, 372.148)], prev=prev)  # begins at the word: its onset
    assert s == pytest.approx(372.148 - LEAD_SECONDS)
    # ...and the previous word must end before the pause does (whisper's slop allowed): a previous word
    # that lies past the pause means the sound after it is that word, not ours
    late_prev = Word(371.29, 371.69, "Okay.")
    s, _ = anchor_edges(371.76, 373.57, 371.91, 373.27, CREATING_SILENCES, prev=late_prev)
    assert s == 371.76
    # a pause shorter than `lead` is kept whole: the start is its beginning
    s, _ = anchor_edges(9.85, 12.3, 10.0, 12.0, [(9.9, 10.0)], prev=Word(9.0, 9.9, "before."))
    assert s == pytest.approx(9.9)
    # whisper early: the pause covers the timestamp and the start moves later, onto `lead` before the sound
    s, _ = anchor_edges(9.85, 12.3, 10.0, 12.0, [(9.85, 10.6)])
    assert s == pytest.approx(10.45)
    assert anchor_edges(9.85, 12.3, 10.0, 12.0, [])[0] == 9.85  # nothing found: unchanged


def test_anchor_edges_moves_an_end_onto_the_pause_after_the_last_word():
    """talk2 396.41: "thing." is timed 395.55-396.11 but the sound stops at 395.71; a 0.49 s pause
    follows and then untranscribed speech, which the 0.3 s tail ran into. The end goes `tail` into
    the pause nearest the last word's end."""
    ws = words([(395.31, 395.41, "do"), (395.41, 395.55, "its"), (395.55, 396.11, "thing."), (398.01, 398.13, "to")])
    sil = [(394.661, 395.135), (395.710, 396.198)]
    _, e = anchor_edges(395.16, 396.41, 395.31, 396.11, sil, prev=None, nxt=ws[-1])
    assert e == pytest.approx(395.71 + TAIL_SECONDS)
    # the pause must end no earlier than ONSET_SLOP before the word's end (else it is a closure inside
    # the word, and the word goes on after it), and the next word may not begin before the pause
    _, e = anchor_edges(395.16, 396.41, 395.31, 396.11, [(395.710, 395.95)], nxt=ws[-1])
    assert e == 396.41
    _, e = anchor_edges(395.16, 396.41, 395.31, 396.11, [(396.35, 396.7)], nxt=Word(396.12, 396.5, "Yeah."))
    assert e == 396.41
    # a next word whisper started inside the pause is whisper's slop, not sound: the pause wins
    _, e = anchor_edges(395.16, 396.41, 395.31, 396.11, sil, nxt=Word(395.9, 396.4, "Yeah."))
    assert e == pytest.approx(395.71 + TAIL_SECONDS)
    # a pause shorter than `tail` is kept whole, and the source's end still clamps
    _, e = anchor_edges(0.0, 12.3, 0.1, 12.0, [(12.05, 12.2)])
    assert e == pytest.approx(12.2)
    _, e = anchor_edges(0.0, 12.3, 0.1, 12.0, [(12.05, 12.6)], duration=12.25)
    assert e == 12.25


def test_cut_moments_anchors_the_clip_edges_and_reports_embedded_fillers():
    [cut], [rep] = cut_moments([Moment(371.91, 373.27, "t.", "t")], CREATING, [CREATING_SILENCES])
    assert (cut.start, cut.end) == (371.91, 373.27)  # the speech edges stay whisper's (moments.json round trip)
    assert cut.segments[0][0] == pytest.approx(371.234 - LEAD_SECONDS)
    assert cut.segments[-1][1] == pytest.approx(373.307 + TAIL_SECONDS)
    # "Um, you're going to have to stop your agent" (talk2 3947): the "Um," runs into "you're" with no
    # pause after it, so it stays; the clip opens `lead` before the pause that precedes it, not on
    # whisper's "Um," and not 0.15 s before "you're" (which was inside the "Um," at -3 dB)
    ws = words([(3945.0, 3945.4, "right?"), (3947.04, 3947.48, "Um,"), (3947.66, 3947.92, "you're"), (3947.92, 3948.04, "going"),
                (3948.04, 3949.48, "to have to stop your agent")])
    [cut], [rep] = cut_moments([Moment(3947.04, 3949.48, "t.", "t")], ws, [[(3945.51, 3947.171), (3949.6, 3950.4)]])
    assert cut.segments[0][0] == pytest.approx(3947.171 - LEAD_SECONDS)
    assert rep.fillers == 0 and rep.embedded == 1


def test_detection_spans_reach_past_the_padded_moment():
    ms = [Moment(10.0, 20.0, "t.", "t"), Moment(0.2, 5.0, "u.", "u")]
    spans = detection_spans(ms, duration=21.0)
    assert spans[0][0] < 10.0 - LEAD_SECONDS - EDGE_REACH and spans[0][1] == 21.0
    assert spans[1] == (0.0, pytest.approx(5.0 + TAIL_SECONDS + EDGE_REACH + max(LEAD_SECONDS, TAIL_SECONDS)))


def test_moments_from_specs_uses_word_edges_when_words_are_timed():
    cues = [Cue(3454.0, 3457.0, "review it and approve it before it merges."),
            Cue(3458.0, 3466.5, "So it must just be determining what the task is if it's which part of the stage it's at.")]
    snapper = snapper_for(cues, MERGES)
    assert isinstance(snapper, WordSnapper)
    [m] = moments_from_specs([{"start": 3455.6, "end": 3458.11, "title": "x"}], cues, snapper=snapper)
    assert (m.start, m.end) == (3454.31, 3456.53) and m.requested == (3455.6, 3458.11)
    assert moments_from_specs([m.spec()], cues, snapper=snapper)[0].end == m.end  # round trip
    # without word timings the cue-based path is untouched: outward to cue-estimated sentence edges
    assert isinstance(snapper_for(cues, []), Snapper)
    [m2] = moments_from_specs([{"start": 3455.6, "end": 3458.11}], cues)
    assert (m2.start, m2.end) == (3454.0, 3466.5)


def test_intro_card_runtime_is_the_whole_reel_including_the_outro():
    info = SourceInfo("assets/demo-clip.mp4", 72.0, True, True, 0)
    ms = [Moment(10.0, 30.0, "a.", "a", lesson="Lesson a"), Moment(40.0, 61.0, "b.", "b", lesson="Lesson b")]
    plan = build_reel_plan(info, ms, "out/x", title="t", date="d", takeaways=["one", "two"])
    reel = plan["output"]["reel"]
    total = (reel["intro"]["seconds"] + sum(c["seconds"] for c in reel["opening"])
             + sum(c["card"]["seconds"] for c in plan["clips"])
             + sum(s["end"] - s["start"] for c in plan["clips"] for s in c["segments"])
             + sum(c["seconds"] for c in reel["closing"]) + reel["outro"]["seconds"])
    assert total == pytest.approx(4 + 4.5 + 6 + (20.45 + 21.45) + 4.5 + 3)  # 63.9 s
    assert plan_runtime(plan) == pytest.approx(total)
    assert reel["intro"]["lines"][-1] == "2 moments · 1:04"  # rounded, not floored to 1:03


# --- the real thing --------------------------------------------------------------------------

DATA = Path("/home/orangepi/projects/video-editor-bot-data")
REAL = {
    "source": DATA / "talk2-recording.mp4",
    "srt": DATA / "talk2-words.srt",
    "words": DATA / "talk2-words.words.json",
    "moments": DATA / "talk2-moments-v3.json",
}
REAL_AUDIO = DATA / "talk2-16k.wav"  # 16-bit mono PCM of the recording; the audit decodes the mp4 itself without it
LONG_SILENCE = 0.9  # the independent verifier's threshold for a pause a viewer notices


def _tools_present() -> bool:
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def _covering(t: float, silences: list[tuple[float, float]]) -> tuple[float, float] | None:
    return next(((s, e) for s, e in silences if s - 1e-3 <= t <= e + 1e-3), None)


def acoustic_safety(plan: dict, ws: list[Word], source, *, lead: float = LEAD_SECONDS,
                    tail: float = TAIL_SECONDS) -> dict[str, list]:
    """What the verifier hears, measured on the audio, not on the word list:
    `long`: a silence of LONG_SILENCE or more inside a kept segment; `air`: more
    than `lead` / `tail` of nothing at a clip's ends, counted from where the
    sound stops or from whisper's word edge, whichever is nearer the cut;
    `chopped`: an edge inside a whisper word span whose pause is too short to be
    a shortened pause (<= MAX_SILENCE, so it came from an anchor) and does not
    reach the word's edge (a start whose pause begins deeper than cuts.ONSET_SLOP
    into the word, or an end whose pause ends earlier than that before the
    word's end): the cut sits in a stop closure and takes a syllable, which the
    audit cannot hear because both sides of such a cut are quiet. A pause cut
    inside a whisper span stretched over dead air is by design (cuts.py
    docstring, 3) and is not counted, nor is a filler: whisper's span for an
    "uh," begins up to 0.3 s before its sound, and cutting the sound alone is
    the point (talk2 963.4-964.0). The pauses are detected at
    the floor the cutter uses (cuts.MIN_PAUSE), so a click the cutter bridged is
    bridged here too. Whether a cut runs through sound is the audit's question
    (clipbot.audit, 40 ms of peak level on each side)."""
    out: dict[str, list] = {"long": [], "air": [], "chopped": []}
    kept_words = [w for w, f in zip(ws, filler_mask(ws)) if not f]
    segments = [(s["start"], s["end"]) for c in plan["clips"] for s in c["segments"]]
    around = detect_silences(source, [(a - 1.0, b + 1.0) for a, b in segments], min_seconds=MIN_PAUSE)
    inside = detect_silences(source, segments, min_seconds=LONG_SILENCE)
    k = 0
    for clip in plan["clips"]:
        n = len(clip["segments"])
        for j, seg in enumerate(clip["segments"]):
            a, b = seg["start"], seg["end"]
            sil = around[k]
            for edge, kind in ((a, "start"), (b, "end")):
                for w in kept_words:
                    if w.start + ONSET_SLOP < edge < w.end - ONSET_SLOP:
                        cover = _covering(edge, sil)
                        if cover is None or cover[1] - cover[0] > MAX_SILENCE:
                            continue  # voiced on both sides (the audit's case), or a shortened pause
                        # a closure: the pause begins inside the word AND the word goes on after it (a word
                        # whisper stretched into the pause ends inside it and loses nothing)
                        if kind == "start":
                            deep = cover[0] > w.start + ONSET_SLOP + 1e-3 and w.end > cover[1] + ONSET_SLOP
                        else:
                            deep = cover[1] < w.end - ONSET_SLOP - 1e-3 and w.start < cover[0] - ONSET_SLOP
                        if deep:
                            out["chopped"].append((clip["id"][:7], kind, edge, w.text, cover))
            out["long"].extend((clip["id"][:7], round(s, 2), round(e, 2), round(e - s, 2)) for s, e in inside[k])
            words_in = [w for w in ws if w.end > a and w.start < b]
            if words_in and j == 0:
                head = _covering(a, sil)
                onset = min(words_in[0].start, head[1] if head else words_in[0].start)
                if onset - a > lead + 1e-3:
                    out["air"].append((clip["id"][:7], "lead", round(onset - a, 3)))
            if words_in and j == n - 1:
                last = _covering(b, sil)
                offset = max(words_in[-1].end, last[0] if last else words_in[-1].end)
                if b - offset > tail + 1e-3:
                    out["air"].append((clip["id"][:7], "tail", round(b - offset, 3)))
            k += 1
    return out


def _accepted(bad, accepted: tuple[float, ...], slack: float = 0.1) -> None:
    """Every offending edge is one of the documented residuals (see the tests below)."""
    stray = [r.line() for r in bad if not any(abs(r.time - t) <= slack for t in accepted)]
    assert stray == [], stray


@pytest.mark.skipif(not all(p.exists() for p in REAL.values()) or not _tools_present(),
                    reason="needs the talk2 recording, its whisper words and ffmpeg")
def test_real_talk2_reel_never_cuts_sound_and_leaves_no_long_silence(tmp_path, capsys):
    """The third reel's verifier: 0 in-word cut ends but 10 acoustic silences >= 0.9 s inside kept
    audio, all of them whisper spans stretched over a pause or shifted into dead air. The fourth's:
    no long silence, but 11 of 86 edges with speech above -25 dB on both sides, nine of them filler
    cuts placed at whisper's edges for an "uh". Now every join sits in a detected pause, no long
    silence survives anywhere in the kept audio, and the audit finds speech on both sides of an
    edge only where the audio has no pause at all within reach of the word: with this word file,
    the start of clip-02, where "Okay." (371.29-371.69, the other speaker) runs straight into "So"
    (371.83) and the only pause is before the "Okay."."""
    out = tmp_path / "plan.json"
    rc = cli.main(["reel", "--source", str(REAL["source"]), "--srt", str(REAL["srt"]), "--words", str(REAL["words"]),
                   "--moments", str(REAL["moments"]), "--out", str(out)])
    assert rc == 0, capsys.readouterr()
    plan = json.loads(out.read_text(encoding="utf-8"))
    ws = load_words(REAL["words"])
    found = acoustic_safety(plan, ws, REAL["source"])
    assert found["air"] == []
    assert found["long"] == []
    assert found["chopped"] == []
    readings = audit_plan(plan, REAL_AUDIO if REAL_AUDIO.exists() else REAL["source"], words=ws)
    assert len(readings) == 2 * sum(len(c["segments"]) for c in plan["clips"])
    _accepted(offenders(readings), accepted=(371.74,))
    # and the pauses that did survive in the third reel are gone: "content," (655.4-656.6), "flipped"
    # (950.9-952.0), "for | us" (3950.1-3952.3), the tail of "while." (3979.2-3980.3), "a good" (4398.1-4399.7)
    segments = [(s["start"], s["end"]) for c in plan["clips"] for s in c["segments"]]
    for t in (655.9, 951.4, 3951.0, 3979.9, 4399.0):
        assert not any(a < t < b for a, b in segments), t


MERGED = {**REAL, "words": DATA / "talk2-words-merged.words.json", "framing": DATA / "talk2-framing-v2.json"}


@pytest.mark.skipif(not all(p.exists() for p in MERGED.values()) or not _tools_present(),
                    reason="needs the talk2 recording, its merged whisper words, framing and ffmpeg")
def test_real_talk2_fourth_reel_edges_sit_in_pauses(tmp_path, capsys):
    """The fourth reel's exact command (merged words, framing v2, music). Its verifier found 11 of
    86 edges voiced on both sides; the ones that remain are where the speaker ran two sentences
    together with no pause of MIN_PAUSE within EDGE_REACH of the word: "cases. What could go wrong?"
    at 929.73 (-16/-23 dB) and "this. And so" at 3440.82 (-22/-24 dB). Everything else, including
    every filler cut, sits in a pause; fillers with no pause beside them are kept and counted."""
    out = tmp_path / "plan.json"
    rc = cli.main(["reel", "--source", str(MERGED["source"]), "--srt", str(MERGED["srt"]), "--words", str(MERGED["words"]),
                   "--moments", str(MERGED["moments"]), "--framing", str(MERGED["framing"]),
                   "--music", str(REPO_ROOT / "assets" / "music" / "bed.mp3"), "--out", str(out)])
    assert rc == 0, capsys.readouterr()
    text = capsys.readouterr().out
    assert "fillers kept: no pause beside them" in text
    plan = json.loads(out.read_text(encoding="utf-8"))
    ws = load_words(MERGED["words"])
    found = acoustic_safety(plan, ws, MERGED["source"])
    assert found["air"] == [] and found["long"] == [] and found["chopped"] == []
    readings = audit_plan(plan, REAL_AUDIO if REAL_AUDIO.exists() else MERGED["source"], words=ws)
    bad = offenders(readings)
    _accepted(bad, accepted=(929.73, 3440.82))
    assert all(r.kind == "start" and r.segment == 1 for r in bad)  # never a join, never a clip end
    # the joins the verifier heard inside "Uh," (374.54, 2009.43, 3960.49) are gone: those fillers are
    # embedded and stay, and the pause before each is what gets shortened
    for t in (374.54, 2009.43, 3960.49, 3947.51, 4406.99, 2026.19, 3970.47, 371.91, 396.41):
        assert not any(abs(r.time - t) < 0.02 for r in readings), t
