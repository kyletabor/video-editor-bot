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
from clipbot.captions import Cue
from clipbot.cuts import (
    BREATH_SECONDS,
    KEEP_PAUSE,
    LEAD_SECONDS,
    MAX_SILENCE,
    TAIL_SECONDS,
    WORD_MARGIN,
    Snapper,
    WordSnapper,
    tighten,
)
from clipbot.probe import SourceInfo
from clipbot.reel import Moment, build_reel_plan, cut_moments, moments_from_specs, plan_runtime, snapper_for
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
    # now starts on its own first word, with the lead clamped off the neighbour's last word
    [cut], _ = cut_moments([Moment(3456.53, 3466.13, "t.", "t")], MERGES, [[(3456.6, 3458.1)]])
    assert cut.start == 3458.15 and cut.segments[0][0] == pytest.approx(3458.0)
    # a moment whisper has no words for is padded the plain way
    [cut], _ = cut_moments([Moment(100.0, 110.0, "t.", "t")], MERGES, [None])
    assert (cut.start, cut.end) == (100.0, 110.0) and cut.segments == ((pytest.approx(99.85), pytest.approx(110.3)),)


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
LONG_SILENCE = 0.9  # the independent verifier's threshold for a pause a viewer notices
CUT_MARGIN = 0.1  # a cut inside a whisper span must be at least this far from sound


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
    `voiced_cuts`: a segment edge inside a whisper word span that is not inside a
    detected silence with CUT_MARGIN of silence to spare (whisper's span is an
    estimate; the audio is the ground truth, cuts.py docstring 3); `long`: a
    silence of LONG_SILENCE or more inside a kept segment; `air`: more than
    `lead` / `tail` of nothing at a clip's ends, counted from where the sound
    stops or from whisper's word edge, whichever is nearer the cut."""
    out: dict[str, list] = {"voiced_cuts": [], "long": [], "air": []}
    segments = [(s["start"], s["end"]) for c in plan["clips"] for s in c["segments"]]
    around = detect_silences(source, [(a - 1.0, b + 1.0) for a, b in segments], min_seconds=0.2)
    inside = detect_silences(source, segments, min_seconds=LONG_SILENCE)
    k = 0
    for clip in plan["clips"]:
        n = len(clip["segments"])
        for j, seg in enumerate(clip["segments"]):
            a, b = seg["start"], seg["end"]
            sil = around[k]
            for edge in (a, b):
                if any(w.start + 1e-3 < edge < w.end - 1e-3 for w in ws):
                    cover = _covering(edge, sil)
                    if cover is None or edge - cover[0] < CUT_MARGIN - 1e-3 or cover[1] - edge < CUT_MARGIN - 1e-3:
                        out["voiced_cuts"].append((clip["id"][:7], edge, cover))
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


@pytest.mark.skipif(not all(p.exists() for p in REAL.values()) or not _tools_present(),
                    reason="needs the talk2 recording, its whisper words and ffmpeg")
def test_real_talk2_reel_never_cuts_sound_and_leaves_no_long_silence(tmp_path, capsys):
    """The third reel's verifier: 0 in-word cut ends but 10 acoustic silences >= 0.9 s inside kept
    audio, all of them whisper spans stretched over a pause or shifted into dead air. Now a cut may
    sit inside a whisper span, but only inside detected silence with margin, and no long silence
    survives anywhere in the kept audio: not inside a word span, not at a segment tail, not across a
    join."""
    out = tmp_path / "plan.json"
    rc = cli.main(["reel", "--source", str(REAL["source"]), "--srt", str(REAL["srt"]), "--words", str(REAL["words"]),
                   "--moments", str(REAL["moments"]), "--out", str(out)])
    assert rc == 0, capsys.readouterr()
    plan = json.loads(out.read_text(encoding="utf-8"))
    ws = load_words(REAL["words"])
    found = acoustic_safety(plan, ws, REAL["source"])
    assert found["voiced_cuts"] == []
    assert found["air"] == []
    assert found["long"] == []
    # and the pauses that did survive in the third reel are gone: "content," (655.4-656.6), "flipped"
    # (950.9-952.0), "for | us" (3950.1-3952.3), the tail of "while." (3979.2-3980.3), "a good" (4398.1-4399.7)
    segments = [(s["start"], s["end"]) for c in plan["clips"] for s in c["segments"]]
    for t in (655.9, 951.4, 3951.0, 3979.9, 4399.0):
        assert not any(a < t < b for a, b in segments), t
