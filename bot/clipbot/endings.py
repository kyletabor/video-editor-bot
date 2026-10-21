"""Does a clip stop on a setup line, before the speaker's point?

Kyle's review of the Talk #4 reel (2026-10-19): it was "really good except" a couple of
clips that "just got cut off" where it "sounded like was gonna be a great sound bite",
the speaker saying something like "and the cool thing is..." and the reel cutting to the
next card. Every edge was on a word boundary and a sentence boundary, so the earlier
rules (cuts.py) were satisfied: a 0.45 s pause, or a period in Meet's captions, closes a
sentence, and "And the cool thing is." is a sentence by that measure. It is not a
thought. The payoff is the next sentence.

`dangling(text)` reads the last sentence of a clip and names the setup it ends on, or
returns None for a complete point. It is a text rule, so it works the same on Meet
captions, an SRT and whisper words. reel.finish_thought uses it to carry a clip's end
on through the payoff; the rules are deliberately narrow (a lead-in phrase, a colon or
an ellipsis, or a sentence that ends on a word that cannot end one) so a complete
sentence is never stretched.
"""

from __future__ import annotations

import re

# "the cool thing is", "and the best part was", "one big reason is", "the trick here is"
_NOUNS = (
    "thing|part|idea|trick|point|reason|key|secret|question|problem|catch|kicker|"
    "takeaway|difference|lesson|goal|plan|result|answer|bit|news|beauty|magic|insight|benefit|"
    "advantage|upshot|bottom line|punchline|hack|cool part|nice part"
)
# Leading connectives a setup sentence may open with: "And so the cool thing is"
# Meet captions keep the fillers ("Um, the cool thing is."), and a setup can follow a clause
# ("That is a good question, the cool thing is"), so the anchor is the start or a comma.
_OPENER = (r"(?:^|,\s*)(?:(?:and|so|but|now|okay|ok|well|plus|because|then|like|uh|um|you know|i mean|"
           r"i think|honestly|actually|basically)[,]?\s+)*")
_LEAD_INS = [
    # Anchored to the sentence start: "I don't know what the real reason was." is a finished point.
    re.compile(_OPENER + rf"(?:the|a|my|our|one|another|this|that|other)\s+(?:[\w'-]+\s+){{0,3}}(?:{_NOUNS})\s+"
               r"(?:(?:here|there|about (?:it|this|that))\s+)?(?:is|was|being|are|were)$"),
    # Talk #4 clip 3: "...and this is the cool part about uh clawed Jesus." (Meet's captions for
    # "Claude Design"); the payoff, what Claude Design does, was the next 24 s
    # "this is" points forward; "and that's the cool thing" points back at a finished point.
    re.compile(rf"\b(?:this|here)(?:'s| is)\s+(?:the|my|a|one|another)\s+(?:[\w'-]+\s+){{0,2}}(?:{_NOUNS})"
               # "about <a name>" only: "the best part of the day" and "the thing about it" are finished
               r"(?:\s+about(?:\s+(?:uh|um))?(?!\s+(?:it|this|that|them|him|her|you)$)(?:\s+[\w'-]+){1,3})?$"),
    # "what I love about it is", "what's cool is", "the way it works is", "how it works is"
    # Anchored too: "I know where the file is." is not a setup.
    re.compile(_OPENER + r"(?:what(?:'s| is| was)?|the way|how)\b(?:\s+[\w'-]+){0,5}\s+(?:is|was)$"),
    # "here's the thing", "here's what", "here is how"
    re.compile(r"\bhere(?:'s| is)(?:\s+(?:the\s+[\w'-]+|what|how|why|where|who|one|another|a))?$"),
    re.compile(r"\b(?:let me show you|i'll show you|i can show you|watch this|check this out|look at this|"
               r"for example|for instance|such as|you know what|guess what|get this|and then|"
               r"so what (?:i|we|it|you) (?:did|do|does) (?:is|was)|it turns out|turns out|"
               r"the answer is|it's basically|basically it's)$"),
    # Asides that hold the floor before the point (Talk #4 clip 3, after the setup: "I don't know
    # how else to show it without just starting a new one."); the payoff is still to come.
    # "hold on" / "one sec" / "where was i" only as the whole sentence: "Hold on to your hats" is not one.
    re.compile(r"^(?:(?:um|uh|okay|ok|so|and|oh)[,]?\s+)*(?:hold on|one sec(?:ond)?|where was i)$"),
    re.compile(_OPENER + r"(?:i don't know how else to (?:show|explain|do)|"
               r"let me (?:just )?(?:find|pull|open|scroll|share))\b"),
]
# A sentence cannot end on these, whatever the punctuation says ("...and it gives you the.")
# (Not "for", "to", "of", "with": "That's what it's for." is ordinary English.)
_OPEN_WORDS = {"and", "but", "or", "because", "the", "a", "an", "if", "gonna", "whether", "unless", "plus", "than"}
_RHETORICAL = re.compile(r"\b(?:you know what|guess what|and then what|know why)$")
_OPEN_PUNCT = (":", "...", "…", "—", "–", "-", ",")
# "that's what it is", "that's just how it was": a pronoun alone before is/was closes the thought
_COMPLETE = re.compile(r"^(?:what|which|how|why|where|the way)\s+(?:it|that|this|he|she|they|you|we|i|things|life)"
                       r"(?:\s+(?:just|always|really))?\s+(?:is|was)$")
_TRIM = re.compile(r"[\s\"'“”‘’)\]]+$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def last_sentence(text: str) -> str:
    """The final sentence of `text` (by . ! ?), whitespace-normalised."""
    text = " ".join(text.split())
    parts = [p for p in _SENTENCE.split(text) if p.strip()]
    return parts[-1] if parts else ""


def dangling(text: str) -> str | None:
    """The setup the last sentence of `text` ends on, or None when it ends on a point.

    >>> dangling("It writes the plan. And the cool thing is...")
    'the cool thing is'
    >>> dangling("It writes the plan for you.") is None
    True
    """
    sentence = _TRIM.sub("", last_sentence(text))
    if not sentence:
        return None
    if sentence.endswith(_OPEN_PUNCT):
        tail = sentence.rstrip(":.…—–-, ")
        return (" ".join(tail.split()[-4:]) + sentence[len(tail):].strip()) or sentence
    bare = re.sub(r"[.!?]+$", "", sentence).strip().lower()
    bare = bare.replace("’", "'")
    if sentence.endswith("?"):  # a question is a finished turn, unless the speaker asks it to answer it
        m = _RHETORICAL.search(bare)
        return m.group(0) if m else None
    for rx in _LEAD_INS:
        m = rx.search(bare)
        if m and not _COMPLETE.search(m.group(0)):
            return m.group(0)
    words = bare.split()
    if words and words[-1] in _OPEN_WORDS:
        return " ".join(words[-3:])
    return None
