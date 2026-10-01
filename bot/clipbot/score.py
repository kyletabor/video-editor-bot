"""Score a reel: one music cue per card run, assembled from a style's blocks.

Kyle on the theme (2026-10-01): "Make sure the music comes in only at transitions ... comes
in and out appropriately like it was professionally produced ... finish the clip with an
outro slide and music. The outro of the song you made was great to use."

A bed that fades in wherever the cards happen to be cannot do that: it enters mid-phrase and
leaves mid-phrase. So a `cues` style (styles.py) ships its music as blocks cut on the beat,
and this module writes a cue for every card run of a plan:

    first run   (intro + opening + first chapter card)
                pickup, then as many vamp bars as fit, then a held chord that fades as the
                first clip arrives. The last bar is its `resolved` variant, so the chord it
                lands on is home.
    chapter card  one sting (the stings rotate so ten cards are not ten copies), then the
                held chord for what is left of the card. Longer cards get vamp bars.
    last run    (closing cards + outro) back-timed: the outro block is placed so the tune's
                own ending finishes END_MARGIN before the reel does, vamp bars fill the time
                before it, and the pickup leads in. The music ends; it is not faded out.

The renderer needs no change for this. With `music.under: cards` it already takes one piece
per card run from a running offset into the file (render/cliprender/reel.py, `music_pieces`),
so a file that is the cues laid end to end, each exactly as long as its run, plays cue k
under run k. `card_runs` here mirrors the renderer's arithmetic (whole frames per card, a
dissolve's overlap) so the offsets agree to the sample.

Everything up to `cue_graph` is pure arithmetic on Fractions; ffmpeg only mixes.
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .plan import REPO_ROOT, contract_path
from .styles import Style

RATE = 48000
MIN_CUE = Fraction(1, 2)  # a run shorter than this stays silent
MIN_HOLD = Fraction(3, 5)  # the held chord needs at least this long to read as an ending
HOLD_FADE = Fraction(1, 2)  # ...and fades over this much of its end
END_MARGIN = Fraction(1, 5)  # the outro is finished this long before the reel is
EDGE = Fraction(1, 25)  # a 40 ms fade wherever a ring-out is cut at the end of a run
# The plan's music.fade_seconds. A cue starts on a drum hit and carries its own fades (the held
# chord's, and EDGE at the end of every run), so the renderer adds none: even 50 ms of fade-in
# takes the attack off the downbeat.
CUE_FADE_SECONDS = 0.0
PAD = Fraction(1)  # silence after the last cue, so the renderer never runs out of bed
OVERLAPPING = ("dissolve", "module")  # transitions whose bridge takes frames from both sides


@dataclass(frozen=True)
class Run:
    """One maximal run of consecutive cards: where it starts on the reel and how long it is."""

    start: Fraction
    seconds: Fraction
    position: str  # "first", "middle" or "last"


@dataclass(frozen=True)
class Placement:
    """One block in a cue: `at` seconds into the cue, optionally trimmed and faded."""

    file: Path
    at: Fraction
    skip: Fraction = Fraction(0)  # seconds dropped from the start of the block
    seconds: Fraction | None = None  # how long it plays; None = to the end of the block
    fade_in: Fraction = Fraction(0)
    fade_out: Fraction = Fraction(0)


@dataclass(frozen=True)
class Blocks:
    """A cues style's music, resolved to files and exact lengths."""

    bar: Fraction
    vamp: tuple[tuple[Path, Path | None], ...]  # (bar, its resolved variant)
    stings: tuple[Path, ...]
    hold: Path
    hold_seconds: Fraction
    pickup: Path | None = None
    pickup_seconds: Fraction = Fraction(0)
    outro: Path | None = None
    outro_seconds: Fraction = Fraction(0)
    outro_lead: Fraction = Fraction(0)  # the outro's bars before its final chord


def blocks_of(style: Style) -> Blocks:
    music = style.music
    if not music or music["kind"] != "cues":
        raise ValueError(f"style {style.name}: no cue music to score with")
    beat = Fraction(60) / Fraction(str(music["bpm"]))
    bar = beat * music["beats_per_bar"]
    pickup, outro = music.get("pickup"), music.get("outro")
    if outro and bar * outro["lead_bars"] >= Fraction(str(outro["seconds"])):
        raise ValueError(f"style {style.name}: music.outro.lead_bars ({outro['lead_bars']}) is the whole "
                         f"{outro['seconds']:g} s outro; nothing would be left for the final chord")
    return Blocks(
        bar=bar,
        vamp=tuple((style.file(b["file"]), style.file(b["resolved"]) if "resolved" in b else None) for b in music["vamp"]),
        stings=tuple(style.file(s) for s in music["stings"]),
        hold=style.file(music["hold"]["file"]),
        hold_seconds=Fraction(str(music["hold"]["seconds"])),
        pickup=style.file(pickup["file"]) if pickup else None,
        pickup_seconds=beat * Fraction(str(pickup["beats"])) if pickup else Fraction(0),
        outro=style.file(outro["file"]) if outro else None,
        outro_seconds=Fraction(str(outro["seconds"])) if outro else Fraction(0),
        outro_lead=bar * outro["lead_bars"] if outro else Fraction(0),
    )


def verify_blocks(style: Style) -> None:
    """The lengths style.json states against the files: a wrong `outro.seconds` back-times the
    ending to the wrong place and cuts it off mid-ring, and nothing else would notice."""
    music = style.music
    for key in ("hold", "outro"):
        block = music.get(key)
        if not block:
            continue
        actual = float(_ffprobe(style.file(block["file"]), "format=duration"))
        stated = float(block["seconds"])
        if (key == "outro" and abs(actual - stated) > 0.02) or (key == "hold" and stated > actual + 0.02):
            limit = "is" if key == "outro" else "is only"
            raise ValueError(f"style {style.name}: music.{key}.seconds says {stated:g} s but {block['file']} "
                             f"{limit} {actual:.3f} s")


# --- where the cards are (mirrors render/cliprender/reel.py; pinned by contract/fixtures/card-runs.json) ---

def _ffprobe(path: str | Path, entries: str, *select: str) -> str:
    try:
        return subprocess.run(["ffprobe", "-v", "error", *select, "-show_entries", entries, "-of", "csv=p=0", str(path)],
                              check=True, capture_output=True, text=True).stdout.strip()
    except FileNotFoundError as e:
        raise RuntimeError("ffprobe not found on PATH") from e
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()[-1:] or ["no message"]
        raise RuntimeError(f"ffprobe could not read {path}: {tail[0]}") from e


def source_fps(path: str | Path) -> Fraction:
    """The reel's frame rate as the renderer picks it: the source's nominal rate, or 24 when
    that is implausible. A source that cannot be read is an error, not 24: cues cut on the
    wrong frame grid drift a little further off their cards with every run."""
    if not Path(path).is_file():
        raise RuntimeError(f"cannot score the music: the plan's source {path} is not there to take the frame rate from")
    text = _ffprobe(path, "stream=r_frame_rate", "-select_streams", "v:0")
    try:
        rate = Fraction(text.splitlines()[0].strip().rstrip(","))
    except (ValueError, ZeroDivisionError, IndexError):
        return Fraction(24)
    return rate if 1 <= rate <= 120 else Fraction(24)


def frame_count(seconds, fps: Fraction) -> int:
    return max(1, round(Fraction(str(seconds)) * fps))


def bridge_frames(transition: dict, fps: Fraction, frames: list[int]) -> int:
    """Frames a join takes from both neighbours: the renderer's `transition_frames`."""
    if transition.get("kind", "cut") not in OVERLAPPING or len(frames) < 2:
        return 0
    count = round(Fraction(str(transition.get("seconds", 0.4))) * fps)
    limit = min([frames[0] - 1, frames[-1] - 1, *((n - 1) // 2 for n in frames[1:-1])])
    return max(0, min(count, limit))


def card_runs(plan: dict, fps: Fraction) -> list[Run]:
    """Every run of consecutive cards in the plan's reel, in playback order."""
    reel = plan["output"]["reel"]
    mode = reel.get("chapter_cards", "auto")
    items: list[tuple[bool, int]] = []  # (is a card, frames)

    def card(spec: dict) -> None:
        items.append((True, frame_count(spec.get("seconds", 3), fps)))

    if "intro" in reel:
        card(reel["intro"])
    for spec in reel.get("opening", []):
        card(spec)
    for clip in plan["clips"]:
        spec = clip.get("card")
        if spec is None and mode == "all":
            spec = {}
        if spec is not None and mode != "none":
            card(spec)
        kept = sum(Fraction(str(s["end"])) - Fraction(str(s["start"])) for s in clip["segments"])
        items.append((False, frame_count(kept, fps)))
    for spec in reel.get("closing", []):
        card(spec)
    if "outro" in reel:
        card(reel["outro"])

    frames = [n for _, n in items]
    overlap = Fraction(bridge_frames(reel.get("transition") or {}, fps, frames)) / fps
    spans: list[list] = []  # [start, end, first index, last index]
    at = Fraction(0)
    for index, (is_card, count) in enumerate(items):
        seconds = Fraction(count) / fps
        if is_card:
            if spans and spans[-1][3] == index - 1:
                spans[-1][1], spans[-1][3] = at + seconds, index
            else:
                spans.append([at, at + seconds, index, index])
        at += seconds - overlap
    last = len(items) - 1
    return [
        Run(start, end - start, "first" if a == 0 else "last" if b == last else "middle")
        for start, end, a, b in spans
    ]


# --- what plays under each run ---------------------------------------------------------------------

def _bars(blocks: Blocks, count: int) -> list[Path]:
    """`count` vamp bars from the top; the last one resolved, because home follows it."""
    files = []
    for i in range(count):
        bar, resolved = blocks.vamp[i % len(blocks.vamp)]
        files.append(resolved if i == count - 1 and resolved else bar)
    return files


def _hold(blocks: Blocks, at: Fraction, until: Fraction) -> list[Placement]:
    length = min(until - at, blocks.hold_seconds)
    if length < Fraction(1, 5):
        return []
    return [Placement(blocks.hold, at, seconds=length, fade_out=min(HOLD_FADE, length * Fraction(7, 10)))]


def _lead_in(blocks: Blocks, downbeat: Fraction) -> list[Placement]:
    """The pickup into a downbeat `downbeat` seconds into the cue: whole when there is room,
    its end when there is not, nothing when less than 0.3 s is left."""
    if not blocks.pickup:
        return []
    if downbeat >= blocks.pickup_seconds:
        return [Placement(blocks.pickup, downbeat - blocks.pickup_seconds)]
    if downbeat >= Fraction(3, 10):
        return [Placement(blocks.pickup, Fraction(0), skip=blocks.pickup_seconds - downbeat, fade_in=Fraction(3, 100))]
    return []


def _ending(seconds: Fraction, blocks: Blocks) -> list[Placement]:
    """The last run, back-timed so the outro block ends END_MARGIN before the reel."""
    end = seconds - END_MARGIN
    start = end - blocks.outro_seconds
    if start < 0:  # too short for the whole ending: the pickup into its final chord, or just the held chord
        chord = blocks.outro_seconds - blocks.outro_lead
        if blocks.outro_lead > 0 and end - chord >= 0:
            # Cutting into the block mid-file lands in the ring-out of the bars it skips: 5 ms of
            # fade hides that step and still leaves the hit its attack.
            return _lead_in(blocks, end - chord) + [
                Placement(blocks.outro, end - chord, skip=blocks.outro_lead, fade_in=Fraction(1, 200))]
        return _hold(blocks, Fraction(0), seconds)
    count = math.floor(start / blocks.bar)
    first = start - count * blocks.bar
    placements = _lead_in(blocks, first)
    placements += [Placement(f, first + k * blocks.bar) for k, f in enumerate(_bars(blocks, count))]
    placements.append(Placement(blocks.outro, start))
    return placements


def plan_cue(seconds, position: str, blocks: Blocks, sting: int = 0) -> list[Placement]:
    """The blocks for one run of `seconds`. `sting` picks the phrase of a one-bar chapter cue."""
    seconds = Fraction(seconds)
    if seconds < MIN_CUE:
        return []
    if position == "last" and blocks.outro:
        return _ending(seconds, blocks)
    lead = blocks.pickup_seconds if position == "first" and blocks.pickup else Fraction(0)
    count = math.floor((seconds - lead - MIN_HOLD) / blocks.bar)
    if count < 1 and lead:  # no room for pickup and a bar: the bar matters more
        lead = Fraction(0)
        count = math.floor((seconds - MIN_HOLD) / blocks.bar)
    if count < 1:
        return _hold(blocks, Fraction(0), seconds)
    placements = [Placement(blocks.pickup, Fraction(0))] if lead else []
    if position == "middle" and count == 1:
        files = [blocks.stings[sting % len(blocks.stings)]]
    else:
        files = _bars(blocks, count)
    placements += [Placement(f, lead + k * blocks.bar) for k, f in enumerate(files)]
    return placements + _hold(blocks, lead + count * blocks.bar, seconds)


def score(plan: dict, style: Style, fps: Fraction) -> list[tuple[Run, list[Placement]]]:
    """Every card run of the plan with its cue; the chapter stings rotate through the reel."""
    blocks = blocks_of(style)
    scored, sting = [], 0
    for run in card_runs(plan, fps):
        cue = plan_cue(run.seconds, run.position, blocks, sting)
        if run.position == "middle":
            sting += 1
        scored.append((run, cue))
    return scored


# --- mixing ----------------------------------------------------------------------------------------

def _samples(seconds: Fraction, rate: int) -> int:
    return round(seconds * rate)


def cue_graph(scored: list[tuple[Run, list[Placement]]], rate: int = RATE) -> tuple[list[Path], str, int] | None:
    """(input files, filter graph ending in [out], total samples), or None when nothing plays.

    One input per placement. Each run is mixed on its own, cut to exactly the run's length
    (a ring-out that outlives its run would otherwise be heard under the next card run), then
    delayed to its offset in the file: the sum of the runs before it, which is where the
    renderer will look for it.
    """
    inputs: list[Path] = []
    chains: list[str] = []
    run_labels: list[str] = []
    offset = Fraction(0)
    for k, (run, cue) in enumerate(scored):
        first, last = _samples(offset, rate), _samples(offset + run.seconds, rate)
        offset += run.seconds
        labels = []
        for p in cue:
            j = len(inputs)
            inputs.append(p.file)
            chain = f"[{j}:a]aresample={rate},aformat=sample_fmts=fltp:channel_layouts=stereo"
            if p.skip or p.seconds is not None:
                trim = f"start_sample={_samples(p.skip, rate)}"
                if p.seconds is not None:
                    trim += f":end_sample={_samples(p.skip + p.seconds, rate)}"
                chain += f",atrim={trim},asetpts=PTS-STARTPTS"
            if p.fade_in:
                chain += f",afade=t=in:st=0:d={float(p.fade_in):.6f}"
            if p.fade_out and p.seconds is not None:
                chain += f",afade=t=out:st={float(p.seconds - p.fade_out):.6f}:d={float(p.fade_out):.6f}"
            delay = _samples(p.at, rate)
            if delay:
                chain += f",adelay={delay}S|{delay}S"
            labels.append(f"[p{j}]")
            chains.append(chain + labels[-1])
        if not labels:
            continue
        length = last - first
        mix = "".join(labels) + (f"amix=inputs={len(labels)}:normalize=0:duration=longest," if len(labels) > 1 else "anull,")
        edge = min(float(EDGE), float(run.seconds) / 4)
        mix += (f"apad,atrim=end_sample={length},"
                f"afade=t=out:st={float(run.seconds) - edge:.6f}:d={edge:.6f}")
        if first:
            mix += f",adelay={first}S|{first}S"
        run_labels.append(f"[r{k}]")
        chains.append(mix + run_labels[-1])
    if not run_labels:
        return None
    total = _samples(offset + PAD, rate)
    tail = f"apad,atrim=end_sample={total}[out]"
    if len(run_labels) > 1:
        chains.append("".join(run_labels) + f"amix=inputs={len(run_labels)}:normalize=0:duration=longest," + tail)
    else:
        chains.append(run_labels[0] + tail)
    return inputs, ";".join(chains), total


def render_cues(scored: list[tuple[Run, list[Placement]]], out_path: Path, rate: int = RATE) -> int:
    """Write the cue file (16-bit stereo WAV). Returns its length in samples; 0 = nothing to play."""
    built = cue_graph(scored, rate)
    if built is None:
        return 0
    inputs, graph, total = built
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y"]
    for path in inputs:
        cmd += ["-i", str(path)]
    cmd += ["-filter_complex", graph, "-map", "[out]", "-ar", str(rate), "-ac", "2", "-c:a", "pcm_s16le", str(out_path)]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise RuntimeError("ffmpeg not found on PATH") from e
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()[-1:] or ["no message"]
        raise RuntimeError(f"ffmpeg could not mix the music cues: {tail[0]}") from e
    return total


def describe(scored: list[tuple[Run, list[Placement]]]) -> str:
    cues = [cue for _, cue in scored if cue]
    seconds = float(sum((run.seconds for run, cue in scored if cue), Fraction(0)))
    return f"{len(cues)} cue{'s' if len(cues) != 1 else ''} over {seconds:.1f} s of cards"


def style_music(plan: dict, style: Style, out_dir: Path, *, gain_db: float | None = None,
                fade_seconds: float | None = None) -> tuple[dict | None, str]:
    """`output.reel.music` for this plan in this style, and a line for the terminal.

    A bed style points the plan at the bed. A cues style writes `music-cues.wav` next to the
    plan and points at that; the plan's cards must be final first, because the file is cut to
    them (re-run `clipbot score` after editing card seconds or the transition by hand).
    """
    music = style.music
    if not music:
        return None, f"style {style.name}: no music"
    level = gain_db if gain_db is not None else music.get("gain_db")
    if music["kind"] == "bed":
        out: dict = {"path": contract_path(style.file(music["file"]))}
        if level is not None:
            out["gain_db"] = level
        fade = fade_seconds if fade_seconds is not None else music.get("fade_seconds")
        if fade is not None:
            out["fade_seconds"] = fade
        return out, f"style {style.name}: bed {Path(music['file']).name} under the cards"
    verify_blocks(style)
    source = Path(plan["source"]["path"])
    fps = source_fps(source if source.is_absolute() else REPO_ROOT / source)
    scored = score(plan, style, fps)
    path = Path(out_dir) / "music-cues.wav"
    if not render_cues(scored, path):
        return None, f"style {style.name}: the cards are too short for any music"
    out = {"path": contract_path(path), "under": "cards", "fade_seconds": CUE_FADE_SECONDS, "loop": False}
    if level is not None:
        out["gain_db"] = level
    return out, f"style {style.name}: {describe(scored)} -> {path}"
