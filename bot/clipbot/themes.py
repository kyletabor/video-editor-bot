"""Themes: the renderer's look for a reel's cards, burned captions and labels (`clipbot reel --theme`).

A theme belongs to the renderer, not to the plan. The contract leaves the look of a card to
the renderer, so `cliprender --theme NAME|FILE` picks it at render time and the plan does not
change (render/README.md, "Themes"). The bot passes it on, and adds two things:

- It checks the theme's name before the slow steps (transcription, the silence scan,
  redaction), in the renderer's order: a built-in name, else a theme file (as given, else from
  the repository root), else an error. A theme file must be a JSON object whose "theme" is a
  built-in name. That theme's own keys (aoi: an episode of up to 4 characters, a date of up
  to 40) are the renderer's to judge; cliprender checks them before its first encode.
- It records the theme next to the plan as `<plan>.theme.json`, in the renderer's theme-file
  form, `{"theme": "aoi", "episode": "04", "date": "Friday, October 9"}`. `--render` passes
  that copy, and a later `cliprender PLAN --theme <plan>.theme.json` renders the same look even
  if the original file has changed. A run without a theme removes an earlier record, so the
  outputs never name a look the plan was not made with.

The built-in names are the renderer's own `THEMES` (render/cliprender/themes/__init__.py),
read from its source rather than imported: the renderer is a separate project with its own
dependencies. Without a renderer next to the bot any name passes here and cliprender judges it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from .plan import REPO_ROOT

THEMES_DIR = REPO_ROOT / "render" / "cliprender" / "themes"


def builtin(themes_dir: Path | None = None) -> list[str]:
    """The renderer's built-in theme names, its own `THEMES`; empty without a renderer checkout."""
    init = (themes_dir or THEMES_DIR) / "__init__.py"
    try:
        tree = ast.parse(init.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == ["THEMES"]:
            try:
                return sorted(ast.literal_eval(node.value))
            except ValueError:
                return []
    return []


def _check_name(name: str, where: str) -> None:
    known = builtin()
    if known and name not in known:
        raise ValueError(f"{where}: unknown theme {name!r}; use one of {', '.join(known)} or a theme JSON file")


def _check_object(data, where: str) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("theme"), str) or not data["theme"].strip():
        raise ValueError(f'{where}: expected a JSON object whose "theme" names a theme, e.g. {{"theme": "aoi"}}')
    _check_name(data["theme"], where)
    return dict(data)


def check_spec(value, where: str) -> str | dict:
    """The framing file's "theme": a name or a file path (text), or the theme file's object itself."""
    if isinstance(value, dict):
        return _check_object(value, where)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{where}: expected a theme name, a theme JSON file or {{"theme": NAME, ...}}')
    return value.strip()


def theme_file(text: str) -> Path | None:
    """The file `text` names: as given (relative to the working directory), else relative to the
    repository root, as cliprender looks for it; None when neither exists."""
    path = Path(text).expanduser()
    if path.is_file():
        return path
    if not path.is_absolute() and (REPO_ROOT / path).is_file():
        return REPO_ROOT / path
    return None


def resolve(spec: str | dict, where: str = "--theme") -> dict:
    """The theme-file object for a name, a theme file or an object. Raises ValueError."""
    if isinstance(spec, dict):
        return _check_object(spec, where)
    known = builtin()
    if spec in known:
        return {"theme": spec}
    path = theme_file(spec)
    if path is None:
        if spec.lower().endswith(".json"):
            raise ValueError(f"{where}: theme file {spec} not found")
        if known:
            raise ValueError(f"{where}: unknown theme {spec!r}; use one of {', '.join(known)} or a theme JSON file")
        return {"theme": spec}  # no renderer checkout to ask; cliprender checks the name
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))  # BOM allowed, as cliprender reads it
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"{where}: cannot read theme file {path} ({e})") from e
    return _check_object(data, f"{where} {path}")


def recorded_path(plan_path: str | Path) -> Path:
    """Where the theme a plan was made with is kept: `<plan stem>.theme.json` beside the plan."""
    plan_path = Path(plan_path)
    return plan_path.with_name(plan_path.stem + ".theme.json")


def record(plan_path: str | Path, theme: dict | None, *, log=lambda msg: None) -> Path | None:
    """Write `theme` next to the plan and return the file; with None, remove an earlier record."""
    path = recorded_path(plan_path)
    if theme is None:
        if path.is_file():
            path.unlink()
            log(f"theme: removed {path} from an earlier run (this plan has no theme)")
        return None
    path.write_text(json.dumps(theme, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
