"""Clips end on the point, not on the setup (endings.dangling, reel.finish_thought).

Kyle on the Talk #4 reel (2026-10-19): a couple of clips "just got cut off" where it
"sounded like was gonna be a great sound bite", the speaker saying "and the cool thing
is..." and the reel moving on. Those ends were clean sentence edges, so only a rule
about what the sentence says can catch them.
"""

import pytest

from clipbot.captions import Cue
from clipbot.cuts import Snapper, WordSnapper
from clipbot.endings import dangling, last_sentence
from clipbot.reel import MAX_FINISH_SECONDS, finish_thought, moments_from_specs, spans_for
from clipbot.words import Word


def words(seq, speaker=None):
    return [Word(float(a), float(b), t, speaker) for a, b, t in seq]


@pytest.mark.parametrize("text", [
    # Talk #4, Ramsey's reel (2026-10-17), clip ends as transcribed from the reel
    "The only difference was, and this is the cool part.",  # clip 3: the Claude Design payoff came next
    "So it's going to start—",  # clip 4: cut mid-sentence
    "This is the way it coined—",  # clip 5
    "It writes the plan. And the cool thing is...",
    "And the cool thing is.",
    "and the best part about it was",
    "The trick here is",
    "What I love about it is",
    "what's really cool is",
    "Here's the thing.",
    "So here's what",
    "So what we did was",
    "Let me show you.",
    "For example,",
    "It turns out",
    "You get the.",
    "We hooked it up to Slack and",
    "The idea:",
    "It's not perfect but…",
])
def test_setups_dangle(text):
    assert dangling(text)


@pytest.mark.parametrize("text", [
    "The cool thing is that it runs on your own machine.",
    "That's what it is.",
    "That's just how it was.",
    "The reason is simple.",
    "I think so.",
    "I built the whole site with Claude in an afternoon.",
    "Here's the thing. It never forgets a step.",
    "And that's the cool thing.",
    # Talk #4 clips 1 and 2 ended on finished points
    "See if it can house the videos as well as kind of have some sort of framework.",
    "It's like building a template of brand.",
    "",
])
def test_complete_points_do_not(text):
    assert dangling(text) is None


def test_last_sentence():
    assert last_sentence("One. Two!  And the  cool thing is...") == "And the cool thing is..."
    assert last_sentence("  ") == ""


# A clip that the snapper ends on "is." (a period in Meet's captions, or a 0.45 s pause).
COOL = words([
    (10.0, 10.4, "It"), (10.4, 10.8, "builds"), (10.8, 11.2, "the"), (11.2, 11.8, "site."),
    (12.2, 12.4, "And"), (12.4, 12.6, "the"), (12.6, 12.9, "cool"), (12.9, 13.2, "thing"), (13.2, 13.5, "is"),
    (14.1, 14.4, "it"), (14.4, 14.8, "hosts"), (14.8, 15.0, "it"), (15.0, 15.6, "too."),
    (16.4, 16.8, "Next"), (16.8, 17.2, "question."),
])


def test_word_end_on_a_setup_carries_on_to_the_payoff():
    snap = WordSnapper(COOL)
    assert snap.end(13.4) == 13.5  # the 0.6 s pause after "is" makes it a sentence end
    assert snap.sentence_before(13.5) == "And the cool thing is"
    assert finish_thought(snap, 13.5) == 15.6  # through "it hosts it too."
    assert finish_thought(snap, 15.6) == 15.6  # a finished point is a fixed point
    assert finish_thought(snap, 11.8) == 11.8  # "It builds the site." is a point already


def test_a_payoff_by_someone_else_or_out_of_reach_leaves_the_end_alone():
    other = words([(w.start, w.end, w.text) for w in COOL[:9]], "Ramsey") + \
        words([(w.start, w.end, w.text) for w in COOL[9:]], "Kyle")
    assert finish_thought(WordSnapper(other), 13.5) == 13.5  # Kyle answers: not Ramsey's payoff
    far = words([(w.start, w.end, w.text) for w in COOL[:9]]) + words([(40.0, 40.5, "it"), (40.5, 41.0, "hosts.")])
    assert 40.0 - 13.5 > MAX_FINISH_SECONDS
    assert finish_thought(WordSnapper(far), 13.5) == 13.5
    # two setups in a row and nothing finished within two sentences: stays put (a fixed point)
    chain = words([(0, 0.5, "Here's"), (0.5, 1.0, "the"), (1.0, 1.5, "thing."), (2.0, 2.5, "The"), (2.5, 3.0, "trick"),
                   (3.0, 3.5, "is."), (4.0, 4.5, "Let"), (4.5, 4.8, "me"), (4.8, 5.0, "show"), (5.0, 5.5, "you."),
                   (6.0, 6.5, "Done.")])
    assert finish_thought(WordSnapper(chain), 1.5) == 1.5


def test_cue_captions_carry_on_too():
    # Meet captions, which is what the Talk #4 reel was cut from
    cues = [Cue(0, 4, "So we pointed it at the recording.", "Ramsey"), Cue(4, 8, "And the cool thing is.", "Ramsey"),
            Cue(8, 12, "it picked the moments by itself.", "Ramsey"), Cue(12, 16, "Okay, thanks.", "Kyle")]
    snap = Snapper(spans_for(cues))
    end = snap.end(7.5)
    assert snap.sentence_before(end).endswith("cool thing is.")
    assert finish_thought(snap, end) > 8
    assert dangling(snap.sentence_before(finish_thought(snap, end))) is None
    # a hand-picked moment that stops on the setup ends after the payoff, and re-snaps to itself
    [m] = moments_from_specs([{"start": 0, "end": 7.5, "title": "Cool"}], cues)
    assert m.end == finish_thought(snap, end)
    [again] = moments_from_specs([m.spec()], cues)
    assert again.end == m.end
