"""Themes: the look of a reel's cards, burned captions and labels, chosen at render time.

Why at render time and not in the plan: the contract leaves the look of a card to the renderer
(no image files cross it), so a theme changes nothing the bot decides. The same plan renders
with or without one; only the dressing differs. Choosing it on the command line
(`cliprender plan.json --theme aoi`) therefore needs no contract change, and every plan,
old or new, can be rendered in any theme.

`--theme NAME` uses a built-in theme with its defaults. `--theme FILE.json` names one and fills
in the per-video values the theme shows, such as an episode number and a date:

    {"theme": "aoi", "episode": "04", "date": "Friday, October 9"}

Unknown names and keys are errors, as in the plan itself: a typo would otherwise be seen only
on the rendered card, after the clips' encodes.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..media import RenderError
from ..overlays import LabelLook

# Built-in themes: each is a module here with `theme(values) -> Theme`.
THEMES = ("aoi",)


@dataclass(frozen=True)
class Theme:
    """What a theme changes. `draw_card(card, (width, height))` returns an RGB image of
    exactly that size; `caption_style` is a libass `force_style` for burned captions (None
    keeps FFmpeg's default); `label_look` sets the v1.4 overlay labels."""

    name: str
    draw_card: Callable
    caption_style: str | None = None
    label_look: LabelLook | None = None


def theme_file(text, root):
    """The theme file `text` names: as given (relative to the working directory), else
    relative to the repository root; None when neither exists."""
    path = Path(text)
    if path.is_file():
        return path
    if root is not None and not path.is_absolute() and (Path(root) / path).is_file():
        return Path(root) / path
    return None


def load_theme(spec, root=None):
    """A `Theme` from a built-in name or a theme JSON file; RenderError when neither."""
    if isinstance(spec, Theme):
        return spec
    text = str(spec)
    values = {}
    if text in THEMES:
        name = text
    else:
        path = theme_file(text, root)
        if path is None:
            raise RenderError(
                f"Unknown theme {text!r}: use one of {', '.join(THEMES)} or a theme JSON file"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RenderError(f"Cannot read theme file {path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("theme") not in THEMES:
            raise RenderError(
                f'Theme file {path}: expected a JSON object whose "theme" is one of '
                f"{', '.join(THEMES)}"
            )
        values = {key: value for key, value in data.items() if key != "theme"}
        name = data["theme"]
    module = importlib.import_module(f".{name}", __name__)
    try:
        return module.theme(values)
    except ValueError as exc:
        raise RenderError(f"Theme {name}: {exc}") from exc
