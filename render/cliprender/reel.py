"""Assemble the contract v1.1 reel: [intro] + for each clip ([card] + clip) + [outro].

Why re-encode instead of stream-copying through the concat demuxer: stream copy is faster,
but it requires bit-identical codec parameters in every segment and inherits each file's
AAC priming and container timescale quirks at every junction, and those details differ
between FFmpeg 4.4 and 7. One `concat` filter graph decodes every segment, normalizes size,
frame rate, pixel format, sample rate and channel layout, and emits one continuous timeline
whose length is simply the sum of its parts. Cards are short and clips are already CRF 18,
so the second generation costs little; the reel is the deliverable, the clips are the proof.
"""

from __future__ import annotations

from fractions import Fraction

from .cards import Card, chapter_footer, encode_card_segment, write_card_png
from .media import RenderError

MAX_RECOMMENDED_SECONDS = 600
# Card segments and clips are each exact to well under a frame; junction slop comes from AAC
# frame padding and the constant-frame-rate conformance, so the budget is per segment.
TOLERANCE_PER_SEGMENT = 0.2


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
    """
    mode = reel.get("chapter_cards", "auto")
    clips = plan["clips"]
    items = []
    if "intro" in reel:
        items.append(Card.from_plan(reel["intro"]))
    for index, clip in enumerate(clips, 1):
        card = clip.get("card")
        if card is None and mode == "all":
            card = {"title": clip["takeaway"]}
        if card is not None and mode != "none":
            footer = chapter_footer(index, len(clips), clip["segments"][0]["start"])
            items.append(Card.from_plan(card, footer))
        items.append(clip["id"])
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


def concat_graph(count, dimensions, fps, audio):
    """Normalize every input to the reel's format, then join them in order."""
    width, height = dimensions
    graph = []
    for i in range(count):
        # `start_time=0` pads a clip whose first frame sits after its audio start with a copy
        # of that frame, so no junction opens a hole; the size filters are a safety net since
        # cards are drawn at the reel size and clips were rendered at it.
        graph.append(
            f"[{i}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"fps={fps_text(fps)}:start_time=0,format=yuv420p[v{i}]"
        )
        if audio:
            layout = "stereo" if int(audio["channels"]) == 2 else "mono"
            graph.append(
                f"[{i}:a:0]aresample={int(audio['sample_rate'])},"
                f"aformat=sample_fmts=fltp:channel_layouts={layout},asetpts=PTS-STARTPTS[a{i}]"
            )
    pads = "".join(f"[v{i}][a{i}]" if audio else f"[v{i}]" for i in range(count))
    outputs = "[video][audio]" if audio else "[video]"
    graph.append(f"{pads}concat=n={count}:v=1:a={1 if audio else 0}{outputs}")
    return ";\n".join(graph)


def render_reel(
    tools, job, items, rendered, dimensions, fps, audio, graph_flag, cfr_flags, filename, title
):
    """Draw and encode the cards, concatenate everything, verify, and return the staged file.

    `rendered` maps clip id to (staged clip path, verified seconds). The result stays inside
    the job directory: the caller publishes it together with the clips, so a reel failure
    publishes nothing, exactly like a failed clip.
    """
    workspace = job / "_reel"
    workspace.mkdir()
    inputs, expected = [], Fraction(0)
    for position, item in enumerate(items):
        if isinstance(item, Card):
            stem = workspace / f"card-{position:02d}"
            png = write_card_png(item, dimensions, stem.with_suffix(".png"))
            segment = stem.with_suffix(".mp4")
            encode_card_segment(tools, png, segment, item.seconds, fps_text(fps), audio)
            inputs.append(segment)
            expected += item.seconds
        else:
            path, seconds = rendered[item]
            inputs.append(path)
            expected += Fraction(str(seconds))
    script = workspace / "concat.txt"
    script.write_text(concat_graph(len(inputs), dimensions, fps, audio), encoding="utf-8")
    output = workspace / filename
    args = ["-y"]
    for path in inputs:
        args += ["-i", path]
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
    seconds = verify_reel(tools, output, dimensions, audio, expected, len(inputs))
    return output, seconds


def verify_reel(tools, path, dimensions, audio, expected, segments):
    """Same discipline as a clip: streams, geometry, duration and a full decode, before publishing."""
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
                f"{float(expected):g}s sum of its segments"
            )
    if audio:
        if int(audios[0]["channels"]) != int(audio["channels"]):
            raise RenderError("Reel verification failed: audio channel count changed")
        if abs(float(audios[0].get("start_time", 0))) > 0.001:
            raise RenderError("Reel verification failed: unexpected output audio offset")
    tools.encode(["-i", path, "-map", "0:v", "-map", "0:a?", "-f", "null", "-"])
    return max(ends)
