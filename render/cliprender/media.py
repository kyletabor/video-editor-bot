"""Bounded local media processes, timeline probing and output geometry."""

import json
import math
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from pathlib import Path


class RenderError(Exception):
    """An actionable plan, media, process or publication failure."""


@dataclass
class Tools:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    timeout: float = 1800

    def run(self, args, *, cwd=None):
        try:
            with subprocess.Popen(
                [str(a) for a in args],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as process:
                try:
                    stdout, stderr = process.communicate(timeout=self.timeout)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    process.kill()
                    process.communicate()
                    raise
        except FileNotFoundError as exc:
            raise RenderError(
                f"Tool not found: {args[0]}; install FFmpeg/ffprobe or set its CLI path"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RenderError(
                f"{Path(args[0]).name} timed out after {self.timeout:g}s; increase --timeout"
            ) from exc
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace")[-5000:].strip()
            raise RenderError(f"{Path(args[0]).name} failed ({process.returncode}): {detail}")
        return stdout.decode("utf-8-sig", errors="replace")

    def encode(self, args, *, cwd=None):
        return self.run(
            [self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror", *args], cwd=cwd
        )

    def probe(self, path):
        return json.loads(
            self.run(
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-show_format",
                    "-show_streams",
                    "-of",
                    "json",
                    path,
                ]
            )
        )

    def frames(self, path, time_base=None):
        if time_base is None:
            video = next(s for s in self.probe(path)["streams"] if s["codec_type"] == "video")
            time_base = video["time_base"]
        data = json.loads(
            self.run(
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_frames",
                    "-show_entries",
                    "frame=best_effort_timestamp,pkt_duration_time,duration_time",
                    "-of",
                    "json",
                    path,
                ]
            )
        )
        frames = data.get("frames", [])
        try:
            times = [int(f["best_effort_timestamp"]) * Fraction(time_base) for f in frames]
        except (KeyError, ValueError) as exc:
            raise RenderError(
                "Video frames have no usable presentation timestamps; remux the source"
            ) from exc
        if not times or any(not math.isfinite(t) for t in times):
            raise RenderError("Source has no decodable video frames with finite timestamps")
        if any(b <= a for a, b in pairwise(times)):
            raise RenderError(
                "Video timestamps are not strictly increasing; repair the source timestamps"
            )
        durations = [float(f.get("duration_time", f.get("pkt_duration_time", 0))) for f in frames]
        return times, durations

    def timing_flags(self):
        help_text = self.run([self.ffmpeg, "-hide_banner", "-h", "full"])
        return ["-fps_mode", "passthrough"] if "-fps_mode" in help_text else ["-vsync", "0"]

    def graph_flag(self):
        help_text = self.run([self.ffmpeg, "-hide_banner", "-h", "full"])
        return (
            "-filter_complex_script"
            if "-filter_complex_script" in help_text
            else "-/filter_complex"
        )


def inspect_media(data):
    streams = data.get("streams", [])
    videos = [s for s in streams if s["codec_type"] == "video"]
    audios = [s for s in streams if s["codec_type"] == "audio"]
    if len(videos) != 1 or videos[0].get("disposition", {}).get("attached_pic"):
        raise RenderError("Unsupported media: need exactly one video track, without cover art")
    if len(audios) > 1:
        raise RenderError(
            "Unsupported media: multiple audio tracks; select one in a separate source file"
        )
    video = videos[0]
    if video.get("color_transfer") in {"smpte2084", "arib-std-b67"} or any(
        "dovi" in str(side).lower() or "mastering display" in str(side).lower()
        for side in video.get("side_data_list", [])
    ):
        raise RenderError(
            "Unsupported HDR video; supply an SDR conversion with tested tone mapping"
        )
    if video.get("field_order", "progressive") not in {"unknown", "progressive"}:
        raise RenderError("Unsupported interlaced video; deinterlace the source first")
    if audios and int(audios[0].get("channels", 0)) not in {1, 2}:
        raise RenderError("Unsupported audio layout; supply mono or stereo audio")
    origin = float(data.get("format", {}).get("start_time", video.get("start_time", 0)))
    if not math.isfinite(origin):
        raise RenderError("Source has an invalid presentation-time origin")
    return video, audios[0] if audios else None, origin


def geometry(video, aspect, focus, max_height):
    """Honor display geometry, autorotation and codec-even dimensions, without upscaling."""
    width, height = int(video["width"]), int(video["height"])
    sar = video.get("sample_aspect_ratio", "1:1")
    try:
        ratio = float(Fraction(sar.replace(":", "/"))) if sar not in {"N/A", "0:1"} else 1.0
    except (ValueError, ZeroDivisionError) as exc:
        raise RenderError("Invalid sample aspect ratio") from exc
    if not math.isfinite(ratio) or ratio <= 0:
        raise RenderError("Invalid sample aspect ratio")
    # Squaring anamorphic pixels only shrinks the oversized axis.
    display_ratio = width * ratio / height
    rotation = float(video.get("tags", {}).get("rotate", 0))
    for side in video.get("side_data_list", []):
        if "rotation" in side:
            rotation = float(side["rotation"])
    if abs(rotation / 90 - round(rotation / 90)) > 0.001:
        raise RenderError("Unsupported arbitrary rotation; normalize orientation before rendering")
    if round(rotation / 90) % 2:
        width, height = height, width
        display_ratio = 1 / display_ratio
    square_height = min(height, width / display_ratio)
    square_width = square_height * display_ratio
    sw, sh = max(2, int(square_width) // 2 * 2), max(2, int(square_height) // 2 * 2)
    if min(width, height) < 2:
        raise RenderError("Video must be at least 2 by 2 pixels")
    filters = [f"scale={sw}:{sh}", "setsar=1"]
    cw, ch = sw, sh
    if aspect != "16:9":
        target = 9 / 16 if aspect == "9:16" else 1
        cw = min(sw, int(sh * target) // 2 * 2)
        ch = min(sh, int(sw / target) // 2 * 2)
        if min(cw, ch) < 2:
            raise RenderError("Requested crop is smaller than two pixels")
        x = {"left": "0", "right": "iw-ow"}.get(focus, "(iw-ow)/2")
        filters.append(f"crop={cw}:{ch}:{x}:(ih-oh)/2")
    out_h = min(ch, max_height) // 2 * 2
    out_w = max(2, int(cw * out_h / ch) // 2 * 2)
    filters += [f"scale={out_w}:{out_h}", "setsar=1", "format=yuv420p"]
    return ",".join(filters), (out_w, out_h)
