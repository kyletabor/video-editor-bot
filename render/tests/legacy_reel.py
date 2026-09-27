"""The reel assembly as it was before the bounded rebuild: one FFmpeg filter graph over every
segment and the bed, kept as the reference the pieces must reproduce.

`concat_graph`, `music_graph`, `reel_graph` and `render_reel` are `cliprender.reel` at commit
58239d4, `encode_card_segment` is `cliprender.cards` there, verbatim apart from the imports.
Only the tests use this module: a fixture reel is assembled both ways and compared frame by
frame and sample by sample (`test_reel_legacy.py`), so a change to the pieces that altered
what the viewer sees or hears would show up as a difference from this graph.
"""

from fractions import Fraction

from cliprender.cards import Card, write_card_png
from cliprender.media import RenderError, warn
from cliprender.reel import (
    Mix,
    audio_chain,
    edge_fades,
    effective_transition,
    fps_text,
    headroom_gain,
    music_pieces,
    reel_seconds,
    segment_starts,
    stage_bed,
    verify_reel,
    video_chain,
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


def encode_card_segment(tools, png, output, seconds, fps, audio):
    """Turn a card PNG into an H.264 segment of exactly `seconds` at the reel's frame rate.

    The image loops for the whole duration and, when the reel has audio, is paired with
    generated silence at the reel's sample rate and channel layout so the reel's audio track
    never has a hole. Output `-t` bounds both streams; it behaves identically on FFmpeg 4.4
    and 7 whereas `-shortest` depends on interleaving.
    """
    duration = f"{float(seconds):.6f}"
    args = ["-y", "-loop", "1", "-framerate", str(fps), "-i", png]
    if audio:
        layout = "stereo" if int(audio["channels"]) == 2 else "mono"
        args += ["-f", "lavfi", "-i", f"anullsrc=r={int(audio['sample_rate'])}:cl={layout}"]
    args += ["-map", "0:v:0"]
    args += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"] if audio else ["-an"]
    args += [
        "-t",
        duration,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-movflags",
        "+faststart",
        output,
    ]
    tools.encode(args)
    return output


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
