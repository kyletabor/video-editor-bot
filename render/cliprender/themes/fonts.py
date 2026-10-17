"""System font stacks for themes, resolved to font files on the rendering host.

Why system fonts: a brand designed in the browser names CSS stacks such as `system-ui` and
`ui-monospace`, which mean "the platform's own face" (Segoe UI and Cascadia on Windows, San
Francisco and SF Mono on macOS, usually DejaVu on Linux). Resolving the same stacks here keeps
a themed card as close to the design as the host allows without shipping font files, which
the repository cannot license. A host with none of the listed files falls back to Pillow's
bundled face, so a theme always renders; it only looks plainer.

Each role is a list of (file name, face index, variation axes) tried in order. A variable
font (Cascadia Code) gets its weight from the axes; when FreeType cannot set them, the next
static file in the list is used instead.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

from PIL import ImageFont

# (file name, face index in a collection, axis values or None)
ROLES = {
    # Display: the heavy headline weight (the design's 850).
    "display": [
        ("seguibl.ttf", 0, None),  # Segoe UI Black
        ("SF-Pro-Display-Black.otf", 0, None),
        ("Helvetica.ttc", 1, None),
        ("DejaVuSans-Bold.ttf", 0, None),
        ("LiberationSans-Bold.ttf", 0, None),
    ],
    # Bold: episode titles and names (the design's 800).
    "bold": [
        ("segoeuib.ttf", 0, None),
        ("SF-Pro-Display-Bold.otf", 0, None),
        ("Helvetica.ttc", 1, None),
        ("DejaVuSans-Bold.ttf", 0, None),
        ("LiberationSans-Bold.ttf", 0, None),
    ],
    "regular": [
        ("segoeui.ttf", 0, None),
        ("SF-Pro-Text-Regular.otf", 0, None),
        ("Helvetica.ttc", 0, None),
        ("DejaVuSans.ttf", 0, None),
        ("LiberationSans-Regular.ttf", 0, None),
    ],
    # The design's stack: ui-monospace, 'Cascadia Code', 'SF Mono', Menlo, Consolas, monospace.
    "mono": [
        ("CascadiaCode.ttf", 0, [400]),
        ("SFNSMono.ttf", 0, None),
        ("Menlo.ttc", 0, None),
        ("consola.ttf", 0, None),
        ("DejaVuSansMono.ttf", 0, None),
        ("LiberationMono-Regular.ttf", 0, None),
    ],
    "mono_bold": [
        ("CascadiaCode.ttf", 0, [600]),
        ("Menlo.ttc", 1, None),
        ("consolab.ttf", 0, None),
        ("DejaVuSansMono-Bold.ttf", 0, None),
        ("LiberationMono-Bold.ttf", 0, None),
    ],
}

FONT_SUFFIXES = {".ttf", ".otf", ".ttc"}


def font_dirs():
    """Where the host keeps fonts, most specific first."""
    home = Path.home()
    if sys.platform == "win32":
        windir = Path(os.environ.get("WINDIR", "C:/Windows"))
        dirs = [windir / "Fonts"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
        return dirs
    if sys.platform == "darwin":
        return [
            Path("/System/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
            Path("/Library/Fonts"),
            home / "Library" / "Fonts",
        ]
    return [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        home / ".local" / "share" / "fonts",
        home / ".fonts",
    ]


@functools.cache
def font_index():
    """Lower-case file name to path, for every font file under `font_dirs()` (first wins)."""
    found = {}
    for directory in font_dirs():
        if not directory.is_dir():
            continue
        try:
            paths = sorted(directory.rglob("*"))
        except OSError:
            continue
        for path in paths:
            if path.suffix.lower() in FONT_SUFFIXES:
                found.setdefault(path.name.lower(), path)
    return found


def _open(path, index, axes, size):
    face = ImageFont.truetype(str(path), size=size, index=index)
    if axes is not None:
        face.set_variation_by_axes(axes)
    return face


def font(role, size):
    """The first face of `role` the host has, at `size` pixels (at least 4)."""
    return _font(role, max(4, round(size)))


@functools.cache
def _font(role, size):
    index = font_index()
    for name, face_index, axes in ROLES[role]:
        path = index.get(name.lower())
        if path is None:
            continue
        try:
            return _open(path, face_index, axes, size)
        except (OSError, ValueError):
            # A collection without that face, or FreeType without variation support.
            continue
    return ImageFont.load_default(size=size)


def family(role):
    """The family name of the face `font(role, …)` resolves to, for libass styles; None when
    only Pillow's bundled face is available (libass then uses its own default)."""
    face = font(role, 32)
    getname = getattr(face, "getname", None)
    if getname is None:
        return None
    name = getname()[0]
    return name if name and name != "Aileron" else None
