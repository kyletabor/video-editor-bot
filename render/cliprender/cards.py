"""Draw contract cards (intro, opening, chapter, closing, outro slides) and encode them as MP4.

Why this lives in the renderer: the contract deliberately exchanges no image files, so a
plan stays a small JSON document that any bot can produce. The renderer owns the look of a
card. Text is set in Pillow's bundled default font (Aileron, via FreeType) so no font file
has to exist on Windows, macOS or Linux; a card is sized to the reel's own frame so the
concatenation never has to scale it.

Why the text is laid out before it is drawn: a card is on screen for a few seconds and the
viewer cannot scroll, so a clipped or ellipsized title reads as a broken sentence. Every
contract-valid card (title of at most 80 characters, at most four lines of at most 120) must
therefore be shown in full on every contract aspect (16:9, 9:16, 1:1). `layout` gives the
title up to three rows and each line up to three, wraps on word boundaries only and, when
text still does not fit, shrinks the font in 12 % steps (100 → 88 → 76 … → 40 %): first each
block on its own for width, then for height until the block ends above the footer band. The
ellipsis survives only as a last resort for text outside the contract or frames narrower than
1:1; the tests render the contract's extremes to prove that valid text never reaches it.

Contract v1.4 lets a card carry one picture, a PNG/JPEG (`image`) or a URL drawn as a QR code
(`qr`). The picture takes room from the text, so the same layout runs on a narrower column
(16:9: the text keeps the left 55 % of the frame and the picture sits right of it) or a
shorter one (9:16 and 1:1: the text may use at most `TEXT_SHARE` of the height above the
footer band, the picture fills what the text leaves below it). The shrink steps absorb the
difference, so contract-valid text is still never clipped; the extremes tests run with a
picture too.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import segno
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from .media import RenderError

BACKGROUND = (18, 21, 26)
TITLE_COLOR = (245, 246, 248)
LINE_COLOR = (200, 206, 214)
FOOTER_COLOR = (132, 141, 153)
ACCENT = (86, 156, 214)
# Layout is proportional to a 1080-pixel short edge, so 16:9, 9:16 and 1:1 frames all fit.
TITLE_SIZE, LINE_SIZE, FOOTER_SIZE = 88, 46, 30
TITLE_LEADING, LINE_LEADING, FOOTER_LEADING = 1.2, 1.45, 1.2
MAX_TITLE_ROWS, MAX_ROWS_PER_LINE, MAX_LINES = 3, 3, 4
# Font scales tried in turn when text does not fit at the base size, each step 12 % smaller.
# The floor still sets a 35 px title on a 1080p frame, larger than the footer's 30 px.
SHRINK_STEPS = (1.0, 0.88, 0.76, 0.64, 0.52, 0.40)
ELLIPSIS = "..."
# v1.4 pictures. On 16:9 the text column ends at this share of the frame width.
TEXT_SPLIT = 0.55
# On 9:16 and 1:1 the text may use at most this share of the height between the top margin and
# the footer band; the picture gets the rest (more when the text is short).
TEXT_SHARE = 0.6
PICTURE_BORDER = (70, 78, 90)
QR_DARK, QR_LIGHT = (0, 0, 0), (255, 255, 255)
# The QR specification asks for a light margin of four modules around the symbol.
QR_QUIET = 4
URL_SIZE = 30
PICTURE_FORMATS = {"PNG", "JPEG"}


@dataclass(frozen=True)
class Card:
    """One slide as the contract describes it, plus the renderer's footer text."""

    title: str
    lines: tuple[str, ...] = ()
    seconds: Fraction = Fraction(3)
    footer: str = ""
    # v1.4: at most one picture, a resolved PNG/JPEG path or a URL to draw as a QR code.
    image: Path | None = None
    qr: str | None = None

    @property
    def has_picture(self):
        return self.image is not None or self.qr is not None

    @classmethod
    def from_plan(cls, spec, footer="", resolve=None):
        """`resolve` turns the plan's `image` path into an absolute one (the renderer's
        repo-root rule); without it the path is taken as written."""
        image = spec.get("image")
        if image is not None:
            image = resolve(image) if resolve else Path(image)
        return cls(
            spec["title"],
            tuple(spec.get("lines", [])),
            Fraction(str(spec.get("seconds", 3))),
            footer,
            image,
            spec.get("qr"),
        )


@dataclass(frozen=True)
class Layout:
    """Where every piece of a card's text goes, resolved by `layout` before anything is drawn.

    Tests check this instead of pixels: `title_scale` and `line_scale` are the shrink steps
    that were needed (1.0 is the base size), the rows are exactly what will be drawn, and
    `bottom <= limit` is the promise that the text block stays clear of the footer band.
    """

    size: tuple[int, int]
    margin: int
    column: int
    title_font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    title_scale: float
    title_rows: tuple[str, ...]
    title_step: float
    line_font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    line_scale: float
    line_rows: tuple[tuple[str, ...], ...]
    line_step: float
    gap: float
    top: float
    limit: float
    footer_font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    footer_y: float
    # v1.4: (left, top, right, bottom) the picture is fitted into, or None without one.
    picture: tuple[int, int, int, int] | None = None

    @property
    def body_rows(self):
        """Every row of every line, in drawing order."""
        return tuple(row for rows in self.line_rows for row in rows)

    @property
    def block(self):
        """Height of title rows, gap and body rows, including the last row's leading."""
        return (
            len(self.title_rows) * self.title_step + self.gap + len(self.body_rows) * self.line_step
        )

    @property
    def bottom(self):
        return self.top + self.block


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


def wrap(text, font, max_width, break_words=False):
    """Greedy word wrap into rows no wider than `max_width`, breaking on word boundaries only.

    A word wider than the column stays whole (and its row overflows) unless `break_words` is
    set, which splits it at characters. `fit_block` asks for that only at the smallest shrink
    step: plan authors cannot see the frame, and a split word still beats a clipped one.
    """
    pieces = []
    for word in text.split():
        if break_words:
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
    return rows


def fits(rows, font, max_width, max_rows):
    """True when the rows are few enough and none is wider than the column."""
    return len(rows) <= max_rows and all(font.getlength(row) <= max_width for row in rows)


def fit_block(texts, base_size, column, max_rows, start=0):
    """Wrap every text at the largest shrink step (from `start`) where all fit `max_rows` rows.

    Returns `(step index, font, rows per text)`. The texts share one font because they are one
    block (the title, or the lines); rows break on word boundaries only. When even the
    smallest step does not fit, words wider than the column are split and rows beyond
    `max_rows` end in an ellipsis: a fallback that text within the contract never reaches on
    a contract aspect, so a viewer only ever sees it for an over-long plan or an odd frame.
    """
    last = len(SHRINK_STEPS) - 1
    for index in range(min(start, last), last + 1):
        font = load_font(base_size * SHRINK_STEPS[index])
        wrapped = [wrap(text, font, column) for text in texts]
        if all(fits(rows, font, column, max_rows) for rows in wrapped):
            return index, font, wrapped
    font = load_font(base_size * SHRINK_STEPS[last])
    wrapped = []
    for text in texts:
        rows = wrap(text, font, column, break_words=True)
        if len(rows) > max_rows:
            rows = rows[:max_rows]
            rows[-1] = _ellipsize(rows[-1], font, column)
        wrapped.append(rows)
    return last, font, wrapped


def layout(card, size):
    """Resolve fonts, rows and positions for `card` on a frame of `size` (width, height).

    Width first: the title shrinks until it fits `MAX_TITLE_ROWS` rows, the lines (as one
    block) until each fits `MAX_ROWS_PER_LINE`. Height second: while the block would run into
    the footer band, the lines shrink a step, being the bulk of the text, and the title follows
    once the lines have fallen two steps behind it, so the hierarchy survives without the
    title paying for text it did not cause. The block sits slightly above center when it is
    short and starts at the top margin when it is tall.
    """
    width, height = size
    if min(width, height) < 2:
        raise ValueError("Card frames must be at least 2 by 2 pixels")
    scale = min(width, height) / 1080
    margin = round(width * 0.08)
    column = max(1, width - 2 * margin)
    gap = LINE_SIZE * scale
    top_margin = margin * 0.5
    footer_font = load_font(FOOTER_SIZE * scale)
    footer_y = height - margin * 0.6 - FOOTER_SIZE * scale * FOOTER_LEADING
    # The band is reserved whether or not this card has a footer, so every card shares one
    # rhythm and a chapter card never sits differently from the intro before it.
    limit = footer_y - gap * 0.5
    picture_limit = limit
    beside = card.has_picture and width > height
    if beside:
        # Text left of the split, the picture right of it, half a margin of gutter each side.
        split = round(width * TEXT_SPLIT)
        column = max(1, split - margin - round(margin * 0.5))
    elif card.has_picture:
        limit = top_margin + (limit - top_margin) * TEXT_SHARE
    lines = card.lines[:MAX_LINES]
    last = len(SHRINK_STEPS) - 1
    title_size, line_size = TITLE_SIZE * scale, LINE_SIZE * scale

    title_index, title_font, title_rows = fit_block(
        [card.title], title_size, column, MAX_TITLE_ROWS
    )
    line_index, line_font, line_rows = fit_block(lines, line_size, column, MAX_ROWS_PER_LINE)

    def resolve(top, picture=None):
        return Layout(
            size=(width, height),
            margin=margin,
            column=column,
            title_font=title_font,
            title_scale=SHRINK_STEPS[title_index],
            title_rows=tuple(title_rows[0]),
            title_step=title_size * SHRINK_STEPS[title_index] * TITLE_LEADING,
            line_font=line_font,
            line_scale=SHRINK_STEPS[line_index],
            line_rows=tuple(tuple(rows) for rows in line_rows),
            line_step=line_size * SHRINK_STEPS[line_index] * LINE_LEADING,
            gap=gap,
            top=top,
            limit=limit,
            footer_font=footer_font,
            footer_y=footer_y,
            picture=picture,
        )

    result = resolve(top_margin)
    while result.block > limit - top_margin and (title_index < last or line_index < last):
        if line_index < last and (line_index - title_index < 2 or title_index == last):
            line_index, line_font, line_rows = fit_block(
                lines, line_size, column, MAX_ROWS_PER_LINE, start=line_index + 1
            )
        else:
            title_index, title_font, title_rows = fit_block(
                [card.title], title_size, column, MAX_TITLE_ROWS, start=title_index + 1
            )
        result = resolve(top_margin)
    if card.has_picture and not beside:
        # Text from the top margin, the picture in everything it leaves above the footer band.
        top = round(result.bottom + gap)
        box = (margin, top, width - margin, round(picture_limit))
        return resolve(top_margin, box)
    # Slightly above center reads as centered once the footer sits at the bottom; a tall
    # block starts at the top margin instead and never crosses `limit`.
    centered = (height - result.block) / 2 - height * 0.04
    box = None
    if beside:
        box = (split + round(margin * 0.5), round(top_margin), width - margin, round(limit))
    return resolve(max(top_margin, min(centered, limit - result.block)), box)


def draw_card(card, size):
    """Paint a card into an RGB image of exactly `size` (width, height)."""
    plan = layout(card, size)
    width, height = plan.size
    scale = min(width, height) / 1080
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    margin, y = plan.margin, plan.top
    for row in plan.title_rows:
        draw.text((margin, y), row, font=plan.title_font, fill=TITLE_COLOR)
        y += plan.title_step
    gap = plan.gap
    draw.rectangle(
        (margin, y + gap * 0.3, margin + max(2, 120 * scale), y + gap * 0.3 + max(1, 5 * scale)),
        fill=ACCENT,
    )
    y += gap
    for row in plan.body_rows:
        draw.text((margin, y), row, font=plan.line_font, fill=LINE_COLOR)
        y += plan.line_step
    if card.footer:
        draw.text((margin, plan.footer_y), card.footer, font=plan.footer_font, fill=FOOTER_COLOR)
    if card.image is not None:
        draw_image(image, card.image, plan.picture)
    elif card.qr is not None:
        draw_qr(image, qr_placement(card.qr, plan.picture, scale))
    return image


def open_picture(path):
    """The card image as RGB, or a RenderError naming the file.

    A transparent PNG is flattened onto the card background, so its empty areas read as part
    of the slide rather than as black. Only PNG and JPEG are accepted, as the contract says.
    """
    try:
        with Image.open(path) as source:
            if source.format not in PICTURE_FORMATS:
                raise RenderError(f"Card image {path} is {source.format}; use a PNG or JPEG file")
            source.load()
            if source.mode in ("RGBA", "LA") or "transparency" in source.info:
                rgba = source.convert("RGBA")
                flat = Image.new("RGB", rgba.size, BACKGROUND)
                flat.paste(rgba, mask=rgba.getchannel("A"))
                return flat
            return source.convert("RGB")
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise RenderError(f"Cannot read card image {path}: {exc}") from exc


def check_pictures(cards):
    """Open every card image once before any encode, so a missing or broken file fails the
    run with its path instead of after the clips have been rendered."""
    for card in cards:
        if card.image is not None:
            if not Path(card.image).is_file():
                raise RenderError(
                    f"Missing card image: {card.image}; provide a readable PNG or JPEG file"
                )
            open_picture(card.image)


def fit_box(size, box):
    """(left, top, width, height) of `size` scaled to fit `box`, centered; never cropped."""
    left, top, right, bottom = box
    width, height = size
    factor = min((right - left) / width, (bottom - top) / height)
    fitted = (max(1, round(width * factor)), max(1, round(height * factor)))
    x = left + (right - left - fitted[0]) // 2
    y = top + (bottom - top - fitted[1]) // 2
    return x, y, fitted[0], fitted[1]


def draw_image(canvas, path, box):
    """Paste the card image scaled to fit `box` (inside a 2 px border), never cropped."""
    picture = open_picture(path)
    border = 2
    inner = (box[0] + border, box[1] + border, box[2] - border, box[3] - border)
    x, y, width, height = fit_box(picture.size, inner)
    canvas.paste(picture.resize((width, height), Image.Resampling.LANCZOS), (x, y))
    ImageDraw.Draw(canvas).rectangle(
        (x - border, y - border, x + width + border - 1, y + height + border - 1),
        outline=PICTURE_BORDER,
        width=border,
    )


def qr_matrix(url):
    """The QR symbol for `url` as rows of booleans (True = dark), without the quiet zone.

    Error correction M survives a phone camera at an angle and a little video compression; the
    version is the smallest that holds the URL. segno is pure Python, so no system library is
    needed on any renderer host.
    """
    symbol = segno.make(url, error="m", micro=False)
    return [[bool(cell) for cell in row] for row in symbol.matrix]


@dataclass(frozen=True)
class QrPlacement:
    """Where a QR card's symbol and URL go. `x`, `y` and `side` describe the white square
    (symbol plus quiet zone); every module is `module` whole pixels so edges stay crisp."""

    matrix: tuple[tuple[bool, ...], ...]
    x: int
    y: int
    module: int
    side: int
    url_font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    url_rows: tuple[str, ...]
    url_y: int
    url_step: float

    @property
    def symbol_origin(self):
        """Top-left pixel of the first module."""
        return self.x + QR_QUIET * self.module, self.y + QR_QUIET * self.module


def qr_placement(url, box, scale):
    """Fit the QR square and the URL printed under it into `box`.

    The URL is wrapped by characters (it has no spaces) at the footer's size, shrinking until
    it takes at most four rows, so a 300-character URL still prints whole. The square gets the
    rest of the box's height, rounded down to whole pixels per module.
    """
    matrix = tuple(tuple(row) for row in qr_matrix(url))
    left, top, right, bottom = box
    width = right - left
    for step in SHRINK_STEPS:
        font = load_font(URL_SIZE * scale * step)
        rows = wrap(url, font, width, break_words=True)
        if len(rows) <= 4:
            break
    url_step = URL_SIZE * scale * step * FOOTER_LEADING
    url_height = len(rows) * url_step
    gap = round(URL_SIZE * scale * 0.6)
    modules = len(matrix) + 2 * QR_QUIET
    available = min(width, bottom - top - url_height - gap)
    module = max(1, int(available // modules))
    side = module * modules
    block = side + gap + url_height
    x = left + (width - side) // 2
    y = top + max(0, round((bottom - top - block) / 2))
    return QrPlacement(
        matrix, x, y, module, side, font, tuple(rows), round(y + side + gap), url_step
    )


def draw_qr(canvas, place):
    """White square, dark modules, then the URL centred under it."""
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((place.x, place.y, place.x + place.side - 1, place.y + place.side - 1), QR_LIGHT)
    x0, y0 = place.symbol_origin
    for r, row in enumerate(place.matrix):
        for c, dark in enumerate(row):
            if dark:
                x, y = x0 + c * place.module, y0 + r * place.module
                draw.rectangle((x, y, x + place.module - 1, y + place.module - 1), QR_DARK)
    y = place.url_y
    centre = place.x + place.side / 2
    for row in place.url_rows:
        draw.text(
            (centre - place.url_font.getlength(row) / 2, y),
            row,
            font=place.url_font,
            fill=LINE_COLOR,
        )
        y += place.url_step


def write_card_png(card, size, path):
    """The card as a PNG; `cliprender.reel` turns it into a reel piece directly (the image
    looped at the reel's frame rate over generated silence), so a card is encoded once."""
    draw_card(card, size).save(path, format="PNG")
    return path
