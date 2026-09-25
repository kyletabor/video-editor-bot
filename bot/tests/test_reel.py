import json
import random
import subprocess
from pathlib import Path

import pytest

from clipbot import cli
from clipbot.captions import Cue
from clipbot.plan import load_schema, validate
from clipbot.probe import SourceInfo
from clipbot.reel import (
    CARD_SECONDS,
    INTRO_SECONDS,
    Moment,
    build_reel_plan,
    clip_text,
    fit_moments,
    moments_from_specs,
    pick_moments,
    runtime,
    target_band,
)
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
    assert total == INTRO_SECONDS + sum(CARD_SECONDS + m.duration for m in moments)


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


def test_tiny_target_still_yields_a_reel():
    """0.5 min on the 72 s demo: K x 15 s cannot fit, so the minimum window shrinks."""
    moments = pick_moments(DEMO, 0.5)
    assert len(moments) >= 2
    assert all(m.duration >= 8 for m in moments)


def test_moments_from_specs_snaps_to_cue_edges_and_keeps_order():
    specs = [
        {"start": 41, "end": 50.5, "title": "The doubt"},
        {"start": "0:00:10", "end": "0:00:22", "why": "Setup"},
    ]
    ms = moments_from_specs(specs, DEMO)
    assert [(m.start, m.end) for m in ms] == [(40, 52), (8, 24)]  # given order, not chronological
    assert ms[0].title == "The doubt" and ms[0].takeaway == "The doubt."
    assert ms[1].why == "Setup" and ms[1].card()["lines"] == ["Setup"]
    assert ms[1].takeaway.endswith((".", "!", "?"))


def test_moments_from_specs_rejects_backwards_span():
    with pytest.raises(ValueError):
        moments_from_specs([{"start": 20, "end": 10}], DEMO)
    with pytest.raises(ValueError):
        moments_from_specs([{"start": "abc", "end": 10}], DEMO)


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


def test_build_reel_plan_validates_against_v11():
    info = SourceInfo("assets/demo-clip.mp4", 72.0, True, True, 0)
    moments = pick_moments(DEMO, 0.5)
    plan = build_reel_plan(info, moments, "out/demo-reel", title="Demo clip", date="2026-09-25")
    validate(plan, load_schema())
    reel = plan["output"]["reel"]
    assert reel["filename"] == "reel.mp4" and reel["chapter_cards"] == "all"
    assert reel["intro"]["title"] == "Demo clip" and reel["intro"]["seconds"] == INTRO_SECONDS
    assert reel["intro"]["lines"][0] == "Recorded 2026-09-25"
    assert reel["intro"]["lines"][1].startswith(f"{len(moments)} moments · ")
    for clip, m in zip(plan["clips"], moments):
        assert clip["card"]["seconds"] == CARD_SECONDS
        assert 1 <= len(clip["card"]["title"]) <= 80
        assert clip["card"]["lines"] and all(len(ln) <= 120 for ln in clip["card"]["lines"])
        assert clip["segments"] == [{"start": m.start, "end": m.end}]


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
    assert plan["output"]["reel"]["intro"]["title"] == "Demo reel"
    assert plan["source"]["captions"] == {"kind": "embedded"}
    assert plan["summary"]["path"].endswith("summary.md")
    assert (tmp_path / "summary.md").read_text(encoding="utf-8").count("## Reel") == 1
    specs = json.loads((tmp_path / "moments.json").read_text(encoding="utf-8"))
    assert [s["start"] for s in specs] == [c["segments"][0]["start"] for c in plan["clips"]]
    stdout = capsys.readouterr().out
    assert "plan written" in stdout and "reel:" in stdout


@needs_demo
def test_cli_reel_with_moments_file_round_trip(tmp_path):
    moments = tmp_path / "moments.json"
    moments.write_text(json.dumps([
        {"start": 36, "end": 52, "title": "Questioning the clip bot", "lines": ["Kyle's doubt", "Decision"]},
        {"start": "0:00:20", "end": "0:00:32", "why": "Demo"},
    ]), encoding="utf-8")
    out = tmp_path / "plan.json"
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--moments", str(moments), "--out", str(out)])
    assert rc == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    validate(plan)
    assert [c["card"]["title"] for c in plan["clips"]][0] == "Questioning the clip bot"
    assert plan["clips"][0]["card"]["lines"] == ["Kyle's doubt", "Decision"]
    assert plan["clips"][0]["segments"] == [{"start": 36.0, "end": 52.0}]
    assert plan["clips"][1]["segments"] == [{"start": 20.0, "end": 32.0}]
    # the written moments.json can be fed straight back in
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--moments", str(tmp_path / "moments.json"), "--out", str(out)])
    assert rc == 0


@needs_demo
def test_cli_reel_llm_without_key_falls_back(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    out = tmp_path / "plan.json"
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--llm", "--out", str(out)])
    err = capsys.readouterr().err
    assert rc == 0 and "heuristic" in err
    validate(json.loads(out.read_text(encoding="utf-8")))
