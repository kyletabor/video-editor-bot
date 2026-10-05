"""Storyboard: one web page that shows a reel scene by scene before anything is rendered.

Why: Kyle reviewed the Talk #3 reel by watching it (2026-10-05) and the loop was slow
and lossy: by the time the music jumped and the wrong clip opened the reel, an hour of
rendering and blurring was already spent. Professional edits are agreed on paper first
(an A/V script, a storyboard, an animatic) and only then cut. This module turns an edit
plan into that paper: every scene in playback order with its time on the reel, a
picture, who is talking and what they say, the text on screen, what the audio does,
and how it joins the next scene. Nothing is rendered; it reads the plan, the captions,
optional word timings and a few frames of the source.

Layout follows the fields storyboard tools agree on (Boords, StudioBinder, the A/V
two-column script): scene number, time in/out and length, a thumbnail, what is on
screen, dialogue with the speaker, on-screen text, the audio cue, the transition.
A strip at the top shows the whole reel to scale with the music under it, so a score
that restarts at every slide is visible before anyone hears it.
"""

from __future__ import annotations

import base64
import html
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .captions import Cue
from .words import Word

CARD_SECONDS = 3.0
THUMB_WIDTH = 480
QUOTE_WORDS = 70  # dialogue shown per scene: the opening words and the last few
QUOTE_TAIL = 18


@dataclass
class Scene:
    number: int
    kind: str  # "card" or "clip"
    role: str  # intro, opening, section, chapter, clip, closing, outro
    start: float  # on the reel
    seconds: float
    title: str = ""
    lines: list[str] = field(default_factory=list)
    image: str | None = None  # card picture (path) or None
    qr: str | None = None
    source: list[tuple[float, float]] = field(default_factory=list)  # clip: kept source spans
    speakers: list[str] = field(default_factory=list)
    quote: str = ""
    overlays: list[dict] = field(default_factory=list)
    layout: str = "full frame"
    music: str = ""
    music_on: bool = False
    transition: str = "cut"
    thumb: str | None = None  # data: URI

    @property
    def end(self) -> float:
        return self.start + self.seconds


def _clock(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:04.1f}"


def _hms(t: float) -> str:
    t = int(round(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def scenes_of(plan: dict) -> list[Scene]:
    """The reel's scenes in playback order, the renderer's timeline (cliprender reel.timeline):
    intro, opening cards, then per clip its `cards`, its chapter `card` and the clip,
    then closing cards and the outro. Transitions overlap neighbours; the strip ignores
    that (a fraction of a second) so the times stay readable."""
    reel = plan.get("output", {}).get("reel") or {}
    mode = reel.get("chapter_cards", "auto")
    out: list[Scene] = []

    def card(spec: dict, role: str) -> None:
        out.append(Scene(0, "card", role, 0.0, float(spec.get("seconds", CARD_SECONDS)), spec.get("title", ""),
                         list(spec.get("lines", [])), spec.get("image"), spec.get("qr")))

    if reel.get("intro") is not None:
        card(reel["intro"], "intro")
    for spec in reel.get("opening", []):
        card(spec, "opening")
    for clip in plan["clips"]:
        for spec in clip.get("cards", []):
            card(spec, "section")
        spec = clip.get("card")
        if spec is None and mode == "all":
            spec = {"title": clip["takeaway"]}
        if spec is not None and mode != "none":
            card(spec, "chapter")
        spans = [(float(s["start"]), float(s["end"])) for s in clip["segments"]]
        layout = clip.get("layout") or {}
        out.append(Scene(0, "clip", "clip", 0.0, sum(b - a for a, b in spans), clip.get("takeaway", ""),
                         source=spans, overlays=list(clip.get("overlays", [])),
                         layout="screen + speaker corner tile" if layout.get("kind") == "pip" else "full frame"))
    for spec in reel.get("closing", []):
        card(spec, "closing")
    if reel.get("outro") is not None:
        card(reel["outro"], "outro")
    at = 0.0
    transition = (reel.get("transition") or {}).get("kind", "cut")
    for k, s in enumerate(out, 1):
        s.number, s.start = k, at
        s.transition = transition if k < len(out) else "end"
        at += s.seconds
    describe_music(out, reel.get("music"))
    return out


def describe_music(scenes: list[Scene], music: dict | None) -> None:
    """What the audio does in each scene, in the words an editor would use.

    With `under: cards` (score.py) every run of consecutive slides gets its own cue:
    the first run opens the theme, the last plays its ending, and every run between is
    a short sting that starts and stops. Saying so per scene is the point: Talk #3's
    reel had a sting on every slide and the music "kept jumping"."""
    if not music:
        for s in scenes:
            s.music = "no music" if s.kind == "card" else "speech only"
        return
    under = music.get("under", "cards")
    runs: list[list[Scene]] = []
    for s in scenes:
        if s.kind == "card":
            if runs and runs[-1][-1].number == s.number - 1:
                runs[-1].append(s)
            else:
                runs.append([s])
    for k, run in enumerate(runs):
        # score.card_runs: "first" only when the reel opens on it, "last" only when the reel ends on it
        first, last = run[0] is scenes[0], run[-1] is scenes[-1]
        for i, s in enumerate(run):
            s.music_on = True
            if first:
                s.music = "music IN: theme opens" if i == 0 else "music continues"
            elif last:
                s.music = "music IN: theme returns" if i == 0 else "music continues"
                if s is run[-1]:
                    s.music += ", plays its ending"
            else:
                s.music = ("music STING: starts and stops on this slide (a longer slide gets a short vamp)"
                           if len(run) == 1 else "music STING starts" if i == 0 else "music continues, stops at the cut")
    for s in scenes:
        if s.kind == "clip":
            s.music_on = under == "all"
            s.music = "speech; music ducked under it" if under == "all" else "speech only, no music"


def attach_dialogue(scenes: list[Scene], cues: list[Cue], words: list[Word] | None = None) -> None:
    """Who talks in each clip (from speaker-labelled captions) and what they say (word
    timings when there are some, else the captions that overlap the kept spans)."""
    for s in scenes:
        if s.kind != "clip":
            continue
        talk: dict[str, float] = {}
        text: list[str] = []
        for a, b in s.source:
            for c in cues:
                overlap = min(b, c.end) - max(a, c.start)
                if overlap > 0 and c.speaker:
                    talk[c.speaker] = talk.get(c.speaker, 0.0) + overlap
            if words:
                text += [w.text for w in words if w.start < b and w.end > a]
            else:
                text += [c.text for c in cues if c.start < b and c.end > a]
        s.speakers = [n for n, _ in sorted(talk.items(), key=lambda kv: -kv[1])]
        tokens = " ".join(text).split()
        if len(tokens) > QUOTE_WORDS + QUOTE_TAIL:
            tokens = tokens[:QUOTE_WORDS] + ["…"] + tokens[-QUOTE_TAIL:]
        s.quote = " ".join(tokens)


def grab(source: str | Path, t: float, width: int = THUMB_WIDTH) -> str | None:
    """One frame as a small JPEG data URI (None when ffmpeg cannot read it)."""
    proc = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(source),
                           "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2pipe",
                           "-vcodec", "mjpeg", "-"], capture_output=True)
    if proc.returncode or not proc.stdout:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(proc.stdout).decode()


def image_uri(path: str | None) -> str | None:
    if not path or not Path(path).is_file():
        return None
    kind = "png" if path.lower().endswith(".png") else "jpeg"
    return f"data:image/{kind};base64," + base64.b64encode(Path(path).read_bytes()).decode()


NAME = re.compile(r"^[A-Z][a-z]+(?: [A-Z][a-zA-Z'-]+){1,2}$")  # "Jeff Weiner": a Meet tile's name label


def tile_name(source: str | Path, t: float, box: tuple[int, int, int, int] | None) -> str | None:
    """The name Meet prints on the active speaker's tile, read with OCR (Apple Vision).

    Why: speaker labels from meeting notes only cover part of a talk (Talk #3: 146 of
    1,077 captions), but the recording itself names whoever is talking, in the corner of
    their tile. `box` is the tile; without one, the bottom-left of the frame (gallery or
    full-frame speaker view). None when Vision is not available or nothing reads as a name."""
    from . import redact

    if not redact.vision_available():
        return None
    x, y, w, h = box or (0, 0, 0, 0)
    region = (x, y + h * 3 // 4, w // 2, h // 4) if box else None
    with tempfile.TemporaryDirectory(prefix="clipbot-sb-") as tmp:
        png = Path(tmp) / "tile.png"
        crop = f"crop={region[2]}:{region[3]}:{region[0]}:{region[1]}," if region else "crop=iw/3:ih/6:0:ih*5/6,"
        proc = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(source),
                               "-frames:v", "1", "-vf", crop + "scale=iw*3:ih*3:flags=lanczos", str(png)],
                              capture_output=True)
        if proc.returncode or not png.is_file():
            return None
        try:
            words = redact.vision_words(png, (0, 0, 1, 1))
        except Exception:  # noqa: BLE001 - a speaker label is a nicety, never a failure
            return None
    lines: dict = {}
    for w in words:
        lines.setdefault(w.line, []).append(w.text)
    names = [" ".join(ws) for ws in lines.values() if NAME.match(" ".join(ws))]
    return names[0] if names else None


def attach_thumbs(scenes: list[Scene], source: str | Path, layouts: dict[int, dict] | None = None) -> None:
    """A frame a third of the way into each clip (past the cut, before the end), and the
    speakers' names read off their tile at three points in the clip."""
    for s in scenes:
        if s.kind == "clip" and s.source:
            a, b = s.source[0]
            s.thumb = grab(source, a + (b - a) / 3)
            box = (layouts or {}).get(s.number, {}).get("speaker")
            seen = [tile_name(source, a + (b - a) * f, tuple(box) if box else None) for f in (0.2, 0.5, 0.8)]
            named = sorted({n for n in seen if n}, key=seen.index)
            if named:  # the recording's own label beats notes aligned to captions (those mislabel turns)
                s.speakers = named
        elif s.kind == "card":
            s.thumb = image_uri(s.image)


# ----------------------------------------------------------------- the page

CSS = """
:root{--bg:#0f1115;--panel:#171a21;--line:#2a2f3a;--ink:#e8eaed;--muted:#9aa3ae;--card:#3b6fb6;--clip:#3f8f5f;
--music:#c48a2c;--warn:#d9822b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted)}
.strip{margin:18px 0 6px}.lane{display:flex;height:22px;border-radius:6px;overflow:hidden;background:var(--panel)}
.lane div{border-right:1px solid var(--bg);min-width:2px}.lane .c{background:var(--card)}.lane .v{background:var(--clip)}
.lane.m div{background:transparent}.lane.m .on{background:var(--music)}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:13px;margin:6px 0 18px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.scene{display:grid;grid-template-columns:240px 1fr;gap:14px;background:var(--panel);border:1px solid var(--line);
border-radius:12px;padding:12px;margin:10px 0}
.scene img{width:100%;border-radius:8px;display:block}.slide{aspect-ratio:16/9;border-radius:8px;background:#12151b;
border:1px solid var(--line);padding:10px;display:flex;flex-direction:column;justify-content:center;overflow:hidden}
.slide b{font-size:14px}.slide span{color:var(--muted);font-size:11px}
.head{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}.num{font-weight:700}.time{color:var(--muted);font-variant-numeric:tabular-nums}
.chip{font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.chip.card{border-color:var(--card);color:#9cc0f0}.chip.clip{border-color:var(--clip);color:#9fd8b4}
.row{margin-top:6px}.k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em;margin-right:6px}
.quote{border-left:3px solid var(--clip);padding-left:10px;margin-top:6px}.music{color:#e6c07b}.music.off{color:var(--muted)}
.title{font-weight:600;margin-top:4px}
.notes{background:#1d1a12;border:1px solid #5a4a22;border-radius:12px;padding:10px 14px;margin:14px 0}.notes ul{margin:6px 0 0;padding-left:18px}
@media(max-width:700px){.scene{grid-template-columns:1fr}}
"""


def _slide(s: Scene) -> str:
    lines = "".join(f"<span>{html.escape(ln)}</span>" for ln in s.lines)
    extra = f"<span>QR code: {html.escape(s.qr)}</span>" if s.qr else ""
    if s.thumb:
        return f'<img src="{s.thumb}" alt="slide picture">'
    return f'<div class="slide"><b>{html.escape(s.title)}</b>{lines}{extra}</div>'


def render_html(scenes: list[Scene], *, title: str, source_name: str, notes: list[str] = ()) -> str:
    total = sum(s.seconds for s in scenes) or 1.0
    lane = "".join(f'<div class="{"c" if s.kind == "card" else "v"}" style="width:{100 * s.seconds / total:.3f}%" '
                   f'title="#{s.number} {html.escape(s.title)}"></div>' for s in scenes)
    music = "".join(f'<div class="{"on" if s.music_on else ""}" style="width:{100 * s.seconds / total:.3f}%"></div>'
                    for s in scenes)
    cards = sum(1 for s in scenes if s.kind == "card")
    rows = []
    for s in scenes:
        pic = _slide(s) if s.kind == "card" else (f'<img src="{s.thumb}" alt="frame">' if s.thumb else "")
        parts = [f'<div class="head"><span class="num">#{s.number}</span>'
                 f'<span class="time">{_clock(s.start)}–{_clock(s.end)} · {s.seconds:.1f}s</span>'
                 f'<span class="chip {s.kind}">{"Slide" if s.kind == "card" else "Clip"}</span>'
                 f'<span class="chip">{html.escape(s.role)}</span></div>']
        if s.kind == "card":
            parts.append(f'<div class="title">{html.escape(s.title)}</div>')
            parts += [f'<div class="row">{html.escape(ln)}</div>' for ln in s.lines]
            if s.qr:
                parts.append(f'<div class="row"><span class="k">QR</span>{html.escape(s.qr)}</div>')
        else:
            src = ", ".join(f"{_hms(a)}–{_hms(b)}" for a, b in s.source)
            who = ", ".join(s.speakers) or "speaker unknown"
            parts.append(f'<div class="title">{html.escape(s.title)}</div>')
            parts.append(f'<div class="row"><span class="k">Who</span>{html.escape(who)}'
                         f' <span class="k" style="margin-left:10px">Frame</span>{html.escape(s.layout)}</div>')
            if s.quote:
                parts.append(f'<div class="quote">“{html.escape(s.quote)}”</div>')
            for o in s.overlays:
                parts.append(f'<div class="row"><span class="k">On screen</span>{html.escape(o["text"])}'
                             f' <span class="time">({_hms(o["start"])}–{_hms(o["end"])})</span></div>')
            parts.append(f'<div class="row"><span class="k">Source</span>{html.escape(source_name)} {src}</div>')
        parts.append(f'<div class="row music{"" if s.music_on else " off"}"><span class="k">Audio</span>'
                     f'{html.escape(s.music)}</div>')
        parts.append(f'<div class="row"><span class="k">Then</span>{html.escape(s.transition)}</div>')
        rows.append(f'<section class="scene" id="scene-{s.number}"><div>{pic}</div><div>{"".join(parts)}</div></section>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} storyboard</title>
<style>{CSS}</style></head><body><main>
<h1>{html.escape(title)}</h1>
<div class="sub">Storyboard · {len(scenes)} scenes ({cards} slides, {len(scenes) - cards} clips) · {_clock(total)} · nothing rendered yet</div>
{"<div class='notes'><b>Decisions for review</b><ul>" + "".join(f"<li>{html.escape(n)}</li>" for n in notes) + "</ul></div>" if notes else ""}
<div class="strip"><div class="lane">{lane}</div><div class="lane m" style="height:8px;margin-top:4px">{music}</div></div>
<div class="legend"><span><i style="background:var(--card)"></i>slide</span><span><i style="background:var(--clip)"></i>clip</span>
<span><i style="background:var(--music)"></i>music playing</span></div>
{"".join(rows)}
</main></body></html>"""


def build(plan: dict, cues: list[Cue], *, words: list[Word] | None = None, source: str | Path | None = None,
          title: str | None = None, notes: list[str] = ()) -> str:
    scenes = scenes_of(plan)
    attach_dialogue(scenes, cues, words)
    if source:
        clips = iter(plan["clips"])
        layouts = {s.number: (next(clips).get("layout") or {}) for s in scenes if s.kind == "clip"}
        attach_thumbs(scenes, source, layouts)
    reel = plan.get("output", {}).get("reel") or {}
    name = title or (reel.get("intro") or {}).get("title") or "Reel"
    return render_html(scenes, title=name, source_name=Path(plan["source"]["path"]).name, notes=notes)
