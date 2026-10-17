"""Art of Intelligence: Day One AI's on-air package as a card theme (`--theme aoi`).

Source: the "Art of Intelligence" design canvas, built on the Day One AI design system
(2026-10-08), boards "Title card", "Camera overlays", "Screen-share layout", "End card" and
"Transition system". Colours, type roles and proportions are read from those boards at
1920 x 1080; every size here is that board's pixel size times `scale`, the frame's short edge
over 1080, so a 9:16 or 1:1 card keeps the same hierarchy.

What each reel card becomes:

- intro: the title card. Brand header, the sun mark and series line, the "Art of
  Intelligence." lockup (the last word in the sun gradient), the episode chip beside the
  plan's intro title, the intro lines as a mono dateline, and the sun rising on the horizon.
- chapter: "CHAPTER 02 / 07" with the progress bar from the overlay board, the title, a short
  horizon rule and the lines. The header names the episode and where the clip starts in the
  session, which is what the default look's footer said.
- opening, closing, and v1.4 slides before a clip: the same layout with the episode bug (sun
  mark, AOI, episode chip) in place of the counter.
- outro: the end card. The title with its last row in the sun gradient, the lines, the Day One
  AI lockup at the foot and the sun set behind the horizon.

Every card has the navy ground with the faint 64 px grid fading out from one side, the brand
header and the 4 px horizon band with its glow at the bottom edge. The text rules are the
default look's (cards.py): the plan's words are shown whole, wrapped on word boundaries and
shrunk in the same steps when they do not fit, so contract-valid text is never clipped.

The design names CSS system stacks rather than font files; `fonts.py` resolves them on the
host (Segoe UI and Cascadia Code on Windows). Burned captions and labels take the overlay
board's look: light text on a navy panel.
"""

from __future__ import annotations

import contextlib
import functools
import itertools
import math
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from ..cards import (
    SHRINK_STEPS,
    draw_image,
    draw_qr,
    fit_block,
    qr_placement,
    timestamp_label,
    wrap,
)
from ..overlays import LabelLook
from . import Theme
from .fonts import family, font

# Palette, as the boards use it.
NAVY_900 = (10, 22, 38)  # #0a1626, ground top
NAVY_850 = (12, 26, 43)  # #0c1a2b, ground bottom
EDGE = (36, 66, 95)  # #24425f, unlit progress
INK = (234, 241, 248)  # #eaf1f8
MUTED = (157, 179, 201)  # #9db3c9
DIM = (111, 137, 164)  # #6f89a4
SUN = (247, 183, 51)  # #f7b733, sun-1
AMBER = (245, 166, 35)  # #f5a623
ORANGE = (232, 89, 12)  # #e8590c
# The 4 px horizon band, left to right: orange, amber at 45 %, sun.
HORIZON = ((0.0, ORANGE), (0.45, AMBER), (1.0, SUN))
# Display text in the sun gradient (92deg, about left to right).
SUNLIGHT = ((0.0, SUN), (1.0, ORANGE))
# Grid lines: rgba(157,179,201,.16) on a layer at 50 % opacity.
GRID_ALPHA = round(255 * 0.16 * 0.5)
GRID_STEP = 64
# Radial fade of the grid (CSS mask): opaque to 15 % of the radius, gone at 72 %.
GRID_INNER, GRID_OUTER = 0.15, 0.72
# Pillow's radial gradient reads 0 at the centre and rises 181 per unit of its 128 px radius.
RADIAL_UNIT = 128 * math.sqrt(2)

KEYS = ("episode", "date")
EPISODE_LIMIT = 4
DATE_LIMIT = 40
MAX_TITLE_ROWS, MAX_ROWS_PER_LINE, MAX_LINES = 3, 3, 4
SUPERSAMPLE = 4


@dataclass(frozen=True)
class Values:
    """Per-video values from the theme file; both optional."""

    episode: str | None = None
    date: str | None = None


def parse_values(values):
    unknown = sorted(set(values) - set(KEYS))
    if unknown:
        raise ValueError(f"unknown key(s) {', '.join(unknown)}; known: {', '.join(KEYS)}")
    parsed = {}
    for key, limit in (("episode", EPISODE_LIMIT), ("date", DATE_LIMIT)):
        value = values.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key}: expected non-empty text")
        value = " ".join(value.split())
        if len(value) > limit:
            raise ValueError(f"{key}: {len(value)} characters, at most {limit} fit")
        parsed[key] = value
    return Values(**parsed)


def theme(values):
    """The `Theme` for `cliprender.themes.load_theme`."""
    parsed = parse_values(values)
    return Theme(
        name="aoi",
        draw_card=functools.partial(draw_card, values=parsed),
        caption_style=caption_style(),
        label_look=label_look(),
    )


def caption_style():
    """Captions as the overlay board sets text over a shot: bold ink on a navy panel.

    FFmpeg converts SRT to ASS with a 288-unit-high script, so the size and margins are in
    those units (15 is about 56 px on 1080p). BorderStyle 3 draws the opaque box in the
    outline colour; alpha 0x20 leaves it about 87 % opaque, the board's rgba(10,22,38,.86).
    """
    parts = []
    name = family("bold")
    if name:
        parts.append(f"FontName={name}")
    parts += [
        "Bold=-1",
        "Fontsize=15",
        "PrimaryColour=&H00F8F1EA",
        "OutlineColour=&H2026160A",
        "BackColour=&H2026160A",
        "BorderStyle=3",
        "Outline=2",
        "Shadow=0",
        "MarginV=14",
    ]
    return ",".join(parts)


def label_look():
    return LabelLook(
        font=family("bold") or LabelLook().font,
        text="&H00F8F1EA",
        box="&H2026160A",
        bold=True,
    )


# ---------------------------------------------------------------------------------------------
# Drawing primitives. Cards are drawn in RGBA and flattened at the end.


def scale_of(size):
    return min(size) / 1080


def blend(stops, position):
    """The colour at `position` (0..1) along gradient `stops`."""
    position = min(1.0, max(0.0, position))
    for (left, a), (right, b) in itertools.pairwise(stops):
        if position <= right:
            share = 0.0 if right == left else (position - left) / (right - left)
            return tuple(round(a[i] + (b[i] - a[i]) * share) for i in range(3))
    return stops[-1][1]


def hgradient(width, height, stops):
    """An RGB image `width` x `height` running through `stops` from left to right."""
    width, height = max(1, int(width)), max(1, int(height))
    row = Image.new("RGB", (width, 1))
    row.putdata([blend(stops, x / max(1, width - 1)) for x in range(width)])
    return row.resize((width, height), Image.Resampling.NEAREST)


def diagonal_gradient(width, height, start, end):
    """`start` at the top left to `end` at the bottom right (CSS 135deg)."""
    width, height = max(1, int(width)), max(1, int(height))
    down = Image.linear_gradient("L")  # 0 at the top, 255 at the bottom
    across = down.transpose(Image.Transpose.TRANSPOSE)  # 0 at the left, 255 at the right
    mask = ImageChops.add(across.resize((width, height)), down.resize((width, height)), scale=2)
    return Image.composite(
        Image.new("RGB", (width, height), end), Image.new("RGB", (width, height), start), mask
    )


def radial_fade(size, focus):
    """An L mask: 255 near `focus` (fractions of the frame), fading to 0 like the boards'
    `radial-gradient(ellipse at X Y, #000 15%, transparent 72%)` (farthest-corner ellipse)."""
    width, height = size
    cx, cy = width * focus[0], height * focus[1]
    rx = math.sqrt(2) * max(cx, width - cx)
    ry = math.sqrt(2) * max(cy, height - cy)

    def level(value):
        distance = value / RADIAL_UNIT
        if distance <= GRID_INNER:
            return 255
        if distance >= GRID_OUTER:
            return 0
        return round(255 * (GRID_OUTER - distance) / (GRID_OUTER - GRID_INNER))

    ellipse = Image.radial_gradient("L").point(level)
    ellipse = ellipse.resize((max(1, round(2 * rx)), max(1, round(2 * ry))))
    mask = Image.new("L", size, 0)
    mask.paste(ellipse, (round(cx - rx), round(cy - ry)))
    return mask


def ground(size, focus):
    """Navy ground, top to bottom, with the 64 px grid fading out from `focus`."""
    width, height = size
    scale = scale_of(size)
    ramp = Image.linear_gradient("L").resize(size)
    image = Image.composite(
        Image.new("RGB", size, NAVY_850), Image.new("RGB", size, NAVY_900), ramp
    )
    lines = Image.new("L", size, 0)
    draw = ImageDraw.Draw(lines)
    step, weight = GRID_STEP * scale, max(1, round(scale))
    x = 0.0
    while x < width:
        draw.rectangle((round(x), 0, round(x) + weight - 1, height), fill=GRID_ALPHA)
        x += step
    y = 0.0
    while y < height:
        draw.rectangle((0, round(y), width, round(y) + weight - 1), fill=GRID_ALPHA)
        y += step
    layer = Image.new("RGBA", size, MUTED + (0,))
    layer.putalpha(ImageChops.multiply(lines, radial_fade(size, focus)))
    image = image.convert("RGBA")
    image.alpha_composite(layer)
    return image


def composite(image, tile, xy):
    """alpha_composite `tile` at `xy`, clipped to the image (Pillow refuses a negative
    destination, and the sun and the band's glow run off the frame on purpose)."""
    x, y = round(xy[0]), round(xy[1])
    left, top = max(0, -x), max(0, -y)
    right = min(tile.width, image.width - x)
    bottom = min(tile.height, image.height - y)
    if left >= right or top >= bottom:
        return
    image.alpha_composite(tile.crop((left, top, right, bottom)), (x + left, y + top))


def first_baseline(face, step):
    """Offset from a row's top to its baseline when rows are `step` apart, as CSS centres
    the font's ascent plus descent in the line box."""
    ascent, descent = face.getmetrics()
    return (step - (ascent + descent)) / 2 + ascent


def supersampled(width, height, paint):
    """An RGBA tile `width` x `height` painted by `paint(draw, factor)` at SUPERSAMPLE times
    the size and scaled down, so curves and rounded corners come out anti-aliased."""
    factor = SUPERSAMPLE
    big = Image.new("RGBA", (max(1, round(width * factor)), max(1, round(height * factor))))
    paint(ImageDraw.Draw(big), factor)
    return big.resize((max(1, round(width)), max(1, round(height))), Image.Resampling.LANCZOS)


def sun_mark(side):
    """The Day One mark at `side` pixels: the sun on the horizon bar with three rays
    (viewBox 0 0 100 100 on the boards)."""

    def paint(draw, factor):
        unit = side * factor / 100
        draw.ellipse((24 * unit, 32 * unit, 76 * unit, 84 * unit), fill=ORANGE)
        draw.rounded_rectangle((10 * unit, 58 * unit, 90 * unit, 66 * unit), 4 * unit, fill=AMBER)
        for (x0, y0), (x1, y1) in (
            ((50, 14), (50, 26)),
            ((24, 26), (32, 34)),
            ((76, 26), (68, 34)),
        ):
            draw.line(
                (x0 * unit, y0 * unit, x1 * unit, y1 * unit), fill=AMBER, width=round(8 * unit)
            )
            for x, y in ((x0, y0), (x1, y1)):
                draw.ellipse(
                    ((x - 4) * unit, (y - 4) * unit, (x + 4) * unit, (y + 4) * unit), fill=AMBER
                )

    return supersampled(side, side, paint)


def width_of(face, text, tracking=0.0):
    return face.getlength(text) + tracking * max(0, len(text) - 1)


class Tracked:
    """A face with letter-spacing, measured as `put` draws it, so `cards.wrap` and
    `cards.fit_block` (which only call `getlength`) wrap tracked text correctly."""

    def __init__(self, face, share):
        self.face, self.tracking = face, share * face.size

    @property
    def size(self):
        return self.face.size

    def getlength(self, text):
        return width_of(self.face, text, self.tracking)

    def getmetrics(self):
        return self.face.getmetrics()


@dataclass(frozen=True)
class Drawn:
    """One run of text as drawn: its words and its box on the card (for tests)."""

    text: str
    box: tuple[float, float, float, float]


_drawn = None


@contextlib.contextmanager
def recording():
    """Collect a `Drawn` for every run of text drawn inside the block. The theme's tests use
    it to prove that a card shows the plan's words whole and inside the frame."""
    global _drawn
    _drawn = []
    try:
        yield _drawn
    finally:
        _drawn = None


def _record(x, baseline, text, face, tracking):
    if _drawn is None:
        return
    _, top, _, bottom = face.getbbox(text, anchor="ls")
    _drawn.append(
        Drawn(text, (x, baseline + top, x + width_of(face, text, tracking), baseline + bottom))
    )


def _letters(draw, x, baseline, text, face, fill, tracking):
    if not tracking:
        draw.text((x, baseline), text, font=face, fill=fill, anchor="ls")
        return
    for index, letter in enumerate(text):
        offset = face.getlength(text[:index]) + tracking * index
        draw.text((x + offset, baseline), letter, font=face, fill=fill, anchor="ls")


def put(draw, x, baseline, text, face, fill, tracking=0.0):
    """Draw `text` with its baseline at `baseline`; `tracking` px of extra space between
    letters (Pillow has no letter-spacing, so tracked text is set letter by letter at the
    offsets the whole prefix measures, which keeps kerning)."""
    _record(x, baseline, text, face, tracking)
    _letters(draw, x, baseline, text, face, fill, tracking)


def put_sunlit(image, x, baseline, text, face, tracking=0.0):
    """Draw `text` filled with the sun gradient across its own width."""
    _, top, right, bottom = face.getbbox(text, anchor="ls")
    span = width_of(face, text, tracking)
    pad = round(face.size * 0.2)
    box = (round(x) - pad, round(baseline + top) - pad)
    size = (
        round(span + max(0, right - face.getlength(text))) + 2 * pad,
        round(bottom - top) + 2 * pad,
    )
    mask = Image.new("L", size, 0)
    _record(x, baseline, text, face, tracking)
    _letters(ImageDraw.Draw(mask), x - box[0], baseline - box[1], text, face, 255, tracking)
    fill = hgradient(size[0], size[1], ((0.0, SUN), (pad / size[0], SUN), (1.0, ORANGE)))
    tile = fill.convert("RGBA")
    tile.putalpha(mask)
    composite(image, tile, box)


def rule(image, x0, y0, x1, thickness, glow):
    """The horizon band from x0 to x1 at y0: a soft amber glow under the orange-to-sun line
    (the boards' `box-shadow: 0 0 34px 3px rgba(245,166,35,.35)`)."""
    width = round(x1 - x0)
    if width < 1:
        return
    spread = round(glow * 2)
    halo = Image.new("RGBA", (width + 2 * spread, round(thickness) + 2 * spread), AMBER + (0,))
    core = Image.new("L", halo.size, 0)
    ImageDraw.Draw(core).rectangle(
        (
            spread - glow * 0.1,
            spread - glow * 0.1,
            spread + width + glow * 0.1,
            spread + thickness + glow * 0.1,
        ),
        fill=round(255 * 0.35),
    )
    halo.putalpha(core.filter(ImageFilter.GaussianBlur(glow / 2)))
    composite(image, halo, (round(x0) - spread, round(y0) - spread))
    band = hgradient(width, max(1, round(thickness)), HORIZON).convert("RGBA")
    composite(image, band, (x0, y0))


def chip(text, face, pad_x, pad_y, radius):
    """The episode chip: dark mono numerals on the sun gradient, rounded."""
    ascent, descent = face.getmetrics()
    width = width_of(face, text) + 2 * pad_x
    height = ascent + descent + 2 * pad_y
    fill = diagonal_gradient(width, height, SUN, ORANGE).convert("RGBA")
    shape = supersampled(
        width,
        height,
        lambda d, f: d.rounded_rectangle(
            (0, 0, width * f - 1, height * f - 1), radius * f, fill=(255, 255, 255, 255)
        ),
    )
    fill.putalpha(shape.getchannel("A"))
    put(ImageDraw.Draw(fill), pad_x, pad_y + ascent, text, face, NAVY_900)
    return fill


def bug(values, scale):
    """The episode bug: a navy pill with the sun mark, AOI and the episode chip."""
    mark = sun_mark(40 * scale)
    label = font("mono_bold", 22 * scale)
    tracking = 0.2 * 22 * scale
    number = None
    if values.episode:
        number = chip(
            values.episode, font("mono_bold", 20 * scale), 10 * scale, 6 * scale, 8 * scale
        )
    gap = 14 * scale
    inner = mark.width + gap + width_of(label, "AOI", tracking)
    if number is not None:
        inner += gap + number.width
    height = max(mark.height, number.height if number else 0) + 24 * scale
    width = inner + 14 * scale + 18 * scale

    def paint(draw, factor):
        draw.rounded_rectangle(
            (0, 0, width * factor - 1, height * factor - 1),
            height * factor / 2,
            fill=NAVY_900 + (219,),
            outline=MUTED + (41,),
            width=max(1, round(factor * scale)),
        )

    pill = supersampled(width, height, paint)
    x = round(14 * scale)
    pill.alpha_composite(mark, (x, round((height - mark.height) / 2)))
    x += mark.width + gap
    ascent, descent = label.getmetrics()
    put(ImageDraw.Draw(pill), x, (height + ascent - descent) / 2, "AOI", label, INK, tracking)
    if number is not None:
        x += width_of(label, "AOI", tracking) + gap
        pill.alpha_composite(number, (round(x), round((height - number.height) / 2)))
    return pill


def header(image, values, right, scale, margin):
    """The brand line across the top: "DAY ONE AI × ART OF INTELLIGENCE" with ONE in amber,
    and `right` (episode and date, or where the clip starts) at the other side when it fits."""
    draw = ImageDraw.Draw(image)
    width = image.width
    size = 20 * scale
    tracking = 0.16 * size
    plain, strong = font("mono", size), font("mono_bold", size)
    baseline = 56 * scale + size * 0.85
    parts = (
        ("DAY ", plain, DIM),
        ("ONE", strong, AMBER),
        (" AI × ART OF INTELLIGENCE", plain, DIM),
    )
    x = margin
    for text, face, fill in parts:
        put(draw, x, baseline, text, face, fill, tracking)
        x += width_of(face, text, tracking) + tracking
    if right:
        right = right.upper()
        span = width_of(plain, right, tracking)
        if x + 40 * scale + span <= width - margin:
            put(draw, width - margin - span, baseline, right, plain, DIM, tracking)
    return baseline + size * 0.4


def bottom_band(image, scale):
    rule(image, 0, image.height - 4 * scale, image.width, 4 * scale, 34 * scale)


def sun(image, centre, radius, rays, scale):
    """The sun on the horizon: a disc in the sun gradient (92 % opaque), clipped by the frame,
    with the five rays of the title card when `rays`."""
    cx, cy = centre
    box = (round(cx - radius), round(cy - radius))
    side = round(2 * radius)
    disc = diagonal_gradient(side, side, SUN, ORANGE).convert("RGBA")
    shape = supersampled(
        side,
        side,
        lambda d, f: d.ellipse((0, 0, side * f - 1, side * f - 1), fill=(255, 255, 255, 235)),
    )
    disc.putalpha(shape.getchannel("A"))
    composite(image, disc, box)
    if not rays:
        return
    layer = Image.new("RGBA", image.size, AMBER + (0,))
    draw = ImageDraw.Draw(layer)
    weight = max(2, round(14 * scale))
    unit = radius / 340
    # The title card's rays, relative to the centre, in board pixels at a 340 px radius.
    for (x0, y0), (x1, y1) in (
        ((-338, -195), (-390, -225)),
        ((-224, -319), (-258, -369)),
        ((0, -390), (0, -450)),
        ((224, -319), (258, -369)),
        ((338, -195), (390, -225)),
    ):
        a = (cx + x0 * unit, cy + y0 * unit)
        b = (cx + x1 * unit, cy + y1 * unit)
        draw.line((a, b), fill=AMBER + (217,), width=weight)
        for x, y in (a, b):
            draw.ellipse(
                (x - weight / 2, y - weight / 2, x + weight / 2, y + weight / 2),
                fill=AMBER + (217,),
            )
    image.alpha_composite(layer)


def progress(image, x, y, width, chapter, scale):
    """The overlay board's chapter bar: done segments in sun, the current one in amber with a
    glow, the rest unlit."""
    index, count = chapter
    gap = 6 * scale
    height = max(2, round(5 * scale))
    segment = (width - gap * (count - 1)) / count
    if segment < 2 * scale:
        segment, gap = width / count, 0
    draw = ImageDraw.Draw(image)
    for k in range(1, count + 1):
        left = x + (k - 1) * (segment + gap)
        if k == index:
            rule(image, left, y, left + segment, height, 12 * scale)
            draw = ImageDraw.Draw(image)
            continue
        fill = SUN if k < index else EDGE
        draw.rounded_rectangle(
            (round(left), round(y), round(left + segment) - 1, round(y + height) - 1),
            radius=height / 2,
            fill=fill,
        )


# ---------------------------------------------------------------------------------------------
# Layout.


def margins(size):
    scale = scale_of(size)
    return scale, round(96 * scale)


def loader(role):
    return lambda size: font(role, size)


def fit_text_block(
    title,
    lines,
    column,
    available,
    title_size,
    line_size,
    title_role="display",
    line_role="regular",
    title_leading=1.04,
    line_leading=1.42,
    fixed=0.0,
):
    """Title rows and line rows that fit `column` wide and `available` high (`fixed` is the
    height of everything else in the block): the default look's shrink rule (cards.layout):
    each block first for width, then the lines a step at a time for height, the title
    following once the lines are two steps behind it."""
    last = len(SHRINK_STEPS) - 1
    t_index, t_face, t_rows = fit_block(
        [title], title_size, column, MAX_TITLE_ROWS, 0, loader(title_role)
    )
    l_index, l_face, l_rows = fit_block(
        lines, line_size, column, MAX_ROWS_PER_LINE, 0, loader(line_role)
    )

    def height():
        title_h = len(t_rows[0]) * title_size * SHRINK_STEPS[t_index] * title_leading
        body = sum(len(rows) for rows in l_rows) * line_size * SHRINK_STEPS[l_index] * line_leading
        return fixed + title_h + body

    while height() > available and (t_index < last or l_index < last):
        if lines and l_index < last and (l_index - t_index < 2 or t_index == last):
            l_index, l_face, l_rows = fit_block(
                lines, line_size, column, MAX_ROWS_PER_LINE, l_index + 1, loader(line_role)
            )
        else:
            t_index, t_face, t_rows = fit_block(
                [title], title_size, column, MAX_TITLE_ROWS, t_index + 1, loader(title_role)
            )
    return (
        t_face,
        balance(t_rows[0], t_face, column),
        title_size * SHRINK_STEPS[t_index] * title_leading,
        l_face,
        l_rows,
        line_size * SHRINK_STEPS[l_index] * line_leading,
        height(),
    )


def balance(rows, face, column):
    """The same words in the same number of rows, as even as they go (CSS `text-wrap:
    balance`): the narrowest column that still wraps into that many rows. A headline then
    never ends on one stranded word; the row count, and so the layout, does not change."""
    if len(rows) < 2:
        return rows
    text = " ".join(rows)
    low, high = 1, int(column)
    while low < high:
        middle = (low + high) // 2
        if len(wrap(text, face, middle)) <= len(rows):
            high = middle
        else:
            low = middle + 1
    balanced = wrap(text, face, high)
    return tuple(balanced) if len(balanced) == len(rows) else rows


def episode_label(values, at=None):
    parts = []
    if values.episode:
        parts.append(f"EP {values.episode}")
    if at is not None:
        parts.append(timestamp_label(at))
    elif values.date:
        parts.append(values.date)
    return " · ".join(parts)


def picture_box(card, size, margin, top, bottom):
    """Where a v1.4 picture goes, and the text column it leaves: right of the text on a wide
    frame, below it otherwise (cards.layout's split)."""
    width, height = size
    if not card.has_picture:
        return None, None
    if width > height:
        split = round(width * 0.55)
        return (
            split + margin // 2,
            round(top),
            width - margin,
            round(bottom),
        ), split - margin - margin // 2
    return "below", None


def draw_picture(image, card, box, scale):
    rgb = image.convert("RGB")
    if card.image is not None:
        draw_image(rgb, card.image, box)
    else:
        draw_qr(rgb, qr_placement(card.qr, box, scale))
    image.paste(rgb.convert("RGBA"))


def draw_card(card, size, values=None):
    """Paint `card` in the Art of Intelligence look into an RGB image of exactly `size`."""
    values = values or Values()
    width, height = size
    if min(width, height) < 2:
        raise ValueError("Card frames must be at least 2 by 2 pixels")
    if card.kind == "intro" and not card.has_picture:
        image = title_card(card, size, values)
    elif card.kind == "outro" and not card.has_picture:
        image = end_card(card, size, values)
    else:
        image = slide(card, size, values)
    return image.convert("RGB")


def title_card(card, size, values):
    """The episode open (board "Title card")."""
    width, height = size
    scale, margin = margins(size)
    wide = width > height
    image = ground(size, (0.35, 0.45))
    radius = 340 * scale * (1.0 if wide else 0.7)
    sun(image, (width - radius * 360 / 340, height - 4 * scale), radius, True, scale)
    top = header(image, values, episode_label(values), scale, margin)
    bottom = height - 40 * scale
    column = width - 2 * margin
    draw = ImageDraw.Draw(image)

    kicker_face = font("mono_bold", 24 * scale)
    kicker = "A DAY ONE AI SERIES · EVERY FRIDAY"
    kicker_tracking = 0.24 * kicker_face.size
    mark = sun_mark(52 * scale)
    while (
        mark.width + 20 * scale + width_of(kicker_face, kicker, kicker_tracking) > column
        and kicker_face.size > 8
    ):
        kicker_face = font("mono_bold", kicker_face.size * 0.88)
        kicker_tracking = 0.24 * kicker_face.size

    # The lockup shrinks until "Intelligence." fits the column.
    for step in SHRINK_STEPS:
        big = font("display", 168 * scale * step)
        if width_of(big, "Intelligence.", -0.03 * big.size) <= column:
            break
    small = font("mono", big.size * 0.4)
    lockup_step = big.size * 0.95

    chip_face = font("mono_bold", 40 * scale)
    number = (
        chip(values.episode, chip_face, 16 * scale, 10 * scale, 10 * scale)
        if values.episode
        else None
    )
    title_column = column
    if wide:
        # Keep the episode title clear of the sun.
        title_column = min(column, round(width * 0.62) - margin)
    if number is not None:
        title_column -= number.width + 24 * scale
    # The intro lines, as the board's mono dateline: wrapped (never shrunk to a sliver), at
    # most two rows each.
    _, dated, date_rows = fit_block(
        [line.upper() for line in card.lines[:MAX_LINES]],
        22 * scale,
        column,
        2,
        0,
        lambda size: Tracked(font("mono", size), 0.16),
    )
    dates = [row for rows in date_rows for row in rows]
    date_face, date_tracking = dated.face, dated.tracking
    date_step = date_face.size * 1.5

    fixed = (
        mark.height
        + 40 * scale
        + 2 * lockup_step
        + 48 * scale
        + 18 * scale
        + len(dates) * date_step
    )
    t_face, t_rows, t_step, _, _, _, block = fit_text_block(
        card.title,
        [],
        max(1, title_column),
        bottom - top - 40 * scale,
        54 * scale,
        1,
        title_role="bold",
        title_leading=1.1,
        fixed=fixed,
    )
    if number is not None:
        block = max(block, fixed + number.height)
    y = max(top + 40 * scale, (height - block) / 2 - height * 0.02) if wide else top + 60 * scale

    composite(image, mark, (margin, y))
    ascent, descent = kicker_face.getmetrics()
    put(
        draw,
        margin + mark.width + 20 * scale,
        y + (mark.height + ascent - descent) / 2,
        kicker,
        kicker_face,
        AMBER,
        kicker_tracking,
    )
    y += mark.height + 40 * scale

    baseline = y + first_baseline(big, lockup_step)
    put(draw, margin, baseline, "Art", big, INK, -0.03 * big.size)
    x = margin + width_of(big, "Art", -0.03 * big.size) + big.size * 0.16
    put(draw, x, baseline, "of", small, MUTED, 0.02 * small.size)
    baseline += lockup_step
    put_sunlit(image, margin, baseline, "Intelligence.", big, -0.03 * big.size)
    y += 2 * lockup_step + 48 * scale

    x = margin
    rows_top = y
    if number is not None:
        composite(image, number, (margin, y))
        x = margin + number.width + 24 * scale
        # Centre a one-row title on the chip; longer titles hang from its top.
        if len(t_rows) == 1:
            rows_top = y + (number.height - t_step) / 2
    draw = ImageDraw.Draw(image)
    baseline = rows_top + first_baseline(t_face, t_step)
    for row in t_rows:
        put(draw, x, baseline, row, t_face, INK, -0.02 * t_face.size)
        baseline += t_step
    y = max(rows_top + len(t_rows) * t_step, y + (number.height if number else 0)) + 18 * scale
    baseline = y + first_baseline(date_face, date_step)
    for line in dates:
        put(draw, margin, baseline, line, date_face, DIM, date_tracking)
        baseline += date_step
    bottom_band(image, scale)
    return image


def slide(card, size, values):
    """Chapter, opening, closing and section cards (and intro or outro cards with a picture)."""
    width, height = size
    scale, margin = margins(size)
    image = ground(size, (0.3, 0.5))
    right = episode_label(values, card.at) if card.kind == "chapter" else episode_label(values)
    top = header(image, values, right, scale, margin) + 48 * scale
    bottom = height - 64 * scale
    column = min(width - 2 * margin, round(1500 * scale))
    box, beside = picture_box(card, size, margin, top, bottom)
    if beside is not None:
        column = beside
    draw = ImageDraw.Draw(image)

    # The kicker: the chapter counter and bar, or the episode bug.
    if card.kind == "chapter" and card.chapter is not None:
        counter_face = font("mono_bold", 26 * scale)
        index, count = card.chapter
        counter = f"CHAPTER {index:02d} / {count:02d}"
        kicker_height = counter_face.size * 1.2 + 18 * scale + 5 * scale
    else:
        pill = bug(values, scale)
        kicker_height = pill.height
    gap_after_kicker = 44 * scale
    rule_block = 36 * scale + 4 * scale + 36 * scale
    available = bottom - top
    if box == "below":
        available = (bottom - top) * 0.6
    fixed = kicker_height + gap_after_kicker + rule_block
    t_face, t_rows, t_step, l_face, l_rows, l_step, block = fit_text_block(
        card.title,
        list(card.lines[:MAX_LINES]),
        column,
        available,
        100 * scale,
        38 * scale,
        fixed=fixed,
    )
    if box == "below":
        y = top
    else:
        y = max(top, min((height - block) / 2 - height * 0.02, bottom - block))

    if card.kind == "chapter" and card.chapter is not None:
        ascent = counter_face.getmetrics()[0]
        put(draw, margin, y + ascent, counter, counter_face, AMBER, 0.2 * counter_face.size)
        bar_y = y + counter_face.size * 1.2 + 18 * scale
        progress(image, margin, bar_y, min(column, 640 * scale), card.chapter, scale)
        draw = ImageDraw.Draw(image)
    else:
        composite(image, pill, (margin, y))
    y += kicker_height + gap_after_kicker

    baseline = y + first_baseline(t_face, t_step)
    for row in t_rows:
        put(draw, margin, baseline, row, t_face, INK, -0.02 * t_face.size)
        baseline += t_step
    y += len(t_rows) * t_step + 36 * scale
    rule(image, margin, y, margin + min(column, 360 * scale), 4 * scale, 24 * scale)
    draw = ImageDraw.Draw(image)
    y += 4 * scale + 36 * scale
    baseline = y + first_baseline(l_face, l_step)
    for rows in l_rows:
        for row in rows:
            put(draw, margin, baseline, row, l_face, MUTED)
            baseline += l_step
    if box == "below":
        box = (
            margin,
            round(y + sum(len(r) for r in l_rows) * l_step + 24 * scale),
            width - margin,
            round(bottom),
        )
    if box is not None:
        draw_picture(image, card, box, scale)
    bottom_band(image, scale)
    return image


def end_card(card, size, values):
    """The close (board "End card"): the title with its last row in sunlight, the lines, the
    Day One AI lockup at the foot and the sun set behind the horizon."""
    width, height = size
    scale, margin = margins(size)
    wide = width > height
    image = ground(size, (0.3, 0.5))
    radius = 340 * scale * (1.0 if wide else 0.7)
    sun(image, (width - radius * 360 / 340, height + radius * 0.45), radius, False, scale)
    right = f"EP {values.episode} · END" if values.episode else "END"
    top = header(image, values, right, scale, margin) + 40 * scale
    mark = sun_mark(42 * scale)
    foot = height - 72 * scale - mark.height
    column = min(width - 2 * margin, round(width * 0.62) - margin) if wide else width - 2 * margin
    t_face, t_rows, t_step, l_face, l_rows, l_step, block = fit_text_block(
        card.title,
        list(card.lines[:MAX_LINES]),
        column,
        foot - top - 40 * scale,
        88 * scale,
        36 * scale,
        title_leading=1.04,
        fixed=44 * scale,
    )
    y = max(top, min((foot - block) / 2, foot - 40 * scale - block))
    draw = ImageDraw.Draw(image)
    baseline = y + first_baseline(t_face, t_step)
    for number, row in enumerate(t_rows, 1):
        if number == len(t_rows):
            put_sunlit(image, margin, baseline, row, t_face, -0.025 * t_face.size)
        else:
            put(draw, margin, baseline, row, t_face, INK, -0.025 * t_face.size)
        baseline += t_step
    draw = ImageDraw.Draw(image)
    y += len(t_rows) * t_step + 44 * scale
    baseline = y + first_baseline(l_face, l_step)
    for rows in l_rows:
        for row in rows:
            put(draw, margin, baseline, row, l_face, MUTED)
            baseline += l_step

    composite(image, mark, (margin, foot))
    name = font("bold", 28 * scale)
    tag = font("mono", 20 * scale)
    centre = foot + mark.height / 2
    x = margin + mark.width + 14 * scale
    baseline = centre + (name.getmetrics()[0] - name.getmetrics()[1]) / 2
    for text, fill in (("Day ", INK), ("One", AMBER), (" AI", INK)):
        put(draw, x, baseline, text, name, fill, -0.01 * name.size)
        x += width_of(name, text, -0.01 * name.size)
    put(draw, x + 24 * scale, baseline, "day-one.ai", tag, MUTED)
    bottom_band(image, scale)
    return image
