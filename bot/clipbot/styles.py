"""Style packs: the sound and the joins of a reel, chosen by name (`clipbot reel --style NAME`).

A style is a directory with a `style.json` (assets/styles/<name>/, or any directory passed by
path) that names its music and its transition:

    {
      "name": "pipeline",
      "title": "Pipeline: funk organ theme",
      "description": "one or two sentences for `clipbot styles`",
      "music": { ... } | null,
      "transition": {"kind": "dip", "seconds": 0.4}
    }

Music comes in two kinds. A `bed` is one file that plays under the cards from a running
position, faded at the edges of each card run: what `--music FILE` always did.

    "music": {"kind": "bed", "file": "music/signal.mp3", "gain_db": -8, "fade_seconds": 1.0}

`cues` is music written to be cut: bar-long blocks that score.py assembles into one cue per
card run, so the music starts on a downbeat with each card and lands on an ending instead of
fading mid-phrase (score.py explains the rules).

    "music": {
      "kind": "cues", "bpm": 104, "beats_per_bar": 4, "gain_db": -4.5,
      "pickup": {"file": "music/pickup.flac", "beats": 2},
      "vamp": [{"file": "music/riff-1.flac", "resolved": "music/sting-1.flac"}, {"file": "music/riff-2.flac"}],
      "stings": ["music/riff-2.flac", "music/sting-1.flac"],
      "hold": {"file": "music/hold.flac", "seconds": 5.7},
      "outro": {"file": "music/outro.flac", "seconds": 6.08, "lead_bars": 1}
    }

Every block starts exactly on its first beat and carries its ring-out past its nominal length
(a vamp bar is one bar long, the pickup is `beats` long). `resolved` is the same bar turned
towards home, used when the held chord or the outro follows instead of the next vamp bar.
`stings` are the one-bar phrases a chapter card gets, in rotation; they default to the vamp
bars that already resolve. `hold.seconds` is how long the held chord sustains before it dies
on its own, `outro.seconds` the whole file, and `outro.lead_bars` the bars before its final
chord (dropped when the last cards are too short for the whole ending).

`transition` is the plan's `output.reel.transition`: cut, dip, dissolve, or `module` with the
path of a code-drawn transition file (docs/styles.md).

Paths are relative to the style directory. Wrong keys, types and missing files are errors
with the file named, because a style is written by hand or by an agent and a silent default
would be heard only after a ten-minute render.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .plan import REPO_ROOT, contract_path

STYLES_DIR = REPO_ROOT / "assets" / "styles"
MUSIC_KINDS = ("bed", "cues")
TRANSITIONS = ("cut", "dip", "dissolve", "module")
GAIN_RANGE = (-40.0, 0.0)  # contract: music.gain_db
FADE_RANGE = (0.0, 5.0)  # contract: music.fade_seconds
TRANSITION_RANGE = (0.1, 1.5)  # contract: transition.seconds


@dataclass(frozen=True)
class Style:
    name: str
    title: str
    description: str
    root: Path
    music: dict | None
    transition: dict | None

    def file(self, relative: str) -> Path:
        return (self.root / relative).resolve()

    @property
    def music_label(self) -> str:
        if not self.music:
            return "no music"
        if self.music["kind"] == "bed":
            return f"bed ({Path(self.music['file']).name})"
        return f"cues at {self.music['bpm']:g} BPM"

    @property
    def transition_label(self) -> str:
        if not self.transition:
            return "dip 0.4 s"
        kind = self.transition["kind"]
        name = Path(self.transition["module"]).stem if kind == "module" else kind
        return f"{name} {self.transition.get('seconds', 0.4):g} s"


def _number(value, where: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: expected a number")
    if not lo <= float(value) <= hi:
        raise ValueError(f"{where}: {value} is outside {lo:g}..{hi:g}")
    return float(value)


def _keys(data, where: str, allowed: tuple[str, ...], required: tuple[str, ...] = ()) -> None:
    if not isinstance(data, dict):
        raise ValueError(f"{where}: expected an object")
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}; known: {', '.join(allowed)}")
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"{where}: missing {', '.join(missing)}")


def _file(root: Path, value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: expected a file path")
    if not (root / value).is_file():
        raise ValueError(f"{where}: {value} not found in {root}")
    return value


def _music(data, root: Path, where: str) -> dict | None:
    if data is None:
        return None
    if not isinstance(data, dict) or data.get("kind") not in MUSIC_KINDS:
        raise ValueError(f"{where}.kind: expected one of {', '.join(MUSIC_KINDS)}")
    out: dict = {"kind": data["kind"]}
    if "gain_db" in data:
        out["gain_db"] = _number(data["gain_db"], f"{where}.gain_db", *GAIN_RANGE)
    if data["kind"] == "bed":
        _keys(data, where, ("kind", "file", "gain_db", "fade_seconds"), ("file",))
        out["file"] = _file(root, data["file"], f"{where}.file")
        if "fade_seconds" in data:
            out["fade_seconds"] = _number(data["fade_seconds"], f"{where}.fade_seconds", *FADE_RANGE)
        return out
    _keys(data, where, ("kind", "bpm", "beats_per_bar", "gain_db", "pickup", "vamp", "stings", "hold", "outro"),
          ("bpm", "vamp", "hold"))
    out["bpm"] = _number(data["bpm"], f"{where}.bpm", 30, 300)
    beats = data.get("beats_per_bar", 4)
    if isinstance(beats, bool) or not isinstance(beats, int) or not 1 <= beats <= 12:
        raise ValueError(f"{where}.beats_per_bar: expected a whole number from 1 to 12")
    out["beats_per_bar"] = beats
    if "pickup" in data:
        _keys(data["pickup"], f"{where}.pickup", ("file", "beats"), ("file", "beats"))
        out["pickup"] = {"file": _file(root, data["pickup"]["file"], f"{where}.pickup.file"),
                         "beats": _number(data["pickup"]["beats"], f"{where}.pickup.beats", 0.25, 16)}
    if not isinstance(data["vamp"], list) or not data["vamp"]:
        raise ValueError(f"{where}.vamp: expected a non-empty list of bars")
    out["vamp"] = []
    for i, bar in enumerate(data["vamp"]):
        _keys(bar, f"{where}.vamp[{i}]", ("file", "resolved"), ("file",))
        entry = {"file": _file(root, bar["file"], f"{where}.vamp[{i}].file")}
        if "resolved" in bar:
            entry["resolved"] = _file(root, bar["resolved"], f"{where}.vamp[{i}].resolved")
        out["vamp"].append(entry)
    stings = data.get("stings")
    if stings is None:
        stings = [bar.get("resolved", bar["file"]) for bar in out["vamp"]]
    if not isinstance(stings, list) or not stings:
        raise ValueError(f"{where}.stings: expected a non-empty list of files")
    out["stings"] = [_file(root, s, f"{where}.stings[{i}]") for i, s in enumerate(stings)]
    _keys(data["hold"], f"{where}.hold", ("file", "seconds"), ("file", "seconds"))
    out["hold"] = {"file": _file(root, data["hold"]["file"], f"{where}.hold.file"),
                   "seconds": _number(data["hold"]["seconds"], f"{where}.hold.seconds", 0.5, 60)}
    if "outro" in data:
        _keys(data["outro"], f"{where}.outro", ("file", "seconds", "lead_bars"), ("file", "seconds"))
        lead = data["outro"].get("lead_bars", 0)
        if isinstance(lead, bool) or not isinstance(lead, int) or lead < 0:
            raise ValueError(f"{where}.outro.lead_bars: expected a whole number of bars")
        out["outro"] = {"file": _file(root, data["outro"]["file"], f"{where}.outro.file"),
                        "seconds": _number(data["outro"]["seconds"], f"{where}.outro.seconds", 0.5, 120),
                        "lead_bars": lead}
    return out


def _transition(data, root: Path, where: str) -> dict | None:
    if data is None:
        return None
    _keys(data, where, ("kind", "seconds", "module"), ("kind",))
    if data["kind"] not in TRANSITIONS:
        raise ValueError(f"{where}.kind: {data['kind']!r} is not one of {', '.join(TRANSITIONS)}")
    out: dict = {"kind": data["kind"]}
    if "seconds" in data:
        out["seconds"] = _number(data["seconds"], f"{where}.seconds", *TRANSITION_RANGE)
    if data["kind"] == "module":
        if "module" not in data:
            raise ValueError(f"{where}: kind module needs \"module\": the transition's .py file")
        out["module"] = _file(root, data["module"], f"{where}.module")
    elif "module" in data:
        raise ValueError(f"{where}.module: only kind module takes a module")
    return out


def parse_style(data, root: Path, where: str) -> Style:
    _keys(data, where, ("name", "title", "description", "music", "transition"), ("name", "title"))
    for key in ("name", "title"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"{where}.{key}: expected non-empty text")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ValueError(f"{where}.description: expected text")
    return Style(
        name=data["name"].strip(), title=data["title"].strip(), description=" ".join(description.split()),
        root=root, music=_music(data.get("music"), root, f"{where}.music"),
        transition=_transition(data.get("transition"), root, f"{where}.transition"),
    )


def names(styles_dir: Path = STYLES_DIR) -> list[str]:
    if not styles_dir.is_dir():
        return []
    return sorted(p.parent.name for p in styles_dir.glob("*/style.json"))


def load(name_or_path: str, styles_dir: Path = STYLES_DIR) -> Style:
    """A shipped style by name, or any style directory (or its style.json) by path."""
    candidate = Path(name_or_path).expanduser()
    if candidate.is_file() and candidate.name == "style.json":
        path = candidate
    elif (candidate / "style.json").is_file():
        path = candidate / "style.json"
    else:
        path = styles_dir / name_or_path / "style.json"
        if not path.is_file():
            known = ", ".join(names(styles_dir)) or "none installed"
            raise ValueError(f"unknown style {name_or_path!r}; available: {known} (or pass a style directory)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: not valid JSON ({e})") from e
    return parse_style(data, path.resolve().parent, str(path))


def available(styles_dir: Path = STYLES_DIR) -> list[Style]:
    return [load(name, styles_dir) for name in names(styles_dir)]


def plan_transition(style: Style) -> dict | None:
    """The style's transition as `output.reel.transition`, the module path made a contract path."""
    if not style.transition:
        return None
    out = dict(style.transition)
    if "module" in out:
        out["module"] = contract_path(style.file(out["module"]))
    return out
