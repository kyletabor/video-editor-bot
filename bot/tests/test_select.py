from clipbot.captions import Cue
from clipbot.select import best_windows, keywords, score_cue

# 18 four-second cues, like a Meet recording
DEMO = [
    Cue(0, 4, "Okay, this is a"),
    Cue(4, 8, "recording. Let's see how this works. Here is my"),
    Cue(8, 12, "lovely uh let's see if this works. No. No. Oh. Oh, here's my"),
    Cue(12, 16, "lovely sailboat. Um I'm just trying to see what"),
    Cue(16, 20, "format that this this uh video comes out in. And then"),
    Cue(20, 24, "can we use this for our video bot? Can we turn this into"),
    Cue(24, 28, "like we had like a dubstep beat? Look at my hair. My hair's all messed"),
    Cue(28, 32, "up. I should shower more often. Uh, but you know, that's enough"),
    Cue(32, 36, "honesty. I don't think that needs to get out in public. Uh, what"),
    Cue(36, 40, "else? Uh, yeah, I'm really questioning whether Ramsay's idea"),
    Cue(40, 44, "for a video clip bot is going to work. This might be a"),
    Cue(44, 48, "giant cluster f*** cuz like I don't know how we're going to get our"),
    Cue(48, 52, "agents to work together. I also"),
    Cue(52, 56, "don't even know how to define what is"),
    Cue(56, 60, "good for an output for a video clip. So,"),
    Cue(60, 64, "that will be interesting. Um, yeah. Well,"),
    Cue(64, 68, "with that, let's uh let's stop recording and see what format this comes out"),
    Cue(68, 72, "in."),
]


def test_keywords_strips_stopwords_command_words_and_stems():
    kws = keywords("the part where I question whether Ramsey's clip bot idea will work")
    assert "question" in kws and "bot" in kws and "work" in kws
    assert "the" not in kws and "where" not in kws
    assert "clip" not in kws and "part" not in kws  # task words, not topic words


def test_command_words_are_not_topic_evidence():
    """Regression (PR #10 review): 'make a clip about kubernetes networking' must not match."""
    assert best_windows(DEMO, "make a clip about kubernetes networking", 15, 45) == []
    assert best_windows(DEMO, "cut a 30 second highlight about kubernetes", 15, 45) == []


def test_overlapping_cues_never_produce_overlapping_clips():
    """Regression (PR #10 review): simultaneous SRT cues share time, not just indexes."""
    cues = [Cue(0, 20, "alpha is the topic"), Cue(10, 30, "alpha is the other topic")]
    ws = best_windows(cues, "alpha", 15, 20, max_clips=2)
    assert len(ws) == 1
    for a in ws:
        for b in ws:
            if a is not b:
                assert a.end <= b.start or b.end <= a.start


def test_score_prefers_keyword_hits_and_penalizes_filler():
    kws = keywords("clip bot")
    assert score_cue(DEMO[10], kws) > score_cue(DEMO[0], kws)
    filler = Cue(0, 4, "uh um uh um like you know")
    assert score_cue(filler, kws) < 0


def test_demo_request_finds_the_questioning_bit():
    w = best_windows(DEMO, "the part where I question whether Ramsey's clip bot idea will work", 15, 45)
    assert len(w) == 1
    assert 15 <= w[0].duration <= 45
    assert w[0].start <= 36 <= w[0].end and w[0].start <= 48 <= w[0].end  # covers 36-48
    assert "clip bot" in w[0].takeaway.lower() or "questioning" in w[0].takeaway.lower()


def test_windows_respect_bounds_and_do_not_overlap():
    ws = best_windows(DEMO, "hair shower honesty video bot agents", 15, 30, max_clips=2)
    assert 1 <= len(ws) <= 2
    for w in ws:
        assert 15 <= w.duration <= 30
    if len(ws) == 2:
        assert ws[0].end <= ws[1].start


def test_short_matching_core_is_padded_to_min_length():
    """'hair'/'shower' only span 24-32s; the clip must still reach the 15s minimum."""
    ws = best_windows(DEMO, "my hair and showering", 15, 30)
    assert len(ws) == 1
    assert ws[0].start <= 24 and ws[0].end >= 32
    assert 15 <= ws[0].duration <= 30


def test_tight_core_is_not_padded():
    """When the matched core already meets the minimum, no unrelated cues are added."""
    ws = best_windows(DEMO, "the part where I question whether Ramsey's clip bot idea will work", 15, 45)
    assert (ws[0].start, ws[0].end) == (36, 52)


def test_no_match_returns_nothing():
    assert best_windows(DEMO, "kubernetes networking", 15, 45) == []


def test_generic_request_still_picks_something():
    ws = best_windows(DEMO, "make a clip", 15, 45)
    assert len(ws) == 1


def test_one_sentence_cleans_meet_artifacts():
    """Regression (lead's note): Meet multi-speaker separators and filler removal left
    'real - quick' and 'quick ,' in card titles."""
    from clipbot.select import _one_sentence, clean_text

    assert _one_sentence("So, um, real - quick , we should ship it.") == "So, real quick, we should ship it."
    assert clean_text("state-of-the-art tooling - and then") == "state-of-the-art tooling and then"
    assert clean_text("Um, uh, okay") == "okay"


def test_clean_text_keeps_dotted_tokens_and_collapses_filler_commas():
    from clipbot.select import clean_text
    assert clean_text("System agnostic: a .exe is not cool") == "System agnostic: a .exe is not cool"
    assert clean_text("So, um, real quick , we started") == "So, real quick, we started"
    assert clean_text("keep the .env file") == "keep the .env file"
