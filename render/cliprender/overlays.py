"""Contract v1.4 `clips[].overlays`: short labels burned over a clip, timed in source seconds.

Why source seconds: the label says what the shared screen shows, so it belongs to the
picture, not to a position in the clip. The bot writes `[start, end)` against the recording;
the renderer shows the label on exactly the kept frames whose source time falls in that
interval, wherever the segments put them, so trimming or reordering never moves a label onto
the wrong picture.

Why libass and not `drawtext`: Homebrew's plain FFmpeg ships without FreeType, so `drawtext`
is missing there, while the `subtitles` filter (libass) is already required for burned
captions. A clip's labels become one small `.ass` script on the clip's output timeline,
burned by every part against the same timestamps, exactly like the captions' SRT.

Why frame runs rather than mapped intervals: ASS times are centiseconds and the `subtitles`
filter compares them with a frame's timestamp in milliseconds, so an event edge computed in
continuous time could land on either side of a frame. Instead every kept frame is tested
against the interval on its own, consecutive frames that pass become one event, and the event
edges are placed between frames (`event_bounds`), so each frame is decided once, here.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

# Label look: white text on a semi-opaque dark box, about 3.2 % of the frame height.
FONT_SHARE = 0.032
MARGIN_X_SHARE = 0.035
MARGIN_Y_SHARE = 0.045
# ASS colours are &HAABBGGRR with alpha 00 = opaque; 0x50 leaves the box about 69 % opaque.
TEXT_COLOUR = "&H00FFFFFF"
BOX_COLOUR = "&H50101010"
FONT = "Helvetica"
# Margin of an event edge from the frame it must include, in milliseconds. FFmpeg may round a
# frame's microsecond timestamp to the nearest millisecond, so one millisecond is not enough.
EDGE_MS = 2


@dataclass(frozen=True)
class Run:
    """One stretch of consecutive output frames that show a label: the first and last
    frame's output time in seconds, the text and its position."""

    first: Fraction
    last: Fraction
    text: str
    position: str = "top"


def kept_frames(selected, times, origin):
    """(source seconds, output seconds) of every kept frame, in output order.

    `selected` holds (first, stop, start, end, offset) per segment, as `renderer.selections`
    returns them; a frame's output time is exactly what `video_graph`'s `setpts` gives it.
    """
    frames = []
    for first, stop, start, _, offset in selected:
        for index in range(first, stop):
            source = times[index] - origin
            frames.append((source, source - start + offset))
    return frames


def overlay_runs(overlays, frames):
    """Group, per overlay, the consecutive kept frames whose source time is in [start, end).

    A label whose interval two segments both keep (or one segment keeps twice) gets one run
    per stretch; one whose interval no segment keeps gets none.
    """
    runs = []
    for overlay in overlays:
        start, end = Fraction(str(overlay["start"])), Fraction(str(overlay["end"]))
        current = None
        for source, output in frames:
            if start <= source < end:
                current = [output, output] if current is None else [current[0], output]
            elif current is not None:
                runs.append(
                    Run(current[0], current[1], overlay["text"], overlay.get("position", "top"))
                )
                current = None
        if current is not None:
            runs.append(
                Run(current[0], current[1], overlay["text"], overlay.get("position", "top"))
            )
    return sorted(runs, key=lambda run: (run.first, run.position))


def event_bounds(run):
    """(start, end) in centiseconds so the event covers exactly the run's frames.

    The start sits just before the first frame and the end just after the last, each within
    `EDGE_MS` + 10 ms of it, so frames at least 14 ms apart (up to 60 fps) on either side stay
    outside: a frame shows the label when start <= its time < end.
    """
    first_ms = int(run.first * 1000)
    last_ms = int(run.last * 1000)
    start = max(0, (first_ms - EDGE_MS) // 10)
    end = (last_ms + EDGE_MS) // 10 + 1
    return start, end


def ass_time(centiseconds):
    hours, rest = divmod(centiseconds, 360000)
    minutes, rest = divmod(rest, 6000)
    seconds, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def ass_text(text):
    """Plain text for an ASS Dialogue line. Braces open override blocks and a backslash
    starts a tag, so they are replaced by look-alikes rather than interpreted."""
    return text.replace("\\", "⧵").replace("{", "(").replace("}", ")").replace("\n", " ")


def alignment(position, avoid=None):
    """ASS numpad alignment: top-left (7) or bottom-left (1); the right side (9 or 3) when a
    pip speaker tile occupies that left corner."""
    if position == "bottom":
        return 3 if avoid == "bottom-left" else 1
    return 9 if avoid == "top-left" else 7


def format_ass(runs, size, avoid=None):
    """An ASS script that burns `runs` on a frame of `size`, in pixels (PlayRes = size)."""
    width, height = size
    font = max(8, round(height * FONT_SHARE))
    margin_x, margin_y = round(width * MARGIN_X_SHARE), round(height * MARGIN_Y_SHARE)
    pad = max(2, round(font * 0.3))
    style = (
        f"{FONT},{font},{TEXT_COLOUR},{TEXT_COLOUR},{BOX_COLOUR},{BOX_COLOUR},"
        f"0,0,0,0,100,100,0,0,3,{pad},0"
    )
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
    ]
    for position in ("top", "bottom"):
        lines.append(
            f"Style: {position},{style},{alignment(position, avoid)},"
            f"{margin_x},{margin_x},{margin_y},1"
        )
    lines += [
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for run in runs:
        start, end = event_bounds(run)
        lines.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{run.position},,0,0,0,,"
            f"{ass_text(run.text)}"
        )
    return "\n".join(lines) + "\n"
