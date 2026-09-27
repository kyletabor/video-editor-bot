"""Assemble the contract reel: [intro] + opening + Σ([card] + clip) + closing + [outro].

Why re-encode instead of stream-copying through the concat demuxer: stream copy is faster,
but it requires bit-identical codec parameters in every segment and inherits each file's
AAC priming and container timescale quirks at every junction, and those details differ
between FFmpeg 4.4 and 7. One filter graph decodes every segment, normalizes size, frame
rate, pixel format, sample rate and channel layout, and emits one continuous timeline whose
length is simply the sum of its parts. Cards are short and clips are already CRF 18, so the
second generation costs little; the reel is the deliverable, the clips are the proof.

Contract v1.2 (opening/closing cards, a music bed, transitions, audio fades) is layered onto
that same graph rather than run as a second pass, for two reasons. First, a plan that uses no
v1.2 field must render byte for byte as a v1.1 reel: `reel_graph` defers to the unchanged
`concat_graph` and the command line is identical, so nothing can drift. Second, every v1.2
effect is a filter that exists with the same semantics in FFmpeg 4.4 and 7 (`fade`, `afade`,
`xfade`, `acrossfade`, `atrim`, `adelay`, `amix`), which keeps the ARM Pi with the Ubuntu
4.4.2 build a first-class renderer. The music bed is decoded once to a WAV, cut into pieces
placed on the reel with `adelay`, and summed onto the speech with `amix`; the mix is kept
from clipping by arithmetic (`headroom_gain`) rather than a limiter, because FFmpeg 4.4's
`alimiter` delays audio by its look-ahead and drops the tail.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .cards import Card, chapter_footer, encode_card_segment, write_card_png
from .media import RenderError, warn

MAX_RECOMMENDED_SECONDS = 600
# Card segments and clips are each exact to well under a frame; junction slop comes from AAC
# frame padding and the constant-frame-rate conformance, so the budget is per segment.
TOLERANCE_PER_SEGMENT = 0.2
# Any of these in `output.reel` switches the reel from the v1.1 graph to the v1.2 one; the
# schema defaults then apply to the fields that are absent (transition cut, 0.15 s audio fades).
V12_FIELDS = ("opening", "closing", "music", "transition", "audio_fade_seconds")
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


def video_chain(index, dimensions, fps):
    """Conform one input's video to the reel: size, square pixels, constant rate, yuv420p.

    `start_time=0` pads a clip whose first frame sits after its audio start with a copy of
    that frame, so no junction opens a hole; the size filters are a safety net since cards
    are drawn at the reel size and clips were rendered at it.
    """
    width, height = dimensions
    return (
        f"[{index}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
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


def concat_graph(count, dimensions, fps, audio):
    """Normalize every input to the reel's format, then join them in order (the v1.1 graph)."""
    graph = []
    for i in range(count):
        graph.append(f"{video_chain(i, dimensions, fps)}[v{i}]")
        if audio:
            graph.append(f"{audio_chain(i, audio)}[a{i}]")
    pads = "".join(f"[v{i}][a{i}]" if audio else f"[v{i}]" for i in range(count))
    outputs = "[video][audio]" if audio else "[video]"
    graph.append(f"{pads}concat=n={count}:v=1:a={1 if audio else 0}{outputs}")
    return ";\n".join(graph)


def effective_transition(style, durations):
    """Transition length the segments can afford, or 0 when there is nothing to join.

    A dip needs each segment to hold its two half-fades (d >= s). A dissolve consumes s from
    both sides of a join, so a segment between two joins needs d >= 2s and the first and last
    need d >= s. A 1 s card with a 1.5 s transition is a legal plan, so clamp rather than
    reject; the renderer warns when it does.
    """
    if style is None or style.transition == "cut" or len(durations) < 2:
        return Fraction(0)
    if style.transition == "dissolve":
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


def music_graph(pieces, bed_input, audio, fade_seconds, mix_gain):
    """Cut the staged bed into `pieces`, place them on the reel, and sum them onto `[speech]`.

    `adelay` takes sample counts (the `S` suffix) so placement is exact; `amix` runs with
    `normalize=0` because its default divides every input by the input count, which would
    halve the speech. `duration=first` ends the mix with the speech, which is the reel's
    length. Fades never exceed half a piece, so a short card still ramps in and out.
    """
    rate, channels = int(audio["sample_rate"]), int(audio["channels"])
    layout = "stereo" if channels == 2 else "mono"
    labels = "".join(f"[b{k}]" for k in range(len(pieces)))
    graph = [
        (
            f"[{bed_input}:a:0]aformat=sample_fmts=fltp:channel_layouts={layout},"
            f"asplit={len(pieces)}{labels}"
        )
    ]
    for k, piece in enumerate(pieces):
        first = round(piece.offset * rate)
        last = round((piece.offset + piece.seconds) * rate)
        chain = (
            f"[b{k}]atrim=start_sample={first}:end_sample={last},asetpts=PTS-STARTPTS,"
            f"volume={piece.level:.6f}"
        )
        fade = min(fade_seconds, piece.seconds / 2)
        if fade > 0:
            chain += edge_fades(fade, piece.seconds)
        delay = round(piece.start * rate)
        if delay:
            chain += ",adelay=" + "|".join([f"{delay}S"] * channels)
        graph.append(f"{chain}[m{k}]")
    inputs = "[speech]" + "".join(f"[m{k}]" for k in range(len(pieces)))
    mix = f"{inputs}amix=inputs={len(pieces) + 1}:duration=first:dropout_transition=0:normalize=0"
    if mix_gain < 1:
        mix += f",volume={mix_gain:.6f}"
    graph.append(f"{mix}[audio]")
    return graph


def reel_graph(
    count,
    dimensions,
    fps,
    audio,
    style=None,
    durations=(),
    cards=(),
    transition=Fraction(0),
    pieces=(),
    mix_gain=1.0,
):
    """The v1.1 concat graph, or the v1.2 graph with fades, transitions and the music mix.

    `dip` fades each segment's video to black over half the transition and the next one in
    from black over the other half, inside the segments, so the length is unchanged and the
    audio is untouched. `dissolve` cross-fades video (`xfade`) and audio (`acrossfade`) so
    consecutive segments overlap by the transition and the reel shortens by that much per
    join. Clip audio fades at its edges in every mode; cards are silent and need none. The
    bed, when present, is input number `count`, after the segments.
    """
    if style is None:
        return concat_graph(count, dimensions, fps, audio)
    dissolve = style.transition == "dissolve" and transition > 0
    dip = style.transition == "dip" and transition > 0
    half = float(transition) / 2
    graph = []
    for i in range(count):
        chain = video_chain(i, dimensions, fps)
        if dip and i:
            chain += f",fade=t=in:st=0:d={half:.6f}"
        if dip and i < count - 1:
            chain += f",fade=t=out:st={float(durations[i]) - half:.6f}:d={half:.6f}"
        graph.append(f"{chain}[v{i}]")
        if audio:
            chain = audio_chain(i, audio)
            fade = min(style.audio_fade, durations[i] / 2)
            if fade > 0 and not cards[i]:
                chain += edge_fades(fade, durations[i])
            graph.append(f"{chain}[a{i}]")
    speech = "speech" if pieces else "audio"
    if dissolve:
        starts = segment_starts(durations, transition)
        previous = "v0"
        for i in range(1, count):
            label = "video" if i == count - 1 else f"x{i}"
            graph.append(
                f"[{previous}][v{i}]xfade=transition=fade:duration={float(transition):.6f}:"
                f"offset={float(starts[i]):.6f}[{label}]"
            )
            previous = label
        if audio:
            previous = "a0"
            for i in range(1, count):
                label = speech if i == count - 1 else f"y{i}"
                graph.append(
                    f"[{previous}][a{i}]acrossfade=d={float(transition):.6f}:c1=tri:c2=tri[{label}]"
                )
                previous = label
    else:
        pads = "".join(f"[v{i}][a{i}]" if audio else f"[v{i}]" for i in range(count))
        outputs = f"[video][{speech}]" if audio else "[video]"
        graph.append(f"{pads}concat=n={count}:v=1:a={1 if audio else 0}{outputs}")
    if pieces:
        graph += music_graph(pieces, count, audio, style.music.fade_seconds, mix_gain)
    return ";\n".join(graph)


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
    if style.transition == "dissolve":
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
):
    """Draw and encode the cards, join everything, verify, and return the staged file.

    `rendered` maps clip id to (staged clip path, verified seconds). The result stays inside
    the job directory: the caller publishes it together with the clips, so a reel failure
    publishes nothing, exactly like a failed clip. `style` is None for a v1.1 plan, whose
    command line and graph are then exactly those of the v1.1 renderer.
    """
    workspace = job / "_reel"
    workspace.mkdir()
    inputs, durations, cards = [], [], []
    for position, item in enumerate(items):
        if isinstance(item, Card):
            stem = workspace / f"card-{position:02d}"
            png = write_card_png(item, dimensions, stem.with_suffix(".png"))
            segment = stem.with_suffix(".mp4")
            encode_card_segment(tools, png, segment, item.seconds, fps_text(fps), audio)
            inputs.append(segment)
            durations.append(item.seconds)
            cards.append(True)
        else:
            path, seconds = rendered[item]
            inputs.append(path)
            durations.append(Fraction(str(seconds)))
            cards.append(False)
    transition = effective_transition(style, durations)
    if style is not None and 0 < transition < style.transition_seconds:
        warn(
            f"reel {style.transition} shortened from {float(style.transition_seconds):g}s to "
            f"{float(transition):g}s so that every segment can hold it"
        )
    overlap = transition if style is not None and style.transition == "dissolve" else Fraction(0)
    expected = reel_seconds(durations, overlap)
    pieces, bed, mix = [], None, Mix()
    if style is not None and style.music is not None:
        if not audio:
            raise RenderError("music needs a source with an audio track to be mixed into")
        pieces = music_pieces(style.music, durations, cards, overlap)
        if pieces:
            needed = max(piece.offset + piece.seconds for piece in pieces)
            bed = stage_bed(tools, style.music, workspace, audio, needed)
            speech = [rendered[item][0] for item in items if not isinstance(item, Card)]
            mix = headroom_gain(tools, style, speech)
        else:
            warn("music is set but the reel has no cards to play it under")
    script = workspace / "concat.txt"
    graph = reel_graph(
        len(inputs), dimensions, fps, audio, style, durations, cards, transition, pieces, mix.gain
    )
    script.write_text(graph, encoding="utf-8")
    output = workspace / filename
    args = ["-y"]
    for path in inputs:
        # `concat` consumes one segment at a time, so one decoding thread per input keeps
        # well ahead of the encoder; the default frame-threaded decoders would each hold a
        # set of 1080p buffers for inputs that are only waiting their turn. Measured on the
        # 79-minute recording's 17-input reel: 81 fps versus 73 fps, and far fewer threads.
        args += ["-threads", "1", "-i", path]
    if bed is not None:
        args += ["-threads", "1", "-i", bed]
    args += [graph_flag, script, "-map", "[video]"]
    args += ["-map", "[audio]", "-c:a", "aac", "-b:a", "192k"] if audio else ["-an"]
    args += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-r",
        fps_text(fps),
        *cfr_flags,
        "-map_metadata",
        "-1",
        "-metadata",
        f"title={title}",
        "-movflags",
        "+faststart",
        output,
    ]
    tools.encode(args, cwd=workspace)
    seconds = verify_reel(
        tools,
        output,
        dimensions,
        audio,
        expected,
        len(inputs),
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
