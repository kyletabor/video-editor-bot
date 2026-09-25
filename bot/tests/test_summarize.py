from clipbot.captions import Cue
from clipbot.summarize import executive_summary, sentences, to_markdown
from tests.test_select import DEMO


def test_sentences_join_across_cues_and_keep_start_time():
    cues = [Cue(0, 4, "Okay, this is a", "K"), Cue(4, 8, "recording. Let's see how this works.", "K")]
    s = sentences(cues)
    assert s[0].text == "Okay, this is a recording."
    assert s[0].start == 0
    assert s[1].text == "Let's see how this works."
    assert s[1].start == 4


def test_speaker_change_flushes_unfinished_sentence():
    """Regression (PR #10 review): Ramsey's words must not be credited to Kyle."""
    cues = [Cue(0, 4, "The next step is", "Kyle"), Cue(4, 8, "I disagree, we should test first.", "Ramsey")]
    s = sentences(cues)
    assert [(x.speaker, x.text) for x in s] == [
        ("Kyle", "The next step is."),
        ("Ramsey", "I disagree, we should test first."),
    ]


def test_executive_summary_picks_substantive_sentences():
    picked = executive_summary(DEMO, n=3)
    assert 1 <= len(picked) <= 3
    joined = " ".join(p.text for p in picked).lower()
    assert "clip bot" in joined or "agents" in joined or "define" in joined
    # ordered by time
    assert [p.start for p in picked] == sorted(p.start for p in picked)


def test_markdown_has_all_sections_and_clips():
    plan = {"clips": [{"id": "clip-01-x", "takeaway": "The doubt.", "segments": [{"start": 36, "end": 52}]}]}
    md = to_markdown("Demo", "assets/demo-clip.mp4", 72, DEMO, plan=plan)
    assert md.startswith("# Demo")
    for h in ("## Executive summary", "## Clips", "## Full transcript"):
        assert h in md
    assert "clip-01-x" in md and "[0:36–0:52]" in md
    assert "[1:04]" in md  # transcript timestamps
