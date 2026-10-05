"""Contract v1.4 `clips[].layout`: frame a recorded screen share as picture-in-picture.

Why: a meeting recording shows the shared screen in one fixed region and the active speaker
in a small tile beside it, so a clip rendered "as is" wastes a third of the frame on call
chrome and shows the screen too small to read. `pip` crops the `screen` region and scales it
to fill the output frame as far as its shape allows, then shows the `speaker` region small in
`corner`. Where the screen's shape leaves a band (a 4:3 share in a 16:9 frame leaves one at the
side) and the tile fits that band at a useful size, the tile sits in the band and covers no
part of the screen; otherwise it is laid over the screen with a thin light border so it reads
as a separate picture. The band takes the cards' background colour, so a pip clip and the
slides around it look like one programme.

The output frame keeps the size `geometry` gives a 16:9 clip, so a pip clip joins a reel like
any other; only the picture inside it changes. The screen region is enlarged to fill that
frame, which is the point of the layout; the frame itself is never larger than the source's.

The geometry is one filter chain that `renderer.video_graph` places where the plain
`geometry` chain would go (after the trim and the timestamp shift, before burned captions):
`split` into two branches, crop and scale each, `pad` the screen onto the band colour and
`overlay` the tile. Labels are local to the graph of one part, which holds this chain once.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

from .cards import BACKGROUND
from .media import RenderError, geometry

# Width of a tile laid over the screen, as a share of the frame width.
OVERLAY_TILE_SHARE = 0.24
# A tile goes in the band beside or below the screen only when it is at least this share of
# the frame height there; smaller than that a face is a thumbnail, so it is laid over instead.
MIN_BAND_TILE = 0.18
# A tile in a band never grows beyond this share of the frame width, however wide the band.
MAX_BAND_TILE = 0.35
# Margin around the tile, as a share of the frame height, and the tile's border in pixels.
MARGIN_SHARE = 0.025
BORDER = 2
BORDER_COLOR = "0xD8DCE2"
BAND_COLOR = "0x{:02X}{:02X}{:02X}".format(*BACKGROUND)
CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")


def even(value):
    """Round down to an even number of pixels, at least two (yuv420p needs even sizes)."""
    return max(2, int(value) // 2 * 2)


@dataclass(frozen=True)
class Pip:
    """A resolved pip layout on the output frame.

    `screen` and `tile` are (x, y, width, height) in output pixels; `tile` excludes the border
    and is None without a speaker region. `overlaid` says whether the tile covers the screen.
    `filter` is the chain that draws it all from one source frame.
    """

    filter: str
    dimensions: tuple[int, int]
    screen: tuple[int, int, int, int]
    tile: tuple[int, int, int, int] | None
    overlaid: bool
    corner: str

    @property
    def tile_in_bottom_corner(self):
        return self.tile is not None and self.corner.startswith("bottom")

    def caption_clearance(self):
        """Pixels burned captions must keep from each side edge so they never run under a tile
        in a bottom corner: the tile's width plus its border and margin. Symmetric, so the
        captions stay centred under the screen. Zero when no tile sits at the bottom."""
        if not self.tile_in_bottom_corner:
            return 0
        x, _, width, _ = self.tile
        frame_width = self.dimensions[0]
        near = x if self.corner.endswith("left") else frame_width - (x + width)
        return width + BORDER + near + round(frame_width * 0.01)


def source_frame(video):
    """(width, height, pixel aspect) of the frames the filter graph receives.

    FFmpeg autorotates before the graph, so a rotated recording arrives with its sides
    swapped; `layout` regions are in those pixels, as a player shows the frame before any
    square-pixel correction (Meet and Zoom recordings have square pixels anyway).
    """
    width, height = int(video["width"]), int(video["height"])
    sar = video.get("sample_aspect_ratio", "1:1")
    ratio = Fraction(1)
    if sar not in {"N/A", "0:1"}:
        try:
            ratio = Fraction(sar.replace(":", "/"))
        except (ValueError, ZeroDivisionError) as exc:
            raise RenderError("Invalid sample aspect ratio") from exc
    rotation = float(video.get("tags", {}).get("rotate", 0))
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = float(side["rotation"])
    if round(rotation / 90) % 2:
        width, height, ratio = height, width, 1 / ratio
    return width, height, ratio


def check_region(region, width, height, label, clip_id):
    """A layout region must be at least 2 x 2 pixels and lie inside the source frame."""
    x, y, w, h = region
    if w < 2 or h < 2:
        raise RenderError(f"Clip {clip_id}: layout.{label} {region} must be at least 2 x 2 pixels")
    if x + w > width or y + h > height:
        raise RenderError(
            f"Clip {clip_id}: layout.{label} {region} reaches past the {width} x {height} "
            "source frame; give [x, y, w, h] in source pixels inside the frame"
        )


def fit(aspect, width, height):
    """Largest even (w, h) of display aspect `aspect` inside width x height."""
    if aspect >= Fraction(width, height):
        return width, min(height, even(width / aspect))
    return min(width, even(height * aspect)), height


def pip_framing(video, spec, max_height, clip_id="clip"):
    """Resolve `layout: pip` for a 16:9 clip into a `Pip`, or raise for a region off-frame."""
    _, (frame_w, frame_h) = geometry(video, "16:9", "center", max_height)
    src_w, src_h, ratio = source_frame(video)
    screen = tuple(spec["screen"])
    check_region(screen, src_w, src_h, "screen", clip_id)
    speaker = tuple(spec["speaker"]) if spec.get("speaker") else None
    if speaker:
        check_region(speaker, src_w, src_h, "speaker", clip_id)
    corner = spec.get("corner", "bottom-right")
    right, bottom = corner.endswith("right"), corner.startswith("bottom")
    screen_w, screen_h = fit(Fraction(screen[2]) * ratio / screen[3], frame_w, frame_h)
    margin = even(frame_h * MARGIN_SHARE)

    def placed(x, y):
        return (x, y, screen_w, screen_h)

    tile, overlaid, place = None, False, None
    if speaker:
        tile_aspect = Fraction(speaker[2]) * ratio / speaker[3]
        room = 2 * margin + 2 * BORDER
        band_h, band_w = frame_h - screen_h, frame_w - screen_w
        size = None
        if band_h > 0:
            # Band above or below the screen: the tile is as tall as the band allows.
            tile_h = band_h - room
            tile_w = tile_h * tile_aspect
            limit = min(frame_w * MAX_BAND_TILE, frame_w - room)
            if tile_w > limit:
                tile_w, tile_h = limit, limit / tile_aspect
            if tile_h >= frame_h * MIN_BAND_TILE:
                size = (even(tile_w), even(tile_h))
                # The screen moves away from the corner, so the band is on the corner's side.
                place = placed((frame_w - screen_w) // 2 // 2 * 2, 0 if bottom else band_h)
        elif band_w > 0:
            # Band beside the screen: the tile is as wide as the band allows.
            tile_w = band_w - room
            tile_h = tile_w / tile_aspect
            if tile_w > frame_w * MAX_BAND_TILE or tile_h > frame_h - room:
                tile_h = min(frame_w * MAX_BAND_TILE / tile_aspect, frame_h - room)
                tile_w = tile_h * tile_aspect
            if tile_h >= frame_h * MIN_BAND_TILE:
                size = (even(tile_w), even(tile_h))
                place = placed(0 if right else band_w, (frame_h - screen_h) // 2 // 2 * 2)
        if size is None:
            # No band worth using: the screen is centred and the tile laid over it.
            overlaid = True
            tile_w = even(frame_w * OVERLAY_TILE_SHARE)
            size = (tile_w, even(tile_w / tile_aspect))
        tile_x = frame_w - margin - BORDER - size[0] if right else margin + BORDER
        tile_y = frame_h - margin - BORDER - size[1] if bottom else margin + BORDER
        tile = (tile_x, tile_y, *size)
    if place is None:
        place = placed((frame_w - screen_w) // 2 // 2 * 2, (frame_h - screen_h) // 2 // 2 * 2)
    sx, sy, sw, sh = screen
    screen_chain = (
        f"crop={sw}:{sh}:{sx}:{sy},scale={screen_w}:{screen_h},setsar=1,"
        f"pad={frame_w}:{frame_h}:{place[0]}:{place[1]}:color={BAND_COLOR}"
    )
    if tile is None:
        chain = f"{screen_chain},setsar=1,format=yuv420p"
    else:
        kx, ky, kw, kh = speaker
        tx, ty, tw, th = tile
        chain = (
            f"split=2[pip_screen][pip_tile];"
            f"[pip_screen]{screen_chain}[pip_band];"
            f"[pip_tile]crop={kw}:{kh}:{kx}:{ky},scale={tw}:{th},setsar=1,"
            f"pad={tw + 2 * BORDER}:{th + 2 * BORDER}:{BORDER}:{BORDER}:color={BORDER_COLOR}"
            f"[pip_small];"
            f"[pip_band][pip_small]overlay=x={tx - BORDER}:y={ty - BORDER},"
            "setsar=1,format=yuv420p"
        )
    return Pip(chain, (frame_w, frame_h), place, tile, overlaid, corner)


def caption_style(pip, frame_width):
    """`force_style` for the `subtitles` filter that keeps burned captions off a bottom tile.

    FFmpeg converts an SRT to ASS with a 384-unit-wide script resolution, and libass scales
    margins from it to the frame width, so pixels become script units by 384 / width
    (rounded up, so the captions err on the side of clearance). None when nothing to avoid.
    """
    clearance = pip.caption_clearance() if pip is not None else 0
    if not clearance:
        return None
    units = math.ceil(clearance * 384 / frame_width)
    return f"MarginL={units},MarginR={units}"
