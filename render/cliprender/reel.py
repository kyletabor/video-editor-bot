"""Assemble the contract reel: [intro] + opening + Σ([card] + clip) + closing + [outro].

The reel is built in bounded steps, never as one filter graph over every input. Why: FFmpeg
opens a decoder for every input of a graph and (from 7.0, with its threaded pipeline) reads
and decodes ahead on all of them, so a graph over 25 segments of a 1080p reel held about
2.2 GB of frames and was killed by the kernel on an 8-core ARM box with 2.7 GB free, while the
same plan at 720p fitted. The memory grew with the number of pieces, not with what any one
step needed. So every piece (card or clip) is conformed on its own into a segment of exactly
so many frames and samples, with the same encoder settings for all (`piece_encoder_flags`),
the effects that a single graph applied at once are applied per piece or per join (dip fades
inside a piece, a dissolve as a short bridge cross-faded from the two neighbours' ends, the
music stretch of a card run mixed into the segments it overlaps), the audio is joined sample
by sample in the renderer, and the video is joined by the concat demuxer with the stream
copied. Peak memory is that of one piece; the wall time is about the same, since the same
frames are decoded and encoded once.

Contract v1.2 (opening/closing cards, a music bed, transitions, audio fades) uses the same
pieces: a plan without any v1.2 field conforms its pieces with no v1.2 filter (no fades, no
transition, no music), so the v1.1 reel is the plain join, and every v1.2 effect is a filter
that behaves the same on FFmpeg 4.4 and 7 (`fade`, `afade`, `xfade`, `atrim`, `adelay`,
`amix`, `tpad`, `apad`), which keeps the ARM Pi with the Ubuntu 4.4.2 build a first-class
renderer. The mix is kept from clipping by arithmetic (`headroom_gain`) rather than a
limiter, because FFmpeg 4.4's `alimiter` delays audio by its look-ahead and drops the tail.

A module transition (contract v1.3) is a dissolve in everything but the picture of its
bridges: the same overlap, the same audio cross-fade, the same raw ends split off each piece,
with the frames drawn by the plan's Python module (`transitions.encode_module_bridge`)
instead of `xfade`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .cards import Card, chapter_footer, write_card_png
from .media import RenderError, warn
from .transitions import encode_module_bridge, load_transition

MAX_RECOMMENDED_SECONDS = 600
# Card segments and clips are each exact to well under a frame; junction slop comes from AAC
# frame padding and the constant-frame-rate conformance, so the budget is per segment.
TOLERANCE_PER_SEGMENT = 0.2
# Any of these in `output.reel` switches the reel from the v1.1 graph to the v1.2 one; the
# schema defaults then apply to the fields that are absent (transition cut, 0.15 s audio fades).
V12_FIELDS = ("opening", "closing", "music", "transition", "audio_fade_seconds")
# Transitions in which the two neighbours of a join play at once: each consumes its length
# from both sides, is rendered as a bridge, and lets speech sound over a card's music.
OVERLAPPING = ("dissolve", "module")
# The mix is scaled so that the worst-case speech-plus-music peak stays this far below full
# scale. AAC ringing adds a few tenths of a dB on decode and still cannot reach 0 dBFS.
HEADROOM_DB = 1.0
# Verification slack: AAC may overshoot a source that already peaks near full scale by this
# much; music never adds more than the arithmetic bound above, so anything beyond it is a bug.
PEAK_SLACK_DB = 0.5


def db_to_linear(db):
    return 10 ** (db / 20)


def linear_to_db(value):
    return 20 * math.log10(value) if value > 0 else -math.inf


@dataclass(frozen=True)
class Music:
    """`output.reel.music` with its path resolved and the contract defaults filled in."""

    path: Path
    under: str = "cards"
    gain_db: float = -18.0
    duck_db: float = -30.0
    fade_seconds: Fraction = Fraction(1)
    loop: bool = True

    @property
    def gain(self):
        """Linear level of the bed when it is the only sound (under cards)."""
        return db_to_linear(self.gain_db)

    @property
    def duck(self):
        """Linear level of the bed under speech (`under: all` only)."""
        return db_to_linear(self.duck_db)


@dataclass(frozen=True)
class Style:
    """The v1.2 options of a reel. `reel_style` returns None for a v1.1 plan."""

    transition: str = "cut"
    transition_seconds: Fraction = Fraction("0.4")
    audio_fade: Fraction = Fraction("0.15")
    music: Music | None = None
    # The Python file that draws the bridges when `transition` is "module".
    module: Path | None = None


def reel_style(reel, resolve):
    """Resolve the v1.2 fields of `output.reel`, or return None when the plan uses none.

    A v1.1 plan must keep rendering exactly as before, so the presence of any v1.2 field is
    the switch, and only then do the schema defaults (cut, 0.15 s audio fades) apply.
    `resolve` turns a plan path into an absolute one under the renderer's root rule.
    """
    if not any(field in reel for field in V12_FIELDS):
        return None
    music = None
    if "music" in reel:
        spec = reel["music"]
        music = Music(
            resolve(spec["path"]),
            spec.get("under", "cards"),
            float(spec.get("gain_db", -18)),
            float(spec.get("duck_db", -30)),
            Fraction(str(spec.get("fade_seconds", 1.0))),
            bool(spec.get("loop", True)),
        )
    transition = reel.get("transition", {})
    return Style(
        transition.get("kind", "cut"),
        Fraction(str(transition.get("seconds", 0.4))),
        Fraction(str(reel.get("audio_fade_seconds", 0.15))),
        music,
        resolve(transition["module"]) if transition.get("kind") == "module" else None,
    )


def reel_fps(video):
    """Constant output frame rate: the source's nominal rate, or 24 when it is implausible."""
    try:
        rate = Fraction(video.get("r_frame_rate", "24/1"))
    except (ValueError, ZeroDivisionError):
        rate = Fraction(24)
    return rate if 1 <= rate <= 120 else Fraction(24)


def fps_text(fps):
    return f"{fps.numerator}/{fps.denominator}"


def timeline(plan, reel):
    """Cards and clip ids in playback order, honoring `chapter_cards`.

    `auto` shows a card only where the plan author wrote one, `all` synthesizes a card from
    the takeaway for clips without one, and `none` drops every chapter card while keeping the
    intro and outro. A chapter footer names the clip's position in the reel and where its
    first segment starts in the source, so a viewer can find the moment in the recording.
    Opening and closing cards (v1.2) carry no footer: they are not chapters, so a "k of N"
    counter would miscount the reel.
    """
    mode = reel.get("chapter_cards", "auto")
    clips = plan["clips"]
    items = []
    if "intro" in reel:
        items.append(Card.from_plan(reel["intro"]))
    items.extend(Card.from_plan(card) for card in reel.get("opening", []))
    for index, clip in enumerate(clips, 1):
        card = clip.get("card")
        if card is None and mode == "all":
            card = {"title": clip["takeaway"]}
        if card is not None and mode != "none":
            footer = chapter_footer(index, len(clips), clip["segments"][0]["start"])
            items.append(Card.from_plan(card, footer))
        items.append(clip["id"])
    items.extend(Card.from_plan(card) for card in reel.get("closing", []))
    if "outro" in reel:
        items.append(Card.from_plan(reel["outro"]))
    return items


def planned_seconds(items, plan):
    """Length the plan implies before rendering, for the contract's ten-minute warning."""
    kept = {
        clip["id"]: sum(
            Fraction(str(s["end"])) - Fraction(str(s["start"])) for s in clip["segments"]
        )
        for clip in plan["clips"]
    }
    return sum(
        (item.seconds if isinstance(item, Card) else kept[item] for item in items), Fraction(0)
    )


def video_chain(index, dimensions, fps, matrix=None):
    """Conform one input's video to the reel: size, square pixels, constant rate, yuv420p.

    `start_time=0` pads a clip whose first frame sits after its audio start with a copy of
    that frame, so no junction opens a hole; the size filters are a safety net since cards
    are drawn at the reel size and clips were rendered at it. `matrix` names the YUV matrix
    for an RGB source (a card): the conversion then happens in this first `scale`, with that
    matrix, instead of wherever FFmpeg would insert one with its default.
    """
    width, height = dimensions
    convert = f"scale=out_color_matrix={matrix}:out_range=tv,format=yuv420p," if matrix else ""
    return (
        f"[{index}:v:0]{convert}scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
        f"fps={fps_text(fps)}:start_time=0,format=yuv420p"
    )


def audio_chain(index, audio):
    """Conform one input's audio to the reel's sample rate and channel layout, starting at 0."""
    layout = "stereo" if int(audio["channels"]) == 2 else "mono"
    return (
        f"[{index}:a:0]aresample={int(audio['sample_rate'])},"
        f"aformat=sample_fmts=fltp:channel_layouts={layout},asetpts=PTS-STARTPTS"
    )


def effective_transition(style, durations):
    """Transition length the segments can afford, or 0 when there is nothing to join.

    A dip needs each segment to hold its two half-fades (d >= s). A dissolve (and a module
    transition, which is timed like one) consumes s from both sides of a join, so a segment
    between two joins needs d >= 2s and the first and last need d >= s. A 1 s card with a
    1.5 s transition is a legal plan, so clamp rather than reject; the renderer warns when it
    does.
    """
    if style is None or style.transition == "cut" or len(durations) < 2:
        return Fraction(0)
    if style.transition in OVERLAPPING:
        limit = min([durations[0], durations[-1], *(d / 2 for d in durations[1:-1])])
    else:
        limit = min(durations)
    return min(style.transition_seconds, limit)


def segment_starts(durations, overlap):
    """Reel time at which each segment's own clock starts; a dissolve overlaps neighbours."""
    starts, at = [], Fraction(0)
    for seconds in durations:
        starts.append(at)
        at += seconds - overlap
    return starts


def reel_seconds(durations, overlap):
    """Total length: the sum of the segments less one overlap per join."""
    return sum(durations, Fraction(0)) - overlap * (len(durations) - 1)


@dataclass(frozen=True)
class Piece:
    """One stretch of the bed heard on the reel: where it starts in the bed and on the reel,
    how long it lasts, and its linear level. Fades apply at both ends."""

    offset: Fraction
    start: Fraction
    seconds: Fraction
    level: float


def card_runs(durations, cards, overlap):
    """(start, end) on the reel of every maximal run of consecutive cards.

    Intro plus opening cards form one run, each chapter card another, closing plus outro the
    last: the bed must continue seamlessly through a run instead of restarting per card.
    """
    starts = segment_starts(durations, overlap)
    runs = []
    for index, (start, seconds, is_card) in enumerate(zip(starts, durations, cards)):
        if not is_card:
            continue
        end = start + seconds
        if runs and index and cards[index - 1]:
            runs[-1] = (runs[-1][0], end)
        else:
            runs.append((start, end))
    return runs


def music_pieces(music, durations, cards, overlap):
    """Where the bed plays, at what level, and which part of the bed each stretch uses.

    `under: cards`: one piece per card run at `gain`, taken from a running offset into the bed
    so the music progresses through the reel instead of restarting at every card.
    `under: all`: one base piece covers the whole reel at `duck`, and each card run adds a
    piece at `gain - duck` taken from the same bed position (the reel time), so the two sum
    coherently to exactly `gain` under cards and ramp between the levels over the fades.
    That is the "two levels with fades at the boundaries" the contract asks for, with no
    side-chain and no dynamics processing.
    """
    runs = card_runs(durations, cards, overlap)
    if music.under == "all":
        total = reel_seconds(durations, overlap)
        pieces = [Piece(Fraction(0), Fraction(0), total, music.duck)]
        pieces += [Piece(start, start, end - start, music.gain - music.duck) for start, end in runs]
        return pieces
    pieces, offset = [], Fraction(0)
    for start, end in runs:
        pieces.append(Piece(offset, start, end - start, music.gain))
        offset += end - start
    return pieces


def edge_fades(fade, seconds):
    """`afade` in at the start and out at the end of a `seconds`-long stream."""
    return (
        f",afade=t=in:st=0:d={float(fade):.6f}"
        f",afade=t=out:st={float(seconds - fade):.6f}:d={float(fade):.6f}"
    )


# --- Bounded assembly ---------------------------------------------------------------------------
#
# Every piece of the reel is encoded with these settings, so the join can copy the video stream:
# identical settings give identical H.264 parameter sets, `-bf 0` keeps decode order equal to
# presentation order (no reordering delay, no edit list to reconcile at a join), and the explicit
# colour tags make a card drawn in RGB and a clip decoded from the recording describe their pixels
# the same way, whatever either FFmpeg version would have tagged on its own. Untagged 1080p is
# BT.709 to every player, so the tags change nothing visible; they only make the pieces agree.
COLOR_FLAGS = [
    *("-color_range", "tv"),
    *("-colorspace", "bt709"),
    *("-color_primaries", "bt709"),
    *("-color_trc", "bt709"),
]
COLOR_TAGS = "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709"
# Cards are drawn in RGB and converted here; the matrix must match the tag above.
CARD_MATRIX = "bt709"


def piece_encoder_flags(fps, cfr_flags):
    """The H.264 settings of every piece and bridge: the clips' quality, joinable by stream copy."""
    return [
        *("-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-bf", "0"),
        *("-pix_fmt", "yuv420p"),
        *COLOR_FLAGS,
        *("-r", fps_text(fps)),
        *cfr_flags,
    ]


def frame_count(seconds, fps):
    """Whole frames in `seconds` at `fps`, at least one: every piece is cut to whole frames so
    that the stream-copied joins meet at frame boundaries."""
    return max(1, round(Fraction(seconds) * fps))


def sample_count(frames, fps, rate):
    """Samples that play for exactly `frames` at `fps`: a piece's audio is padded or trimmed to
    this, so video and audio stay in step at every join instead of drifting by the difference."""
    return round(Fraction(frames) * rate / fps)


def transition_frames(transition, fps, frames):
    """A dissolve in whole frames, shortened so that every piece keeps at least one frame of its
    own between the bridges (`effective_transition` already bounds it in seconds; this is the
    same rule on the frame grid, where a 0.8 s card and a 0.4 s dissolve would leave nothing)."""
    if transition <= 0 or len(frames) < 2:
        return 0
    count = round(transition * fps)
    limit = min([frames[0] - 1, frames[-1] - 1, *((n - 1) // 2 for n in frames[1:-1])])
    return max(0, min(count, limit))


@dataclass
class Segment:
    """One stretch of the finished reel, in playback order: a piece's body or a bridge.

    `video` (H.264, encoded with `piece_encoder_flags`) is stream-copied into the reel; `audio`
    (float WAV) is joined sample by sample. `frames` and `samples` are exact, `start` is the
    reel sample at which the segment begins, so the music is mixed into exactly the segments it
    overlaps. Both files live in the reel workspace and are named relative to it.
    """

    name: str
    frames: int
    samples: int
    start: int = 0

    @property
    def video(self):
        return f"{self.name}.mp4"

    audio: str = ""

    def __post_init__(self):
        if not self.audio:
            self.audio = f"{self.name}.wav"


def exact_frames(chain, frames):
    """Cut or extend a conformed video stream to exactly `frames`: `tpad` repeats the last frame
    while the source runs short (a clip whose audio outlasts its video, as the v1.1 `concat`
    filter also filled), `trim` drops the surplus and closes the input behind it."""
    return f"{chain},tpad=stop_mode=clone:stop=-1,trim=end_frame={frames}"


def exact_samples(chain, samples):
    """The audio counterpart: `apad` fills with silence up to `samples`, `atrim` cuts there."""
    return f"{chain},apad=whole_len={samples},atrim=end_sample={samples}"


def piece_graph(
    video_input,
    audio_input,
    dimensions,
    fps,
    audio,
    frames,
    samples,
    *,
    card=False,
    dip=(Fraction(0), Fraction(0)),
    audio_fade=Fraction(0),
    head=(0, 0),
    tail=(0, 0),
):
    """The filter graph that turns one source into a reel piece of exactly `frames`/`samples`.

    The conform is the v1.1 one (`video_chain`, `audio_chain`), then the lengths are pinned
    (`exact_frames`, `exact_samples`) and the colour tags set. `dip` is the pair of half-fade
    lengths (in, out) of a dip transition, applied inside the piece as before; `audio_fade` the
    clip's edge fades. For a dissolve, `head`/`tail` are the (frames, samples) that the bridges
    need from each end: they are split off here, in the same pass, so a source is decoded once;
    the body between them is what the reel plays as this piece. Output labels: `[video]` and
    `[audio]` for the body, `[head]`/`[ahead]` and `[tail]`/`[atail]` for the ends.
    """
    length = Fraction(frames) / fps
    matrix = CARD_MATRIX if card else None
    video = exact_frames(video_chain(video_input, dimensions, fps, matrix), frames)
    fade_in, fade_out = dip
    if fade_in > 0:
        video += f",fade=t=in:st=0:d={float(fade_in):.6f}"
    if fade_out > 0:
        video += f",fade=t=out:st={float(length - fade_out):.6f}:d={float(fade_out):.6f}"
    video += f",{COLOR_TAGS}"
    lines = _split_ends(video, "v", frames, head[0], tail[0], "trim", "frame")
    if audio:
        sound = exact_samples(audio_chain(audio_input, audio), samples)
        if audio_fade > 0:
            sound += edge_fades(audio_fade, length)
        lines += _split_ends(sound, "a", samples, head[1], tail[1], "atrim", "sample")
    return ";\n".join(lines)


def _split_ends(chain, kind, total, head, tail, trim, unit):
    """Route `chain` to its body and, when asked, to head and tail outputs of exact lengths."""
    body, head_label, tail_label = (
        ("video", "head", "tail") if kind == "v" else ("audio", "ahead", "atail")
    )
    if not head and not tail:
        return [f"{chain}[{body}]"]
    setpts = "setpts=PTS-STARTPTS" if kind == "v" else "asetpts=PTS-STARTPTS"
    split = "split" if kind == "v" else "asplit"
    taps = [f"[{kind}b]"] + ([f"[{kind}h]"] if head else []) + ([f"[{kind}t]"] if tail else [])
    lines = [f"{chain},{split}={len(taps)}{''.join(taps)}"]
    lines.append(f"[{kind}b]{trim}=start_{unit}={head}:end_{unit}={total - tail},{setpts}[{body}]")
    if head:
        lines.append(f"[{kind}h]{trim}=end_{unit}={head}[{head_label}]")
    if tail:
        lines.append(f"[{kind}t]{trim}=start_{unit}={total - tail},{setpts}[{tail_label}]")
    return lines


def bridge_video_graph(frames, fps):
    """Cross-fade the outgoing tail (input 0) into the incoming head (input 1): `xfade`, as the
    v1.2 graph used, over the whole of both, pinned to exactly `frames` (FFmpeg 4.4's `xfade`
    emits one frame more than 7.0.2 over a chain; here that can only change the last frame)."""
    seconds = float(Fraction(frames) / fps)
    chain = f"[0:v:0][1:v:0]xfade=transition=fade:duration={seconds:.6f}:offset=0"
    return f"{exact_frames(chain, frames)},{COLOR_TAGS}[video]"


def bridge_audio_graph(samples, rate):
    """The audio of a dissolve: a linear cross-fade of the tail into the head, sample-exact.

    Equivalent to `acrossfade=c1=tri:c2=tri` on the two ends, written as two fades summed so
    that both inputs may be exactly as long as the fade (acrossfade wants more of the first).
    """
    seconds = float(Fraction(samples) / rate)
    lines = [
        f"[0:a:0]afade=t=out:st=0:d={seconds:.6f}:curve=tri[out]",
        f"[1:a:0]afade=t=in:st=0:d={seconds:.6f}:curve=tri[in]",
        exact_samples(
            "[out][in]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0", samples
        )
        + "[audio]",
    ]
    return ";\n".join(lines)


def bed_piece_graph(piece, audio, fade_seconds):
    """One stretch of the bed as its own file: the v1.2 chain (cut from the staged bed at a
    sample offset, level, edge fades) without the placement, which `mix_graph` does per
    segment. Returns the graph and the stretch's sample count."""
    rate = int(audio["sample_rate"])
    layout = "stereo" if int(audio["channels"]) == 2 else "mono"
    first = round(piece.offset * rate)
    last = round((piece.offset + piece.seconds) * rate)
    chain = (
        f"[0:a:0]aformat=sample_fmts=fltp:channel_layouts={layout},"
        f"atrim=start_sample={first}:end_sample={last},asetpts=PTS-STARTPTS,"
        f"volume={piece.level:.6f}"
    )
    fade = min(fade_seconds, piece.seconds / 2)
    if fade > 0:
        chain += edge_fades(fade, piece.seconds)
    return chain + "[audio]", last - first


def mix_graph(overlaps, channels):
    """Sum the overlapping stretches of bed files onto one segment's audio (input 0).

    `overlaps` are (input index, first sample, last sample, delay) tuples: the samples of that
    bed file heard during this segment and where in the segment they begin. `amix` runs with
    `normalize=0`, as before, so the speech keeps its level; `duration=first` ends with the
    segment. Fades are already in the bed files, so a run's ramps cross segment joins intact.
    """
    lines, labels = [], []
    for k, (index, first, last, delay) in enumerate(overlaps, 1):
        chain = f"[{index}:a:0]atrim=start_sample={first}:end_sample={last},asetpts=PTS-STARTPTS"
        if delay:
            chain += ",adelay=" + "|".join([f"{delay}S"] * channels)
        lines.append(f"{chain}[m{k}]")
        labels.append(f"[m{k}]")
    lines.append(
        f"[0:a:0]{''.join(labels)}amix=inputs={len(overlaps) + 1}:duration=first:"
        "dropout_transition=0:normalize=0[audio]"
    )
    return ";\n".join(lines)


def concat_list(segments, fps):
    """The concat-demuxer script of the reel's video: every segment starts at 0 and lasts
    exactly its frames, so the demuxer's running offset is the sum of the declared durations
    and the containers' own lengths are never consulted."""
    lines = ["ffconcat version 1.0"]
    for segment in segments:
        lines += [f"file {segment.video}", f"duration {float(Fraction(segment.frames) / fps):.6f}"]
    return "\n".join(lines) + "\n"


def wav_layout(path):
    """(fmt chunk, data offset, data length, samples) of a WAV, read from its RIFF chunks."""
    with open(path, "rb") as handle:
        header = handle.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise RenderError(f"{path.name} is not a WAV file")
        fmt, data = None, None
        position = 12
        while data is None:
            chunk = handle.read(8)
            if len(chunk) < 8:
                raise RenderError(f"{path.name} has no data chunk")
            kind, size = chunk[:4], int.from_bytes(chunk[4:], "little")
            position += 8
            if kind == b"fmt ":
                fmt = handle.read(size)
            elif kind == b"data":
                data = (position, size)
            else:
                handle.seek(size + (size & 1), 1)
            position += size + (size & 1)
    if fmt is None:
        raise RenderError(f"{path.name} has no fmt chunk")
    block = int.from_bytes(fmt[12:14], "little")
    if not block or data[1] % block:
        raise RenderError(f"{path.name} holds a partial sample")
    return fmt, data[0], data[1], data[1] // block


def join_wavs(paths, output):
    """Concatenate WAVs of one format into `output`, byte for byte; return the sample count.

    Why here and not in FFmpeg: the pieces' audio is PCM, so the join is a copy, and doing it
    in the renderer makes the reel's sample count exact and checkable before a single AAC
    frame is encoded. AAC per piece would have opened a gap of encoder priming at every join.
    """
    layouts = [wav_layout(path) for path in paths]
    fmt = layouts[0][0]
    if any(layout[0] != fmt for layout in layouts):
        raise RenderError("reel audio pieces differ in format")
    total = sum(layout[2] for layout in layouts)
    with open(output, "wb") as out:
        out.write(b"RIFF" + (4 + 8 + len(fmt) + 8 + total).to_bytes(4, "little") + b"WAVE")
        out.write(b"fmt " + len(fmt).to_bytes(4, "little") + fmt)
        out.write(b"data" + total.to_bytes(4, "little"))
        for path, (_, offset, size, _) in zip(paths, layouts):
            with open(path, "rb") as source:
                source.seek(offset)
                remaining = size
                while remaining:
                    block = source.read(min(remaining, 1 << 20))
                    if not block:
                        raise RenderError(f"{path.name} ended early")
                    out.write(block)
                    remaining -= len(block)
    return sum(layout[3] for layout in layouts)


def wav_seconds(data):
    """Exact length of a staged WAV: its sample count over its rate."""
    stream = next(s for s in data["streams"] if s["codec_type"] == "audio")
    if "duration_ts" in stream and "time_base" in stream:
        return int(stream["duration_ts"]) * Fraction(stream["time_base"])
    return Fraction(str(stream["duration"]))


def stage_bed(tools, music, workspace, audio, needed):
    """Decode the bed to a WAV at the reel's rate and layout that covers `needed` seconds.

    Why a WAV first: mp3 and ogg frames carry encoder delay and padding, so looping the
    compressed file seams with a gap, and its probed duration is approximate. A WAV loops
    sample-exactly with `-stream_loop`, its length is exact, and the 16-bit conversion bounds
    the bed at full scale, so `headroom_gain` needs no measurement of it. The first pass is
    bounded by `-t`, so a long bed is never decoded further than the reel needs.
    """
    rate, channels = int(audio["sample_rate"]), int(audio["channels"])
    margin = float(needed) + 1
    once = workspace / "bed-once.wav"
    try:
        tools.encode(
            [
                "-y",
                "-i",
                music.path,
                "-map",
                "0:a:0",
                "-t",
                f"{margin:.6f}",
                "-ar",
                str(rate),
                "-ac",
                str(channels),
                "-c:a",
                "pcm_s16le",
                once,
            ]
        )
        seconds = wav_seconds(tools.probe(once))
    except (RenderError, StopIteration, KeyError, ValueError) as exc:
        raise RenderError(f"music bed {music.path}: {exc}") from exc
    if seconds <= 0:
        raise RenderError(f"music bed {music.path} contains no audio")
    if seconds >= needed:
        return once
    if not music.loop:
        warn(
            f"music bed is {float(seconds):g}s but {float(needed):g}s of the reel call for it "
            "and loop is off; the music runs out and the rest is silent"
        )
        return once
    looped = workspace / "bed.wav"
    tools.encode(
        [
            "-y",
            "-stream_loop",
            str(math.ceil(margin / float(seconds))),
            "-i",
            once,
            "-t",
            f"{margin:.6f}",
            "-c:a",
            "pcm_s16le",
            looped,
        ]
    )
    return looped


@dataclass(frozen=True)
class Mix:
    """Static gain on the whole mix, and the linear peak of the loudest clip it contains."""

    gain: float = 1.0
    speech_peak: float = 0.0

    @property
    def ceiling_db(self):
        """Loudest peak the verified reel may show: full scale, or the speech itself when the
        source already peaks above it (that is the recording's doing, not the music's)."""
        return max(0.0, linear_to_db(self.speech_peak * self.gain)) + PEAK_SLACK_DB


def headroom_gain(tools, style, clips):
    """Gain that keeps the worst-case speech-plus-music peak HEADROOM_DB below full scale.

    Why arithmetic instead of a limiter: FFmpeg 4.4's `alimiter` delays the audio by its
    look-ahead and never flushes it (the `latency` option is 5.1+), so it would shift every
    reel by 5 ms and drop the tail on the Pi. The bed is bounded at full scale by its 16-bit
    staging, so its peak is at most its level; the speech peak is measured on each clip. The
    two only coincide under `under: all` (bed at `duck`) or across a dissolve (the speech tail
    over a card's music); only then can the mix exceed what the speech already was, and if
    that sum would pass the target the whole mix is lowered by the shortfall with a warning.
    Speech that plays alone is never touched, however hot the recording is: a v1.1 reel would
    have carried it as is.
    """
    music = style.music
    speech = 0.0
    for path in clips:
        peak, _ = tools.audio_stats(path)
        if peak is not None:
            speech = max(speech, db_to_linear(peak))
    with_speech = music.duck if music.under == "all" else 0.0
    if style.transition in OVERLAPPING:
        with_speech = max(with_speech, music.gain)
    bound = music.gain
    if with_speech > 0:
        bound = max(bound, speech + with_speech)
    ceiling = db_to_linear(-HEADROOM_DB)
    if bound <= ceiling:
        return Mix(1.0, speech)
    gain = ceiling / bound
    warn(
        f"reel audio lowered by {-linear_to_db(gain):.1f} dB so that speech plus music stays "
        f"{HEADROOM_DB:g} dB below full scale"
    )
    return Mix(gain, speech)


def encode_piece(tools, workspace, name, inputs, graph, fps, cfr_flags, graph_flag, audio, ends):
    """One FFmpeg process per piece: `<name>.mp4` and `<name>.wav` for the body and, for a
    dissolve, `<name>-head`/`<name>-tail` files (raw video, PCM audio) for the bridges."""
    script = workspace / f"{name}.txt"
    script.write_text(graph, encoding="utf-8")
    args = ["-y", *inputs, graph_flag, script.name]
    args += ["-map", "[video]", *piece_encoder_flags(fps, cfr_flags), f"{name}.mp4"]
    for end, (frames, samples) in zip(("head", "tail"), ends):
        if frames:
            # `-r` pins the rate the raw file declares: after `setpts` a tail has none of its
            # own, and `xfade` refuses inputs whose time bases differ.
            args += ["-map", f"[{end}]", "-r", fps_text(fps), "-f", "yuv4mpegpipe"]
            args.append(f"{name}-{end}.y4m")
    if audio:
        args += ["-map", "[audio]", "-c:a", "pcm_f32le", f"{name}.wav"]
        for end, (frames, samples) in zip(("head", "tail"), ends):
            if samples:
                args += ["-map", f"[a{end}]", "-c:a", "pcm_f32le", f"{name}-{end}.wav"]
    tools.encode(args, cwd=workspace)


def encode_bridge(
    tools,
    workspace,
    name,
    outgoing,
    incoming,
    frames,
    samples,
    fps,
    cfr_flags,
    graph_flag,
    audio,
    *,
    dimensions=None,
    module=None,
    draw=None,
):
    """Two small FFmpeg processes per dissolve: the tail of `outgoing` into the head of
    `incoming`, video (`xfade`) and audio (linear cross-fade), exactly `frames`/`samples`.

    With `draw` (the `render` of the transition module at `module`) the video is drawn by
    it from the same two ends instead, and encoded with the same settings; the audio is
    the dissolve's either way."""
    if draw is not None:
        encode_module_bridge(
            tools,
            workspace,
            name,
            outgoing,
            incoming,
            frames,
            fps,
            dimensions,
            draw,
            module,
            matrix=CARD_MATRIX,
            tags=COLOR_TAGS,
            encoder_flags=piece_encoder_flags(fps, cfr_flags),
        )
    else:
        script = workspace / f"{name}.txt"
        script.write_text(bridge_video_graph(frames, fps), encoding="utf-8")
        args = [
            "-y",
            "-i",
            f"{outgoing}-tail.y4m",
            "-i",
            f"{incoming}-head.y4m",
            graph_flag,
            script.name,
        ]
        args += ["-map", "[video]", *piece_encoder_flags(fps, cfr_flags), f"{name}.mp4"]
        tools.encode(args, cwd=workspace)
    if audio:
        script = workspace / f"{name}-audio.txt"
        script.write_text(bridge_audio_graph(samples, int(audio["sample_rate"])), encoding="utf-8")
        args = ["-y", "-i", f"{outgoing}-tail.wav", "-i", f"{incoming}-head.wav"]
        args += [graph_flag, script.name, "-map", "[audio]", "-c:a", "pcm_f32le", f"{name}.wav"]
        tools.encode(args, cwd=workspace)


def render_beds(tools, workspace, bed, pieces, audio, fade_seconds, graph_flag):
    """Write every stretch of the bed the reel plays as its own WAV; return
    (file name, first reel sample, last reel sample) per stretch, for `mix_music`."""
    rate = int(audio["sample_rate"])
    beds = []
    for k, piece in enumerate(pieces):
        name = f"bed-{k:02d}"
        script = workspace / f"{name}.txt"
        graph, samples = bed_piece_graph(piece, audio, fade_seconds)
        script.write_text(graph, encoding="utf-8")
        tools.encode(
            ["-y", "-i", bed.name, graph_flag, script.name, "-map", "[audio]"]
            + ["-c:a", "pcm_f32le", f"{name}.wav"],
            cwd=workspace,
        )
        first = round(piece.start * rate)
        beds.append((f"{name}.wav", first, first + samples))
    return beds


def mix_music(tools, workspace, segments, beds, audio, graph_flag):
    """Mix into each segment the stretches of bed heard while it plays, one process per segment
    that has any: the segment plus at most the base stretch and one card run (`under: all`)."""
    channels = int(audio["channels"])
    for segment in segments:
        end = segment.start + segment.samples
        inputs, overlaps = [], []
        for name, first, last in beds:
            low, high = max(segment.start, first), min(end, last)
            if high > low:
                inputs += ["-i", name]
                overlaps.append((len(overlaps) + 1, low - first, high - first, low - segment.start))
        if not overlaps:
            continue
        script = workspace / f"{segment.name}-mix.txt"
        script.write_text(mix_graph(overlaps, channels), encoding="utf-8")
        mixed = f"{segment.name}-mix.wav"
        tools.encode(
            ["-y", "-i", segment.audio, *inputs, graph_flag, script.name, "-map", "[audio]"]
            + ["-c:a", "pcm_f32le", mixed],
            cwd=workspace,
        )
        segment.audio = mixed


def render_reel(
    tools,
    job,
    items,
    rendered,
    dimensions,
    fps,
    audio,
    graph_flag,
    cfr_flags,
    filename,
    title,
    style=None,
    draw=None,
):
    """Draw the cards, conform every piece, bridge the dissolves, mix the music, join, verify.

    `rendered` maps clip id to (staged clip path, verified seconds). The result stays inside
    the job directory: the caller publishes it together with the clips, so a reel failure
    publishes nothing, exactly like a failed clip. `style` is None for a v1.1 plan, whose
    pieces are then conformed with no v1.2 filter at all (no fades, no transition, no music):
    the v1.1 reel is the plain join of its pieces. No FFmpeg process here holds more than one
    piece's decoder and encoder, or a few seconds of audio, whatever the reel's length.
    `draw` is the already loaded `render` of `style.module`; it is loaded here when the
    caller has not (the caller loads it early, so a broken module fails before any encode).
    """
    module = style.module if style is not None else None
    if module is not None and draw is None:
        draw = load_transition(module)
    workspace = job / "_reel"
    workspace.mkdir()
    rate = int(audio["sample_rate"]) if audio else 0
    layout = "stereo" if audio and int(audio["channels"]) == 2 else "mono"
    sources, durations, cards = [], [], []
    for position, item in enumerate(items):
        if isinstance(item, Card):
            png = write_card_png(item, dimensions, workspace / f"card-{position:02d}.png")
            inputs = ["-loop", "1", "-framerate", fps_text(fps), "-i", png.name]
            if audio:
                inputs += ["-f", "lavfi", "-i", f"anullsrc=r={rate}:cl={layout}"]
            sources.append((inputs, 1))
            durations.append(item.seconds)
            cards.append(True)
        else:
            path, seconds = rendered[item]
            sources.append((["-i", path], 0))
            durations.append(Fraction(str(seconds)))
            cards.append(False)
    transition = effective_transition(style, durations)
    if style is not None and 0 < transition < style.transition_seconds:
        warn(
            f"reel {'transition' if module else style.transition} shortened from "
            f"{float(style.transition_seconds):g}s to "
            f"{float(transition):g}s so that every segment can hold it"
        )
    dissolve = style is not None and style.transition in OVERLAPPING and transition > 0
    half = transition / 2 if style is not None and style.transition == "dip" else Fraction(0)
    # Everything below lives on the frame grid: whole frames per piece, the matching sample
    # counts, and a dissolve of whole frames, so that the joins meet exactly.
    frames = [frame_count(seconds, fps) for seconds in durations]
    samples = [sample_count(count, fps, rate) if audio else 0 for count in frames]
    bridge = transition_frames(transition, fps, frames) if dissolve else 0
    bridge_samples = sample_count(bridge, fps, rate) if audio else 0
    lengths = [Fraction(count) / fps for count in frames]
    overlap = Fraction(bridge) / fps
    expected = reel_seconds(lengths, overlap)
    pieces, bed, mix = [], None, Mix()
    if style is not None and style.music is not None:
        if not audio:
            raise RenderError("music needs a source with an audio track to be mixed into")
        pieces = music_pieces(style.music, lengths, cards, overlap)
        if pieces:
            needed = max(piece.offset + piece.seconds for piece in pieces)
            bed = stage_bed(tools, style.music, workspace, audio, needed)
            speech = [rendered[item][0] for item in items if not isinstance(item, Card)]
            mix = headroom_gain(tools, style, speech)
        else:
            warn("music is set but the reel has no cards to play it under")
    beds = []
    if pieces:
        beds = render_beds(
            tools, workspace, bed, pieces, audio, style.music.fade_seconds, graph_flag
        )
    segments = []
    last = len(sources) - 1
    for k, (inputs, audio_input) in enumerate(sources):
        head = (bridge, bridge_samples) if k else (0, 0)
        tail = (bridge, bridge_samples) if k < last else (0, 0)
        fade = Fraction(0)
        if style is not None and not cards[k]:
            fade = min(style.audio_fade, lengths[k] / 2)
        graph = piece_graph(
            0,
            audio_input,
            dimensions,
            fps,
            audio,
            frames[k],
            samples[k],
            card=cards[k],
            dip=(half if k else Fraction(0), half if k < last else Fraction(0)),
            audio_fade=fade,
            head=head,
            tail=tail,
        )
        name = f"piece-{k:02d}"
        encode_piece(
            tools, workspace, name, inputs, graph, fps, cfr_flags, graph_flag, audio, (head, tail)
        )
        if k and bridge:
            joint = f"bridge-{k - 1:02d}"
            encode_bridge(
                tools,
                workspace,
                joint,
                f"piece-{k - 1:02d}",
                name,
                bridge,
                bridge_samples,
                fps,
                cfr_flags,
                graph_flag,
                audio,
                dimensions=dimensions,
                module=module,
                draw=draw,
            )
            segments.append(Segment(joint, bridge, bridge_samples))
        segments.append(
            Segment(name, frames[k] - head[0] - tail[0], samples[k] - head[1] - tail[1])
        )
    position = 0
    for segment in segments:
        segment.start = position
        position += segment.samples
    if beds:
        mix_music(tools, workspace, segments, beds, audio, graph_flag)
    script = workspace / "reel.txt"
    script.write_text(concat_list(segments, fps), encoding="utf-8")
    args = ["-y", "-f", "concat", "-safe", "1", "-auto_convert", "0", "-i", script.name]
    if audio:
        joined = join_wavs(
            [workspace / segment.audio for segment in segments], workspace / "reel.wav"
        )
        if joined != position:
            raise RenderError(
                f"reel audio holds {joined} samples where its pieces add up to {position}; "
                "the track is not continuous"
            )
        args += ["-i", "reel.wav", "-map", "0:v:0", "-c:v", "copy", "-map", "1:a:0"]
        if mix.gain < 1:
            args += ["-af", f"volume={mix.gain:.6f}"]
        args += ["-c:a", "aac", "-b:a", "192k"]
    else:
        args += ["-map", "0:v:0", "-c:v", "copy", "-an"]
    args += ["-map_metadata", "-1", "-metadata", f"title={title}", "-movflags", "+faststart"]
    args.append(filename)
    tools.encode(args, cwd=workspace)
    output = workspace / filename
    seconds = verify_reel(
        tools,
        output,
        dimensions,
        audio,
        expected,
        len(sources),
        styled=style is not None,
        ceiling_db=mix.ceiling_db if pieces else None,
    )
    return output, seconds


def verify_reel(
    tools, path, dimensions, audio, expected, segments, *, styled=False, ceiling_db=None
):
    """Same discipline as a clip: streams, geometry, duration and a full decode, before publishing.

    A v1.2 reel is also decoded for statistics: the sample count must match the expected
    length (the audio is continuous, with no missing piece), and once music has been mixed
    in, the true peak must stay under `ceiling_db` (the music clipped nothing).
    """
    data = tools.probe(path)
    videos = [s for s in data["streams"] if s["codec_type"] == "video"]
    audios = [s for s in data["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1 or len(audios) != bool(audio):
        raise RenderError("Reel verification failed: output video/audio stream counts differ")
    video = videos[0]
    if (video["width"], video["height"]) != dimensions:
        raise RenderError("Reel verification failed: output dimensions differ")
    tolerance = TOLERANCE_PER_SEGMENT * segments
    ends = [float(s.get("start_time", 0)) + float(s["duration"]) for s in videos + audios]
    for end in ends:
        if abs(end - float(expected)) > tolerance:
            raise RenderError(
                f"Reel verification failed: duration {end:g}s differs from the "
                f"{float(expected):g}s its segments and transitions add up to"
            )
    if audio:
        if int(audios[0]["channels"]) != int(audio["channels"]):
            raise RenderError("Reel verification failed: audio channel count changed")
        if abs(float(audios[0].get("start_time", 0))) > 0.001:
            raise RenderError("Reel verification failed: unexpected output audio offset")
        if styled:
            peak, samples = tools.audio_stats(path)
            decoded = samples / int(audio["sample_rate"])
            if abs(decoded - float(expected)) > tolerance:
                raise RenderError(
                    f"Reel verification failed: {decoded:g}s of audio decoded for a "
                    f"{float(expected):g}s reel; the track is not continuous"
                )
            if ceiling_db is not None and peak is not None and peak > ceiling_db:
                raise RenderError(
                    f"Reel verification failed: audio peaks at {peak:+.2f} dBFS after the music "
                    f"mix, above the {ceiling_db:+.2f} dBFS the speech and headroom allow"
                )
    tools.encode(["-i", path, "-map", "0:v", "-map", "0:a?", "-f", "null", "-"])
    return max(ends)
