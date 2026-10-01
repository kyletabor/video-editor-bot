"""Style packs (clipbot/styles.py): choosing a reel's music and transition by name."""

import json

import pytest

from clipbot import cli, styles
from clipbot.framing import parse_framing
from clipbot.plan import REPO_ROOT, validate
from tests.test_reel import DEMO_MP4, needs_demo


def write_style(root, data, files=("music/bed.mp3",)):
    for name in files:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"x")
    (root / "style.json").write_text(json.dumps(data), encoding="utf-8")
    return root


BED = {"name": "mine", "title": "Mine", "music": {"kind": "bed", "file": "music/bed.mp3", "gain_db": -10},
       "transition": {"kind": "dissolve", "seconds": 0.5}}


def test_shipped_styles_load_and_pipeline_is_the_cue_style():
    assert {"pipeline", "signal", "aurora", "classic"} <= set(styles.names())
    loaded = {s.name: s for s in styles.available()}
    pipeline = loaded["pipeline"]
    assert pipeline.music["kind"] == "cues" and pipeline.music["bpm"] == 104 and pipeline.music["gain_db"] == -4.5
    assert pipeline.transition == {"kind": "dip", "seconds": 0.4}
    assert pipeline.music_label == "cues at 104 BPM" and pipeline.transition_label == "dip 0.4 s"
    for style in loaded.values():  # every file a style names is in the repository
        assert style.root.is_dir() and style.title and style.description
    assert loaded["classic"].file(loaded["classic"].music["file"]) == REPO_ROOT / "assets" / "music" / "bed.mp3"


def test_load_by_directory_or_style_json_path(tmp_path):
    root = write_style(tmp_path / "mine", BED)
    for target in (root, root / "style.json"):
        style = styles.load(str(target))
        assert style.name == "mine" and style.root == root.resolve()
        assert style.music == {"kind": "bed", "file": "music/bed.mp3", "gain_db": -10.0}
        assert styles.plan_transition(style) == {"kind": "dissolve", "seconds": 0.5}


def test_unknown_style_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="unknown style 'nope'; available: .*pipeline"):
        styles.load("nope")


@pytest.mark.parametrize("change, message", [
    ({"music": {"kind": "loop", "file": "music/bed.mp3"}}, r"music\.kind: expected one of bed, cues"),
    ({"music": {"kind": "bed", "file": "music/missing.mp3"}}, r"music\.file: music/missing\.mp3 not found"),
    ({"music": {"kind": "bed", "file": "music/bed.mp3", "gain_db": 3}}, r"gain_db: 3 is outside -40\.\.0"),
    ({"music": {"kind": "bed", "file": "music/bed.mp3", "volume": 1}}, r"unknown key\(s\) volume"),
    ({"music": {"kind": "cues", "bpm": 100, "hold": {"file": "music/bed.mp3", "seconds": 4}}}, r"missing vamp"),
    ({"transition": {"kind": "wipe"}}, r"transition\.kind: 'wipe' is not one of"),
    ({"transition": {"kind": "module"}}, r"kind module needs \"module\""),
    ({"transition": {"kind": "dip", "module": "music/bed.mp3"}}, r"only kind module takes a module"),
    ({"transition": {"kind": "dip", "seconds": 4}}, r"seconds: 4 is outside 0\.1\.\.1\.5"),
    ({"title": ""}, r"title: expected non-empty text"),
    ({"colour": "blue"}, r"unknown key\(s\) colour"),
])
def test_a_wrong_style_is_an_error_that_names_the_field(tmp_path, change, message):
    root = write_style(tmp_path / "bad", {**BED, **change})
    with pytest.raises(ValueError, match=message):
        styles.load(str(root))


def test_cue_style_defaults_and_module_transition(tmp_path):
    files = ("m/a.flac", "m/a-home.flac", "m/b.flac", "m/hold.flac", "t/wipe.py")
    data = {"name": "cut", "title": "Cut", "music": {
        "kind": "cues", "bpm": 120,
        "vamp": [{"file": "m/a.flac", "resolved": "m/a-home.flac"}, {"file": "m/b.flac"}],
        "hold": {"file": "m/hold.flac", "seconds": 4}},
        "transition": {"kind": "module", "module": "t/wipe.py", "seconds": 1.0}}
    style = styles.load(str(write_style(tmp_path / "cut", data, files)))
    assert style.music["beats_per_bar"] == 4 and "pickup" not in style.music and "outro" not in style.music
    assert style.music["stings"] == ["m/a-home.flac", "m/b.flac"]  # the bars that land on home
    assert style.transition_label == "wipe 1 s"
    assert styles.plan_transition(style) == {"kind": "module", "seconds": 1.0, "module": (style.root / "t/wipe.py").as_posix()}


def test_framing_file_can_pin_the_style_and_the_audio_fade():
    framing = parse_framing({"style": "pipeline", "audio_fade_seconds": 0.2})
    assert framing.style == "pipeline" and framing.audio_fade_seconds == 0.2
    with pytest.raises(ValueError, match=r"audio_fade_seconds: 2 is outside 0\.\.1"):
        parse_framing({"audio_fade_seconds": 2})


def test_cli_styles_lists_every_style(capsys):
    assert cli.main(["styles"]) == 0
    out = capsys.readouterr().out
    assert "pipeline\tPipeline: funk organ theme\tcues at 104 BPM\tdip 0.4 s" in out
    assert "signal\t" in out and "clipbot reel --style NAME" in out


@needs_demo
def test_cli_reel_bed_style_from_the_framing_file_and_what_overrides_it(tmp_path, capsys):
    framing = tmp_path / "framing.json"
    framing.write_text(json.dumps({"style": "signal", "music_gain_db": -12, "audio_fade_seconds": 0.3}), encoding="utf-8")
    out = tmp_path / "plan.json"
    base = ["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--out", str(out), "--keep-fillers"]
    assert cli.main([*base, "--framing", str(framing)]) == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    validate(plan)
    reel = plan["output"]["reel"]
    assert reel["music"] == {"path": "assets/styles/signal/music/signal.mp3", "gain_db": -12.0, "fade_seconds": 1.0}
    assert reel["transition"] == {"kind": "dissolve", "seconds": 0.5} and reel["audio_fade_seconds"] == 0.3
    assert "style signal: bed signal.mp3 under the cards" in capsys.readouterr().err

    # a cue style carries its own fades: a framing fade is ignored, and says so
    framing.write_text(json.dumps({"style": "pipeline", "music_fade_seconds": 2.0}), encoding="utf-8")
    assert cli.main([*base, "--framing", str(framing)]) == 0
    assert "music_fade_seconds in" in capsys.readouterr().err
    assert json.loads(out.read_text(encoding="utf-8"))["output"]["reel"]["music"]["fade_seconds"] == 0.0

    # --style beats the framing file's style; --transition and --music beat the style
    assert cli.main([*base, "--framing", str(framing), "--style", "classic", "--transition", "cut"]) == 0
    reel = json.loads(out.read_text(encoding="utf-8"))["output"]["reel"]
    assert reel["music"]["path"] == "assets/music/bed.mp3" and reel["transition"]["kind"] == "cut"
    assert cli.main([*base, "--style", "classic", "--music", str(DEMO_MP4)]) == 0
    reel = json.loads(out.read_text(encoding="utf-8"))["output"]["reel"]
    assert reel["music"] == {"path": "assets/demo-clip.mp4"} and reel["transition"] == {"kind": "dip", "seconds": 0.4}

    # without a style nothing changes: dip, no music
    assert cli.main(base) == 0
    reel = json.loads(out.read_text(encoding="utf-8"))["output"]["reel"]
    assert "music" not in reel and reel["transition"] == {"kind": "dip", "seconds": 0.4}


@needs_demo
def test_cli_reel_future_style_names_its_transition_module_and_render_asks_for_numpy(tmp_path, monkeypatch):
    import shutil
    import subprocess as sp

    from tests.test_reel import _FakeProc

    out = tmp_path / "plan.json"
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--out", str(out), "--style", "future",
                   "--keep-fillers"])
    assert rc == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    validate(plan)  # the contract knows kind module
    reel = plan["output"]["reel"]
    assert reel["transition"] == {"kind": "module", "seconds": 1.0, "module": "assets/styles/transitions/token_stream.py"}
    assert reel["music"]["path"] == "assets/styles/signal/music/signal.mp3"
    assert (REPO_ROOT / reel["transition"]["module"]).is_file()

    # a module transition is drawn with numpy, an extra of the renderer: --render must ask for it
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(sp, "Popen", lambda cmd, **kw: _FakeProc(cmd, **kw))
    assert cli.render(out) == 0
    cmd, _ = _FakeProc.calls[-1]
    assert cmd[:7] == ["/usr/bin/uv", "run", "--project", "render", "--extra", "styles", "cliprender"]
    reel["transition"] = {"kind": "dip", "seconds": 0.4}
    out.write_text(json.dumps(plan), encoding="utf-8")
    assert cli.render(out) == 0
    assert "--extra" not in _FakeProc.calls[-1][0]
