"""Storyboard page (storyboard.py): scenes in playback order, dialogue, audio, the page."""

import json

from clipbot import storyboard as sb
from clipbot.captions import Cue
from clipbot.words import Word

PLAN = {
    "version": "1",
    "source": {"path": "assets/demo-clip.mp4"},
    "output": {"dir": "out/x", "reel": {
        "intro": {"title": "Demo", "lines": ["Recorded today"], "seconds": 4},
        "opening": [{"title": "What you'll learn", "lines": ["One", "Two"], "seconds": 6}],
        "chapter_cards": "all",
        "closing": [{"title": "Takeaways", "lines": ["Keep it short"], "seconds": 5}],
        "outro": {"title": "Bye", "seconds": 3},
        "transition": {"kind": "dip", "seconds": 0.4},
        "music": {"path": "m.wav", "under": "cards"},
    }},
    "clips": [
        {"id": "a", "takeaway": "First idea.", "segments": [{"start": 10, "end": 14}, {"start": 20, "end": 22}],
         "cards": [{"title": "Get it", "qr": "https://example.com/repo", "seconds": 5}],
         "overlays": [{"start": 11, "end": 13, "text": "A label", "position": "top"}],
         "layout": {"kind": "pip", "screen": [0, 0, 100, 100], "speaker": [100, 0, 50, 50]}},
        {"id": "b", "takeaway": "Second idea.", "segments": [{"start": 30, "end": 35}]},
    ],
}


def test_scenes_follow_the_renderer_timeline_with_times_and_roles():
    scenes = sb.scenes_of(PLAN)
    assert [(s.kind, s.role, s.title) for s in scenes] == [
        ("card", "intro", "Demo"), ("card", "opening", "What you'll learn"), ("card", "section", "Get it"),
        ("card", "chapter", "First idea."), ("clip", "clip", "First idea."), ("card", "chapter", "Second idea."),
        ("clip", "clip", "Second idea."), ("card", "closing", "Takeaways"), ("card", "outro", "Bye")]
    assert [s.number for s in scenes] == list(range(1, 10))
    clip = scenes[4]
    assert clip.seconds == 6 and clip.start == 4 + 6 + 5 + 3 and clip.layout.startswith("screen")
    assert scenes[2].qr == "https://example.com/repo" and scenes[-1].transition == "end"


def test_music_is_described_per_scene_so_a_sting_on_every_slide_shows():
    scenes = sb.scenes_of(PLAN)
    music = [s.music for s in scenes]
    assert music[0].startswith("music IN") and music[1] == "music continues"
    assert music[2] == music[3] == "music continues"  # the first run: intro to the first clip, one cue
    assert music[5].startswith("music STING: starts and stops")
    assert music[7].startswith("music IN: theme returns") and music[8].endswith("plays its ending")
    assert music[4] == "speech only, no music" and not scenes[4].music_on
    quiet = json.loads(json.dumps(PLAN))
    del quiet["output"]["reel"]["music"]
    assert {s.music for s in sb.scenes_of(quiet)} == {"no music", "speech only"}


def test_dialogue_and_speakers_come_from_the_kept_spans_only():
    scenes = sb.scenes_of(PLAN)
    cues = [Cue(9, 15, "kept words", "Jeff Weiner"), Cue(15, 20, "cut words", "Kyle Tabor"),
            Cue(20, 22, "more kept", "Ramsey Jamoul"), Cue(30, 35, "second clip", None)]
    words = [Word(11, 11.5, "hello"), Word(16, 17, "dropped"), Word(21, 21.5, "there")]
    sb.attach_dialogue(scenes, cues, words)
    first, second = scenes[4], scenes[6]
    assert first.quote == "hello there" and first.speakers == ["Jeff Weiner", "Ramsey Jamoul"]
    sb.attach_dialogue(scenes, cues)  # no word timings: the captions that overlap
    assert scenes[6].quote == "second clip" and scenes[6].speakers == []


def test_long_quotes_keep_the_opening_and_the_end():
    scenes = sb.scenes_of(PLAN)
    words = [Word(10 + i * 0.03, 10 + i * 0.03 + 0.02, f"w{i}") for i in range(130)]
    sb.attach_dialogue(scenes, [], words)
    tokens = scenes[4].quote.split()
    assert tokens[0] == "w0" and "…" in tokens and tokens[-1] == "w129"
    assert len(tokens) == sb.QUOTE_WORDS + sb.QUOTE_TAIL + 1


def test_page_has_every_scene_the_strip_notes_and_escapes_text():
    plan = json.loads(json.dumps(PLAN))
    plan["clips"][1]["takeaway"] = "<script>alert(1)</script>"
    page = sb.build(plan, [], notes=["Music only at the ends"])
    for k in range(1, 10):
        assert f'id="scene-{k}"' in page
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert "Decisions for review" in page and "Music only at the ends" in page
    assert page.count('class="c"') == 7 and page.count('class="v"') == 2  # slides and clips on the strip
    assert "QR</span>https://example.com/repo" in page and "A label" in page


def test_tile_name_pattern_accepts_names_not_ui_text():
    assert sb.NAME.match("Jeff Weiner") and sb.NAME.match("Ramsey Jamoul")
    assert not sb.NAME.match("localhost:8787") and not sb.NAME.match("you")
