import json
import random
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from clipbot import cli
from clipbot.captions import Cue
from clipbot.cuts import TAIL_SECONDS, pad
from clipbot.plan import load_schema, validate
from clipbot.probe import SourceInfo
from clipbot.reel import (
    BOOKENDS_SECONDS,
    CARD_SECONDS,
    INTRO_SECONDS,
    PAD_SECONDS,
    Moment,
    build_reel_plan,
    clip_text,
    cut_moments,
    fit_moments,
    moments_from_specs,
    pick_moments,
    plan_runtime,
    resolve_overlaps,
    runtime,
    spans_for,
    takeaway_lines,
    target_band,
)
from clipbot.summarize import _ts
from clipbot.words import Word, save_words, words_from_cues
from tests.test_select import DEMO

REPO = Path(__file__).resolve().parents[2]
DEMO_MP4 = REPO / "assets" / "demo-clip.mp4"

_VOCAB = (
    "renderer schema plan clip caption ffmpeg timeline bucket window sentence speaker transcript "
    "summary intro card chapter demo meeting recording repo branch review merge contract validate "
    "segment duration preset aspect burn sidecar whisper model latency cache token prompt agent bead "
    "worktree gate check pipeline output source folder script test fixture sample asset dolt sync push"
).split() + [f"topic{i}" for i in range(140)]  # a real meeting's vocabulary is wide; keep windows distinct
_OPENERS = ("We should decide", "The next step is", "The goal is", "We agree that")


def session(minutes: float = 60, good_every: int = 600, good_at: int = 120, seed: int = 7,
            repeat: str | None = None, repeat_at: tuple[int, ...] = ()) -> list[Cue]:
    """Synthetic Meet-style transcript: 4 s cues of low-density chatter with one dense,
    decision-heavy 32 s passage every `good_every` seconds. `repeat` plants the same
    passage verbatim at each `repeat_at` second so dedupe has something to catch."""
    rng = random.Random(seed)
    cues: list[Cue] = []
    t, n = 0, 0
    while t < minutes * 60:
        pos = t % good_every
        if repeat and any(r <= t < r + 32 for r in repeat_at):
            text = repeat
        elif good_at <= pos < good_at + 32:
            text = f"{_OPENERS[n % 4]} {' '.join(rng.sample(_VOCAB, 6))}."
        else:
            words = rng.sample(_VOCAB, 3) + ["and", "then", "we", "kind", "of", "um", "you", "know"]
            rng.shuffle(words)
            text = " ".join(words[: rng.randint(5, 9)]) + rng.choice(["", "", ".", ","])
        cues.append(Cue(t, t + 4, text, "Kyle Tabor" if (n // 9) % 2 == 0 else "Ramsey Jamoul"))
        t += 4
        n += 1
    return cues


def test_picks_spread_across_the_whole_session():
    cues = session(60)
    moments = pick_moments(cues, 4)
    assert len(moments) >= 4
    starts = [m.start for m in moments]
    assert starts[0] < 15 * 60 and starts[-1] > 45 * 60
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert max(gaps) < 2 * 3600 / 7  # no dead zone wider than two of the seven buckets


def test_runtime_including_cards_within_tolerance():
    moments = pick_moments(session(60), 4)
    low, target, high = target_band(4)
    total = runtime(moments)
    assert low <= total <= high, (total, low, high)
    assert total == INTRO_SECONDS + BOOKENDS_SECONDS + sum(CARD_SECONDS + PAD_SECONDS + m.duration for m in moments)
    # the estimate tracks the real plan: bookends + cards + padded segments
    info = SourceInfo("talk.mp4", 3600.0, True, True, None)
    plan = build_reel_plan(info, moments, "out/x", title="t", date="d", takeaways=["a", "b", "c", "d"])
    assert abs(plan_runtime(plan) - total) <= 6.0


def test_chronological_and_within_window_bounds():
    moments = pick_moments(session(90), 3)
    assert [m.start for m in moments] == sorted(m.start for m in moments)
    for a, b in zip(moments, moments[1:]):
        assert a.end <= b.start
    for m in moments:
        assert 15 <= m.duration <= 60


def test_dense_passages_are_preferred():
    """The planted decision passages should win their buckets over the chatter."""
    moments = pick_moments(session(60), 4)
    hits = sum(1 for m in moments if any(k in m.takeaway.lower() for k in ("decide", "next step", "goal", "agree")))
    assert hits >= len(moments) // 2


def test_near_duplicate_passages_are_deduped():
    marker = "We should decide the renderer plan: the goal is to ship the caption timeline tonight."
    cues = session(60, repeat=marker, repeat_at=(600, 2400))
    moments = pick_moments(cues, 4)
    dupes = [m for m in moments if "ship the caption timeline" in " ".join(cues[i].text for i in m.cue_indexes)]
    assert len(dupes) <= 1


def test_picked_edges_are_sentence_boundaries():
    """veb-t2b.19: heuristic picks open and close on a sentence, never on a bare cue edge."""
    cues = session(60)
    spans = spans_for(cues)
    moments = pick_moments(cues, 4, spans=spans)
    for m in moments:
        assert any(abs(m.start - x) < 1e-6 for sp in spans for x in (sp.start, sp.first)), m.start
        assert any(abs(m.end - x) < 1e-6 for sp in spans for x in (sp.end, sp.last)), m.end
        assert m.requested is not None and m.requested[0] >= m.start and m.requested[1] <= m.end


def test_tiny_target_still_yields_a_reel():
    """0.5 min on the 72 s demo: K x 15 s cannot fit, so the minimum window shrinks; sentence
    snapping widens every window, so the picker falls back to the shortest one per start."""
    moments = pick_moments(DEMO, 0.5)
    assert len(moments) >= 2
    assert all(m.duration >= 8 for m in moments)
    for a, b in zip(moments, moments[1:]):
        assert a.end <= b.start


def test_moments_from_specs_snaps_to_sentence_edges_and_keeps_order():
    """veb-t2b.19: 41 s is inside "Uh, yeah, I'm really questioning..." (cue 36-40 onwards) and
    50.5 s inside "This might be a giant cluster..." which ends in cue 48-52: both edges move
    OUTWARD to the cues those sentences begin and end in. Before, they snapped to the cue edge
    (40, 52) and the clip opened mid-sentence."""
    specs = [
        {"start": 41, "end": 50.5, "title": "The doubt"},
        {"start": "0:00:10", "end": "0:00:22", "why": "Setup"},
    ]
    ms = moments_from_specs(specs, DEMO)
    assert [(m.start, m.end) for m in ms] == [(36, 52), (4, 24)]  # given order, not chronological
    assert [m.requested for m in ms] == [(41, 50.5), (10, 22)]
    assert ms[0].title == "The doubt" and ms[0].takeaway == "The doubt."
    assert ms[1].why == "Setup" and ms[1].card()["lines"] == ["Setup"]
    assert ms[1].takeaway.endswith((".", "!", "?"))


def test_end_mid_sentence_snaps_to_sentence_end_plus_tail():
    """The brief's acceptance test: a moment that ends mid-sentence ends, in the plan, at the
    sentence end + tail_seconds. Word timings make the sentence edges exact."""
    cues = [Cue(0, 3, "We tried one agent first."), Cue(3, 8, "Then we split the work across two repos and it worked."),
            Cue(8, 10, "Next topic.")]
    ws = [Word(0.0, 0.4, "We"), Word(0.4, 0.8, "tried"), Word(0.8, 1.2, "one"), Word(1.2, 1.6, "agent"), Word(1.6, 2.4, "first."),
          Word(3.0, 3.3, "Then"), Word(3.3, 3.5, "we"), Word(3.5, 3.9, "split"), Word(3.9, 4.1, "the"), Word(4.1, 4.5, "work"),
          Word(4.5, 5.0, "across"), Word(5.0, 5.3, "two"), Word(5.3, 5.9, "repos"), Word(5.9, 6.1, "and"), Word(6.1, 6.3, "it"),
          Word(6.3, 7.6, "worked."), Word(8.2, 8.6, "Next"), Word(8.6, 9.4, "topic.")]
    spans = spans_for(cues, ws)
    [m] = moments_from_specs([{"start": 1.0, "end": 5.5, "title": "Split"}], cues, spans=spans)
    assert (m.start, m.end) == (0.0, 7.6)
    plan = build_reel_plan(SourceInfo("assets/demo-clip.mp4", 72.0, True, True, 0), [m], "out/x", title="t", date="d")
    assert plan["clips"][0]["segments"] == [{"start": 0.0, "end": round(7.6 + TAIL_SECONDS, 3)}]
    validate(plan)
    # a request already on the sentence boundary is left alone
    [m2] = moments_from_specs([{"start": 3.0, "end": 7.6}], cues, spans=spans)
    assert (m2.start, m2.end) == (3.0, 7.6)


def test_moments_from_specs_rejects_backwards_span():
    with pytest.raises(ValueError):
        moments_from_specs([{"start": 20, "end": 10}], DEMO)
    with pytest.raises(ValueError):
        moments_from_specs([{"start": "abc", "end": 10}], DEMO)


def test_lesson_framing_on_cards_specs_and_takeaways():
    """veb-t2b.21: a moment with a lesson leads with it; context and why become the card body."""
    spec = {"start": 36, "end": 52, "title": "The doubt", "lesson": "write the goal down before building the bot",
            "context": "the room had no definition of a good clip yet", "why": "Decision"}
    [m] = moments_from_specs([spec], DEMO)
    assert m.lesson == "Write the goal down before building the bot" and m.context.startswith("The room had")
    assert m.title == "The doubt"
    assert m.takeaway == "Write the goal down before building the bot."  # the lesson is the idea the clip lands
    card = m.card()
    assert card["title"] == m.lesson and card["lines"] == [m.context, "Decision"] and card["seconds"] == CARD_SECONDS
    again = moments_from_specs([m.spec()], DEMO)[0]  # moments.json round trip keeps the framing
    assert (again.lesson, again.context, again.start, again.end) == (m.lesson, m.context, 36, 52)
    assert "lesson" not in Moment(0, 1, "t.", "t").spec()
    long = moments_from_specs([dict(spec, lesson="l" * 100, context="c" * 200)], DEMO)[0]
    assert len(long.lesson) <= 80 and len(long.context) <= 120
    assert Moment(0, 1, "t.", "t", lines=("a", "b"), lesson="L", context="C").card()["lines"] == ["C", "a", "b"]


def test_resolve_overlaps_trims_the_later_moment_and_drops_swallowed_ones():
    a = Moment(10, 30, "a.", "a")
    b = Moment(25, 40, "b.", "b")
    c = Moment(28, 30.5, "c.", "c")
    d = Moment(80, 90, "d.", "d")
    e = Moment(86, 95, "e.", "e")
    fixed, notes = resolve_overlaps([b, a, c, d, e], duration=85.0)
    assert [(m.start, m.end) for m in fixed] == [(30, 40), (10, 30), (80, 85.0)]  # order kept
    assert len(notes) == 4 and any("dropped" in n for n in notes) and any("ran past the end" in n for n in notes)
    assert resolve_overlaps([a, d]) == ([a, d], [])


def test_takeaway_lines_prefer_given_then_lessons_then_titles_then_summary():
    ms = [Moment(0, 10, "t1.", "Title one", lesson="Lesson one"), Moment(20, 30, "t2.", "Title two")]
    assert takeaway_lines(ms, DEMO, given=["  Given A ", "", "Given B"]) == ["Given A", "Given B"]
    assert takeaway_lines(ms, DEMO, given=["", "  "]) == ["Lesson one"]  # an empty file falls through
    assert takeaway_lines(ms, DEMO) == ["Lesson one"]
    plain = [replace(m, lesson="") for m in ms]
    assert takeaway_lines(plain, DEMO, hand_picked=True) == ["Title one", "Title two"]
    auto = takeaway_lines(plain, DEMO)
    assert auto and all(x and x[0].isupper() for x in auto)  # extractive summary, tidied


def test_cut_moments_fills_segments_and_the_plan_uses_them():
    ws = [Word(10.0, 12.0, "Alpha"), Word(12.0, 13.0, "beta."), Word(16.0, 17.0, "Gamma"), Word(17.0, 20.0, "delta.")]
    m = Moment(10.0, 20.0, "t.", "t")
    [cut], [rep] = cut_moments([m], ws, [[(13.0, 16.0)]], duration=72.0)
    assert rep.silence_seconds == pytest.approx(2.3) and cut.kept == pytest.approx(10.45 - 2.3)
    assert runtime([cut]) < runtime([m]) and cut.duration == m.duration
    info = SourceInfo("assets/demo-clip.mp4", 72.0, True, True, 0)
    plan = build_reel_plan(info, [cut], "out/x", title="t", date="d")
    assert plan["clips"][0]["segments"] == [{"start": 9.85, "end": 13.35}, {"start": 15.65, "end": 20.3}]
    validate(plan)
    [same], [rep2] = cut_moments([m], ws, [[]], fillers=False)
    assert same.segments == ((9.85, 20.3),) and rep2.removed_seconds == 0


def test_fit_moments_stops_near_target_and_never_overlaps():
    cands = [Moment(i * 40, i * 40 + 30, "t.", "t", score=10 - i) for i in range(10)]
    cands.append(Moment(5, 25, "overlap.", "overlap", score=100))  # best score but overlaps #0
    picked = fit_moments(cands, 2)
    low, target, high = target_band(2)
    assert runtime(picked) <= high
    assert [m.start for m in picked] == sorted(m.start for m in picked)
    for a, b in zip(picked, picked[1:]):
        assert a.end <= b.start


def test_tidy_sentence_fixes_orphan_quotes_and_case():
    """Regression (talk2 run): a card read `bots are doing all the work." And I asked, "Is it...`."""
    from clipbot.reel import tidy_sentence

    assert tidy_sentence('bots are doing all the work." And I asked, "Is it the right thing to do?') == \
        "Bots are doing all the work. And I asked, Is it the right thing to do?"
    assert tidy_sentence('"Fully quoted."') == '"Fully quoted."'
    assert tidy_sentence("yeah, I'm questioning it.") == "Yeah, I'm questioning it."
    m = moments_from_specs([{"start": 36, "end": 52}], DEMO)[0]
    assert m.title[0].isupper() and m.takeaway[0].isupper()


def test_clip_text_cuts_at_a_word_boundary():
    long = "word " * 40
    out = clip_text(long, 80)
    assert len(out) <= 80 and out.endswith("…") and not out[:-1].endswith(" ")
    assert clip_text("short.", 80) == "short."


def test_plan_runtime_counts_bookends_cards_and_segments():
    plan = {
        "output": {"reel": {"intro": {"seconds": 4}, "outro": {"seconds": 3}, "opening": [{"seconds": 5}],
                            "closing": [{"seconds": 6}, {"seconds": 2}]}},
        "clips": [{"card": {"seconds": 3}, "segments": [{"start": 0, "end": 10}, {"start": 20, "end": 25}]},
                  {"segments": [{"start": 30, "end": 40}]}],
    }
    assert plan_runtime(plan) == 4 + 3 + 5 + 6 + 2 + 3 + 15 + 10
    assert plan_runtime({"output": {}, "clips": []}) == 0


def test_build_reel_plan_validates_against_v12():
    info = SourceInfo("assets/demo-clip.mp4", 72.0, True, True, 0)
    moments = pick_moments(DEMO, 0.5)
    plan = build_reel_plan(info, moments, "out/demo-reel", title="Demo clip", date="2026-09-25",
                           takeaways=["Test the format first", "Write the goal down"], music="assets/music/bed.mp3")
    validate(plan, load_schema())
    reel = plan["output"]["reel"]
    assert reel["filename"] == "reel.mp4" and reel["chapter_cards"] == "all"
    assert reel["intro"]["title"] == "Demo clip" and reel["intro"]["seconds"] == INTRO_SECONDS
    assert reel["intro"]["lines"] == ["Recorded 2026-09-25", f"{len(moments)} moments · {_ts(plan_runtime(plan))}"]
    assert reel["opening"][0]["title"] == "What you'll learn" and 1 <= len(reel["opening"][0]["lines"]) <= 4
    assert reel["closing"] == [{"title": "Takeaways", "lines": ["Test the format first", "Write the goal down"], "seconds": 4.5}]
    assert reel["outro"]["title"] == "That's the session"
    assert reel["transition"] == {"kind": "dip", "seconds": 0.4} and reel["audio_fade_seconds"] == 0.15
    assert reel["music"] == {"path": "assets/music/bed.mp3"}
    for clip, m in zip(plan["clips"], moments):
        assert clip["card"]["seconds"] == CARD_SECONDS
        assert 1 <= len(clip["card"]["title"]) <= 80
        assert clip["card"]["lines"] and all(len(ln) <= 120 for ln in clip["card"]["lines"])
        s, e = pad(m.start, m.end, duration=72.0)
        assert clip["segments"] == [{"start": round(s, 3), "end": round(e, 3)}]


def test_build_reel_plan_defaults_no_music_no_closing_without_takeaways():
    info = SourceInfo("assets/demo-clip.mp4", 72.0, True, True, 0)
    plan = build_reel_plan(info, pick_moments(DEMO, 0.5), "out/demo-reel", title="t", date="d", transition="cut")
    reel = plan["output"]["reel"]
    assert "music" not in reel and "closing" not in reel and reel["transition"]["kind"] == "cut"
    validate(plan)


class _FakeProc:
    """Stands in for the cliprender subprocess: records how it was started."""

    calls: list = []

    def __init__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        self.stdout = iter(kw.pop("_lines", ["clip-01\t/x/clip-01.mp4\t12.000000\n"]))
        self.returncode = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_render_drops_virtual_env_and_reports_reel_line(monkeypatch, capsys, tmp_path):
    """Regression (lane C review): the nested uv warned about bot/.venv on every --render."""
    import shutil
    import subprocess as sp

    monkeypatch.setenv("VIRTUAL_ENV", "/repo/bot/.venv")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv")
    lines = ["clip-01\t/x/clip-01.mp4\t12.000000\n", "reel\t/x/reel.mp4\t34.000000\n"]
    monkeypatch.setattr(sp, "Popen", lambda cmd, **kw: _FakeProc(cmd, _lines=lines, **kw))
    assert cli.render(tmp_path / "plan.json") == 0
    cmd, kw = _FakeProc.calls[-1]
    assert cmd[:5] == ["/usr/bin/uv", "run", "--project", "render", "cliprender"] and "--overwrite" in cmd
    assert "VIRTUAL_ENV" not in kw["env"] and kw.get("shell") is None
    out = capsys.readouterr().out
    assert "reel written: /x/reel.mp4" in out and "clip-01\t/x/clip-01.mp4" in out

    monkeypatch.setattr(sp, "Popen", lambda cmd, **kw: _FakeProc(cmd, **kw))  # v1 renderer: no reel line
    assert cli.render(tmp_path / "plan.json") == 0
    assert "did not report a reel" in capsys.readouterr().out


def _tools_present() -> bool:
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


needs_demo = pytest.mark.skipif(not (DEMO_MP4.exists() and _tools_present()), reason="needs ffmpeg + assets/demo-clip.mp4")


@needs_demo
def test_cli_reel_end_to_end_on_demo_clip(tmp_path, capsys):
    out = tmp_path / "plan.json"
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--title", "Demo reel", "--out", str(out)])
    assert rc == 0, capsys.readouterr()
    plan = json.loads(out.read_text(encoding="utf-8"))
    validate(plan)
    assert len(plan["clips"]) >= 2
    reel = plan["output"]["reel"]
    assert reel["intro"]["title"] == "Demo reel"
    assert reel["opening"][0]["title"] == "What you'll learn" and reel["closing"] and reel["outro"]
    assert reel["transition"]["kind"] == "dip" and reel["audio_fade_seconds"] == 0.15
    assert plan["source"]["captions"] == {"kind": "embedded"}
    assert plan["summary"]["path"].endswith("summary.md")
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert summary.count("## Reel") == 1 and "## Takeaways" in summary
    specs = json.loads((tmp_path / "moments.json").read_text(encoding="utf-8"))
    assert len(specs) == len(plan["clips"])
    for s, c in zip(specs, plan["clips"]):  # moments.json keeps speech edges; the plan pads and cuts
        assert c["segments"][0]["start"] <= s["start"] + 1.5 and c["segments"][-1]["end"] >= s["end"] - 1.5
    stdout = capsys.readouterr().out
    assert "plan written" in stdout and "reel:" in stdout


@needs_demo
def test_cli_reel_with_moments_file_round_trip(tmp_path, capsys):
    moments = tmp_path / "moments.json"
    moments.write_text(json.dumps([
        {"start": 41, "end": 50.5, "title": "Questioning the clip bot", "lines": ["Kyle's doubt", "Decision"]},
        {"start": "0:00:20", "end": "0:00:37", "why": "Demo"},
    ]), encoding="utf-8")
    out = tmp_path / "plan.json"
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--moments", str(moments), "--out", str(out),
                   "--keep-fillers"])
    assert rc == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    validate(plan)
    assert [c["card"]["title"] for c in plan["clips"]][0] == "Questioning the clip bot"
    assert plan["clips"][0]["card"]["lines"] == ["Kyle's doubt", "Decision"]
    # 41-50.5 snaps to the sentences it cuts into (36-52); 20-37 to 20-44 ("Uh, yeah, I'm really
    # questioning..." ends in cue 40-44). They then share 36-44, so the later one in time starts where
    # the earlier ends: 44-52. --keep-fillers: one padded segment per clip.
    assert plan["clips"][0]["segments"] == [{"start": 43.85, "end": 52.3}]
    assert plan["clips"][1]["segments"] == [{"start": 19.85, "end": 44.3}]
    assert "overlapped its neighbour" in capsys.readouterr().err
    written = json.loads((tmp_path / "moments.json").read_text(encoding="utf-8"))
    assert [(m["start"], m["end"]) for m in written] == [(44, 52), (20, 44)]
    # the written moments.json can be fed straight back in and re-snaps to the same moments
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--moments", str(tmp_path / "moments.json"), "--out", str(out),
                   "--keep-fillers"])
    assert rc == 0
    again = json.loads((tmp_path / "moments.json").read_text(encoding="utf-8"))
    assert [(m["start"], m["end"], m["title"]) for m in again] == [(m["start"], m["end"], m["title"]) for m in written]
    plan2 = json.loads(out.read_text(encoding="utf-8"))
    validate(plan2)
    assert plan2["clips"][0]["segments"] == [{"start": 43.85, "end": 52.3}]
    # times that already sit on sentence boundaries are left alone (the old cue-edge behaviour is a fixed point)
    moments.write_text(json.dumps([{"start": 36, "end": 52, "title": "Fixed"}]), encoding="utf-8")
    assert cli.main(["reel", "--source", str(DEMO_MP4), "--moments", str(moments), "--out", str(out), "--keep-fillers"]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["clips"][0]["segments"] == [{"start": 35.85, "end": 52.3}]


@needs_demo
def test_cli_reel_takeaways_music_transition_and_words(tmp_path, capsys):
    takeaways = tmp_path / "takeaways.txt"
    takeaways.write_text("Test the format first\n\nWrite the goal down\n", encoding="utf-8")
    # word timings estimated from the demo's own captions, saved as if whisper had timed them:
    # enough to exercise the --words path and the filler cut end to end
    from clipbot.captions import extract_embedded_srt, parse_srt

    cues = parse_srt(extract_embedded_srt(DEMO_MP4, 0))
    words_file = tmp_path / "demo.words.json"
    save_words(words_file, [replace(w, timed=True, cut_start=None, cut_end=None) for w in words_from_cues(cues)])
    out = tmp_path / "plan.json"
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--out", str(out), "--takeaways", str(takeaways),
                   "--music", str(REPO / "assets" / "demo-clip.mp4"), "--transition", "dissolve", "--words", str(words_file)])
    err = capsys.readouterr()
    assert rc == 0, err
    plan = json.loads(out.read_text(encoding="utf-8"))
    validate(plan)
    reel = plan["output"]["reel"]
    assert reel["closing"] == [{"title": "Takeaways", "lines": ["Test the format first", "Write the goal down"], "seconds": 4.5}]
    assert reel["music"] == {"path": "assets/demo-clip.mp4"} and reel["transition"]["kind"] == "dissolve"
    assert "timed words from" in err.err and "no word timestamps" not in err.err
    assert "fillers" in err.out
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "- Test the format first\n- Write the goal down" in summary


@needs_demo
def test_cli_reel_llm_without_key_falls_back(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    out = tmp_path / "plan.json"
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--llm", "--out", str(out)])
    err = capsys.readouterr().err
    assert rc == 0 and "heuristic" in err
    validate(json.loads(out.read_text(encoding="utf-8")))
