"""Draw contract v1.1 cards (intro, chapter, outro slides) and encode them as MP4 segments.

Why this lives in the renderer: the contract deliberately exchanges no image files, so a
plan stays a small JSON document that any bot can produce. The renderer owns the look of a
card. Text is set in Pillow's bundled default font (Aileron, via FreeType) so no font file
has to exist on Windows, macOS or Linux; a card is sized to the reel's own frame so the
concatenation never has to scale it.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from PIL import Image, ImageDraw, ImageFont

BACKGROUND = (18, 21, 26)
TITLE_COLOR = (245, 246, 248)
LINE_COLOR = (200, 206, 214)
FOOTER_COLOR = (132, 141, 153)
ACCENT = (86, 156, 214)
# Layout is proportional to a 1080-pixel short edge, so 16:9, 9:16 and 1:1 frames all fit.
TITLE_SIZE, LINE_SIZE, FOOTER_SIZE = 88, 46, 30
MAX_TITLE_ROWS, MAX_ROWS_PER_LINE, MAX_LINES = 2, 2, 4
ELLIPSIS = "..."


@dataclass(frozen=True)
class Card:
    """One slide as the contract describes it, plus the renderer's footer text."""

    title: str
    lines: tuple[str, ...] = ()
    seconds: Fraction = Fraction(3)
    footer: str = ""

    @classmethod
    def from_plan(cls, spec, footer=""):
        return cls(
            spec["title"],
            tuple(spec.get("lines", [])),
            Fraction(str(spec.get("seconds", 3))),
            footer,
        )


def timestamp_label(seconds):
    """Format a source position as h:mm:ss for a card footer (floor, never rounds up)."""
    total = int(Fraction(str(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def chapter_footer(index, count, first_segment_start):
    """`k of N · h:mm:ss`: the clip's position in the reel and where it starts in the source."""
    return f"{index} of {count} · {timestamp_label(first_segment_start)}"


def load_font(size):
    """Pillow's bundled scalable default; the size is ignored only when FreeType is absent."""
    return ImageFont.load_default(size=max(4, int(size)))


def _fit(text, font, max_width):
    """Longest prefix of `text` (at least one character) narrower than `max_width`."""
    low, high = 1, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if font.getlength(text[:middle]) <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _ellipsize(row, font, max_width):
    row = row.rstrip()
    while row and font.getlength(row + ELLIPSIS) > max_width:
        row = row[:-1].rstrip()
    return row + ELLIPSIS


def wrap(text, font, max_width, max_rows):
    """Greedy word wrap into at most `max_rows` rows; overflow ends with an ellipsis.

    A single word wider than the frame is broken at characters rather than overflowing,
    because plan authors cannot see the frame and a clipped word is worse than a split one.
    """
    pieces = []
    for word in text.split():
        while font.getlength(word) > max_width and len(word) > 1:
            head = _fit(word, font, max_width)
            pieces.append(head)
            word = word[len(head) :]
        pieces.append(word)
    rows, current = [], ""
    for piece in pieces:
        candidate = f"{current} {piece}".strip()
        if current and font.getlength(candidate) > max_width:
            rows.append(current)
            current = piece
        else:
            current = candidate
    if current:
        rows.append(current)
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        rows[-1] = _ellipsize(rows[-1], font, max_width)
    return rows


def draw_card(card, size):
    """Paint a card into an RGB image of exactly `size` (width, height)."""
    width, height = size
    if min(width, height) < 2:
        raise ValueError("Card frames must be at least 2 by 2 pixels")
    scale = min(width, height) / 1080
    margin = round(width * 0.08)
    text_width = max(1, width - 2 * margin)
    title_font = load_font(TITLE_SIZE * scale)
    line_font = load_font(LINE_SIZE * scale)
    footer_font = load_font(FOOTER_SIZE * scale)
    title_step, line_step = TITLE_SIZE * scale * 1.2, LINE_SIZE * scale * 1.45
    gap = LINE_SIZE * scale

    title_rows = wrap(card.title, title_font, text_width, MAX_TITLE_ROWS)
    body_rows = []
    for line in card.lines[:MAX_LINES]:
        body_rows.extend(wrap(line, line_font, text_width, MAX_ROWS_PER_LINE))

    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    block = len(title_rows) * title_step + gap + len(body_rows) * line_step
    # Slightly above center reads as centered once the footer sits at the bottom.
    y = max(margin * 0.5, (height - block) / 2 - height * 0.04)
    for row in title_rows:
        draw.text((margin, y), row, font=title_font, fill=TITLE_COLOR)
        y += title_step
    draw.rectangle(
        (margin, y + gap * 0.3, margin + max(2, 120 * scale), y + gap * 0.3 + max(1, 5 * scale)),
        fill=ACCENT,
    )
    y += gap
    for row in body_rows:
        draw.text((margin, y), row, font=line_font, fill=LINE_COLOR)
        y += line_step
    if card.footer:
        footer_y = height - margin * 0.6 - FOOTER_SIZE * scale * 1.2
        draw.text((margin, footer_y), card.footer, font=footer_font, fill=FOOTER_COLOR)
    return image


def write_card_png(card, size, path):
    draw_card(card, size).save(path, format="PNG")
    return path


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
