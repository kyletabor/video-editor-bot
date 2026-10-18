"""Renderer themes from the bot (clipbot/themes.py): `clipbot reel --theme` and framing "theme"
are checked first, recorded next to the plan, and passed to cliprender by --render."""

import json

import pytest

from clipbot import cli, themes
from clipbot.framing import parse_framing
from clipbot.plan import REPO_ROOT, validate
from tests.test_reel import DEMO_MP4, needs_demo

AOI_FILE = {"theme": "aoi", "episode": "04", "date": "Friday, October 9"}


def test_builtin_names_are_the_renderers_themes(tmp_path):
    """The names are the renderer's THEMES, read from its source; each is a module there."""
    assert "aoi" in themes.builtin()
    for name in themes.builtin():
        assert (themes.THEMES_DIR / f"{name}.py").is_file()
    (tmp_path / "__init__.py").write_text('"""no list here"""\nOTHER = ("x",)\n', encoding="utf-8")
    assert themes.builtin(tmp_path) == []  # nothing to go by: any name passes, cliprender judges it


def test_resolve_a_name_a_file_and_an_object(tmp_path, monkeypatch):
    assert themes.resolve("aoi") == {"theme": "aoi"}
    path = tmp_path / "theme.json"
    path.write_text("\ufeff" + json.dumps(AOI_FILE), encoding="utf-8")  # a BOM, as Windows editors write
    assert themes.resolve(str(path)) == AOI_FILE
    assert themes.resolve(dict(AOI_FILE)) == AOI_FILE
    # a relative path is looked for from the working directory, then from the repository root
    monkeypatch.chdir(tmp_path)
    assert themes.theme_file("theme.json") == path.relative_to(tmp_path)
    assert themes.theme_file("render/cliprender/themes/__init__.py") == REPO_ROOT / "render/cliprender/themes/__init__.py"


@pytest.mark.parametrize("content, message", [
    ("{not json", r"cannot read theme file"),
    (json.dumps(["aoi"]), r'expected a JSON object whose "theme" names a theme'),
    (json.dumps({"episode": "04"}), r'expected a JSON object whose "theme" names a theme'),
    (json.dumps({"theme": "aio"}), r"unknown theme 'aio'; use one of aoi"),
])
def test_a_wrong_theme_file_is_an_error_that_names_it(tmp_path, content, message):
    path = tmp_path / "theme.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        themes.resolve(str(path))


def test_an_unknown_name_or_a_missing_file_is_an_error():
    with pytest.raises(ValueError, match=r"--theme: unknown theme 'aio'; use one of aoi or a theme JSON file"):
        themes.resolve("aio")
    with pytest.raises(ValueError, match=r"--theme: theme file out/nowhere/theme\.json not found"):
        themes.resolve("out/nowhere/theme.json")


def test_without_a_renderer_checkout_any_name_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(themes, "THEMES_DIR", tmp_path / "missing")
    assert themes.builtin() == [] and themes.resolve("anything") == {"theme": "anything"}


def test_framing_theme_takes_a_name_a_path_or_the_object():
    assert parse_framing({"theme": " aoi "}).theme == "aoi"
    assert parse_framing({"theme": "out/talk4/theme.json"}).theme == "out/talk4/theme.json"
    assert parse_framing({"theme": AOI_FILE}).theme == AOI_FILE
    with pytest.raises(ValueError, match=r"framing\.theme: expected a theme name"):
        parse_framing({"theme": ""})
    with pytest.raises(ValueError, match=r"framing\.theme: expected a JSON object whose \"theme\""):
        parse_framing({"theme": {"episode": "04"}})
    with pytest.raises(ValueError, match=r"framing\.theme: unknown theme 'aio'"):
        parse_framing({"theme": {"theme": "aio"}})


def test_record_writes_beside_the_plan_and_a_run_without_a_theme_removes_it(tmp_path):
    plan = tmp_path / "plan.json"
    written = themes.record(plan, AOI_FILE)
    assert written == tmp_path / "plan.theme.json" and json.loads(written.read_text(encoding="utf-8")) == AOI_FILE
    notes = []
    assert themes.record(plan, None, log=notes.append) is None
    assert not written.exists() and "removed" in notes[0]
    assert themes.record(plan, None, log=notes.append) is None and len(notes) == 1  # nothing to remove: quiet


def test_a_bad_theme_fails_before_the_source_is_even_read(tmp_path, capsys):
    """The theme is checked before probing, transcription, the silence scan and redaction."""
    out = tmp_path / "plan.json"
    rc = cli.main(["reel", "--source", str(tmp_path / "missing.mp4"), "--theme", "aio", "--out", str(out)])
    assert rc == 1 and "unknown theme 'aio'" in capsys.readouterr().err and not out.exists()
    rc = cli.main(["reel", "--source", str(tmp_path / "missing.mp4"), "--theme", " ", "--out", str(out)])
    assert rc == 1 and "--theme: expected a theme name" in capsys.readouterr().err  # an unset $THEME
    framing = tmp_path / "framing.json"
    framing.write_text(json.dumps({"theme": {"episode": "04"}}), encoding="utf-8")
    rc = cli.main(["reel", "--source", str(tmp_path / "missing.mp4"), "--framing", str(framing), "--out", str(out)])
    assert rc == 1 and "framing.json.theme" in capsys.readouterr().err


@needs_demo
def test_cli_reel_records_the_theme_and_the_plan_does_not_change(tmp_path, capsys):
    out = tmp_path / "plan.json"
    record = tmp_path / "plan.theme.json"
    base = ["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--date", "2026-10-09", "--out", str(out),
            "--keep-fillers"]
    assert cli.main(base) == 0
    plain = out.read_text(encoding="utf-8")
    assert not record.exists()

    theme_file = tmp_path / "theme.json"
    theme_file.write_text(json.dumps(AOI_FILE), encoding="utf-8")
    capsys.readouterr()
    assert cli.main([*base, "--theme", str(theme_file)]) == 0
    assert out.read_text(encoding="utf-8") == plain  # a theme is the renderer's: the plan is the same
    assert json.loads(record.read_text(encoding="utf-8")) == AOI_FILE
    assert f"theme written: {record} (aoi;" in capsys.readouterr().out

    # framing "theme" as the object itself; --theme beats it
    framing = tmp_path / "framing.json"
    framing.write_text(json.dumps({"theme": {"theme": "aoi", "episode": "05"}}), encoding="utf-8")
    assert cli.main([*base, "--framing", str(framing)]) == 0
    assert json.loads(record.read_text(encoding="utf-8")) == {"theme": "aoi", "episode": "05"}
    assert cli.main([*base, "--framing", str(framing), "--theme", "aoi"]) == 0
    assert json.loads(record.read_text(encoding="utf-8")) == {"theme": "aoi"}

    # a later run without a theme leaves no record that would dress the new plan in the old look
    capsys.readouterr()
    assert cli.main(base) == 0
    assert not record.exists() and "theme: removed" in capsys.readouterr().err


@needs_demo
def test_one_command_aoi_reel_passes_the_theme_and_the_wipe_to_the_renderer(tmp_path, monkeypatch):
    """`clipbot reel --style aoi --theme FILE --render`: the Talk #4 command, renderer stubbed."""
    import shutil
    import subprocess as sp

    from tests.test_reel import _FakeProc

    rendered = []
    real_render = cli.render
    monkeypatch.setattr(cli, "render", lambda plan_path, theme=None: rendered.append((plan_path, theme)) or 0)
    theme_file = tmp_path / "theme.json"
    theme_file.write_text(json.dumps(AOI_FILE), encoding="utf-8")
    out = tmp_path / "reel" / "plan.json"
    rc = cli.main(["reel", "--source", str(DEMO_MP4), "--minutes", "0.5", "--out", str(out), "--keep-fillers",
                   "--style", "aoi", "--theme", str(theme_file), "--render"])
    assert rc == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    validate(plan)
    reel = plan["output"]["reel"]
    assert reel["transition"] == {"kind": "module", "seconds": 0.6, "module": "render/cliprender/themes/horizon_wipe.py"}
    assert reel["music"]["path"].endswith("music-cues.wav")
    assert rendered == [(out, out.parent / "plan.theme.json")]

    # the renderer gets the recorded file, and the numpy extra the wipe needs
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(sp, "Popen", lambda cmd, **kw: _FakeProc(cmd, **kw))
    assert real_render(out, theme=out.parent / "plan.theme.json") == 0
    cmd, _ = _FakeProc.calls[-1]
    assert cmd[:7] == ["/usr/bin/uv", "run", "--project", "render", "--extra", "styles", "cliprender"]
    assert cmd[cmd.index("--theme") + 1] == str((out.parent / "plan.theme.json").resolve())
    assert real_render(out) == 0 and "--theme" not in _FakeProc.calls[-1][0]
