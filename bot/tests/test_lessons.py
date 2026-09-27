from clipbot.lessons import (
    LINE_LIMIT,
    card_seconds,
    closing_cards,
    opening_card,
    outro_card,
    teachable,
    top_topics,
)


def test_teachable_scores_explanations_up_and_banter_down():
    assert teachable("We should never cut inside a word because the edit point lands mid-syllable.") > 1.5
    assert teachable("Can you see my screen? Hold on, one sec.") < 0
    assert teachable("and then we kind of went on") == 0
    assert teachable("It runs at 5.7x realtime, so 79 minutes take 14 minutes.") > 0
    assert teachable("The rule: the renderer never reads the transcript.") >= 1


def test_card_seconds_grow_with_lines_within_the_contract():
    assert card_seconds(["a"]) == 3.0 and card_seconds(["a", "b"]) == 4.5 and card_seconds(["a"] * 4) == 7.5
    assert card_seconds(["a"] * 10) == 8.0


def test_top_topics_needs_a_word_in_two_lines():
    assert top_topics(["Beads breaks under load", "Beads is built for agents", "Agents talk through the repo"]) == ["beads", "agents"]
    assert top_topics(["one a", "two b"]) == []


def test_opening_card_lists_lessons_or_summarises_many():
    assert opening_card([]) is None and opening_card(["", "  "]) is None
    card = opening_card(["A", "B"])
    assert card["title"] == "What you'll learn" and card["lines"] == ["A", "B"] and card["seconds"] == 4.5
    many = ["Beads breaks under two writers", "Beads is built for agents", "Agents talk through the repo",
            "Agents need a gate", "The verdict", "Sixth"]
    card = opening_card(many, title_hint="Talk #2")
    assert len(card["lines"]) == 4
    assert card["lines"][0].startswith("6 moments on ") and "agents" in card["lines"][0] and "beads" in card["lines"][0]
    assert card["lines"][1:] == many[:3]
    card = opening_card(["one a", "two b", "three c", "four d", "five e"], title_hint="Talk")
    assert card["lines"][0] == "5 moments from Talk"
    assert opening_card(["x" * 200])["lines"][0].endswith("…") and len(opening_card(["x" * 200])["lines"][0]) <= LINE_LIMIT


def test_closing_cards_split_four_lines_per_card_and_cap_at_two():
    assert closing_cards([]) == [] and closing_cards(["", " "]) == []
    cards = closing_cards([f"Takeaway {i}" for i in range(10)])
    assert len(cards) == 2 and [c["title"] for c in cards] == ["Takeaways", "Takeaways (continued)"]
    assert cards[0]["lines"] == [f"Takeaway {i}" for i in range(4)] and len(cards[1]["lines"]) == 4
    assert cards[0]["seconds"] == 7.5
    long = closing_cards(["x" * 300])
    assert len(long) == 1 and len(long[0]["lines"][0]) <= LINE_LIMIT and long[0]["lines"][0].endswith("…")


def test_outro_card_points_at_the_summary():
    card = outro_card()
    assert card["title"] == "That's the session" and "summary.md" in card["lines"][0] and 1 <= card["seconds"] <= 10
    assert "notes.md" in outro_card("notes.md")["lines"][0]
