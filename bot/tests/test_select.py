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


def test_keywords_strips_stopwords_and_stems():
    kws = keywords("the part where I question whether Ramsey's clip bot idea will work")
    assert "question" in kws and "clip" in kws and "bot" in kws and "work" in kws
    assert "the" not in kws and "where" not in kws


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


def test_no_match_returns_nothing():
    assert best_windows(DEMO, "kubernetes networking", 15, 45) == []


def test_generic_request_still_picks_something():
    ws = best_windows(DEMO, "make a clip", 15, 45)
    assert len(ws) == 1
