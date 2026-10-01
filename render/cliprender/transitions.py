"""Code-drawn transitions: a plan may name a Python file that draws the frames of every join.

`output.reel.transition {kind: module, module: <path>.py}` is timed exactly like a dissolve
(the same overlap in whole frames, the same audio cross-fade), but the bridge's picture comes
from the module instead of `xfade`. The module exposes

    render(a, b, t, state) -> frame

where `a` and `b` are HxWx3 uint8 RGB numpy arrays (the outgoing and the incoming picture at
the same instant, both still moving), `t` runs from 0.0 on the first bridge frame to 1.0 on
the last (0.5 when the bridge is a single frame), and `state` is a dict that lives for one
bridge, pre-filled with `size` (W, H), `fps`, `n_frames`, `seed` and `frame_index`. It returns
an HxWx3 uint8 RGB array. A module's own `SECONDS` is advisory: the plan decides the length.

The frames are streamed: two FFmpeg processes decode the raw tail and head that the pieces
already write for a dissolve, one frame pair at a time, and a third encodes what `render`
returns with the piece encoder settings, so the join still copies the stream. No more than one
frame pair and one result is alive here at once, whatever the bridge's length or size.

numpy is not a dependency of the renderer proper; it comes with the `styles` extra and is
imported only when a plan asks for a module transition.
"""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import time
from contextlib import ExitStack, suppress
from pathlib import Path

from .media import RenderError

# The value every module is told to seed its random choices with, so a reel renders the same
# picture twice (the prototype harness uses the same one).
SEED = 7
# swscale's default YUV/RGB arithmetic is fast and rounds down: a frame taken to RGB and back
# comes out two luma levels darker (measured on FFmpeg 4.4.2 and 7.0.2), which would show as
# a step in brightness on entering and leaving every bridge. With these flags the round trip
# of a frame the module returns untouched is exact on both.
EXACT = "accurate_rnd+full_chroma_int"
STYLES_FIX = (
    "run the renderer with its styles extra: "
    "`uv run --project render --extra styles cliprender ...`"
)


def load_numpy(module):
    """numpy, or a RenderError that names the fix when the `styles` extra is not installed."""
    try:
        import numpy
    except ImportError as exc:
        raise RenderError(
            f"transition module {module} needs numpy, which is an optional extra of the "
            f"renderer; {STYLES_FIX}"
        ) from exc
    return numpy


def load_transition(path):
    """The `render` callable of the transition module at `path`.

    Every way the file can fail to give one is a RenderError that names it: the plan is the
    only place the path comes from, so the operator needs to know which file to fix.
    """
    path = Path(path)
    if not path.is_file():
        raise RenderError(f"transition module not found: {path}")
    load_numpy(path)
    spec = importlib.util.spec_from_file_location(f"cliprender_transition_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RenderError(f"transition module {path} is not a Python source file")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:
        raise RenderError(
            f"transition module {path} could not be imported ({exc}); numpy and scipy come "
            f"with the styles extra, so {STYLES_FIX}"
        ) from exc
    except Exception as exc:  # the module is the plan author's code: any failure is theirs
        raise RenderError(
            f"transition module {path} failed to load: {type(exc).__name__}: {exc}"
        ) from exc
    render = getattr(module, "render", None)
    if not callable(render):
        raise RenderError(f"transition module {path} has no callable `render(a, b, t, state)`")
    return render


def bridge_progress(index, frames):
    """`t` of bridge frame `index`: 0.0 on the first and 1.0 on the last of `frames`, and the
    midpoint for a bridge of a single frame, which has neither a first nor a last half."""
    return 0.5 if frames < 2 else index / (frames - 1)


def checked_frame(np, frame, dimensions, module, index):
    """`render`'s result as bytes for the encoder, or a RenderError that says what it was."""
    width, height = dimensions
    shape = (height, width, 3)
    if isinstance(frame, np.ndarray) and frame.shape == shape and frame.dtype == np.uint8:
        return np.ascontiguousarray(frame).data
    got = (
        f"a {frame.dtype} array of shape {frame.shape}"
        if isinstance(frame, np.ndarray)
        else type(frame).__name__
    )
    raise RenderError(
        f"transition module {module} returned {got} at frame {index}; `render` must return "
        f"a uint8 RGB array of shape {shape}"
    )


def log_tail(log):
    log.seek(0)
    return log.read().decode("utf-8", errors="replace")[-2000:].strip()


def encode_module_bridge(
    tools,
    workspace,
    name,
    outgoing,
    incoming,
    frames,
    fps,
    dimensions,
    render,
    module,
    *,
    matrix,
    tags,
    encoder_flags,
):
    """Write `<name>.mp4`: exactly `frames` frames drawn by `render` from the tail of
    `outgoing` and the head of `incoming` (the raw files a dissolve cross-fades).

    The raw files carry no colour description, so both conversions name the matrix and range
    the pieces are tagged with (`matrix`, limited range), the same pair a card's RGB goes
    through: what `render` sees is what a player shows, and a frame it returns unchanged comes
    back as the pixels it was (`EXACT`). FFmpeg's stderr goes to a file, not a pipe, so a
    chatty process can never block on a reader that is busy drawing.
    """
    np = load_numpy(module)
    width, height = dimensions
    size = width * height * 3
    rate = f"{fps.numerator}/{fps.denominator}"
    common = [str(tools.ffmpeg), "-hide_banner", "-nostdin", "-v", "error", "-xerror"]
    to_rgb = f"scale=in_color_matrix={matrix}:in_range=tv:flags={EXACT},format=rgb24"
    to_yuv = f"scale=out_color_matrix={matrix}:out_range=tv:flags={EXACT},format=yuv420p,{tags}"
    decoders = [
        [*common, "-i", f"{piece}-{end}.y4m", "-map", "0:v:0", "-vf", to_rgb]
        + ["-f", "rawvideo", "-"]
        for piece, end in ((outgoing, "tail"), (incoming, "head"))
    ]
    encoder = [*common, "-y", "-f", "rawvideo", "-pix_fmt", "rgb24"]
    encoder += ["-video_size", f"{width}x{height}", "-framerate", rate, "-i", "pipe:0"]
    encoder += ["-map", "0:v:0", "-vf", to_yuv, *encoder_flags, f"{name}.mp4"]
    deadline = time.monotonic() + tools.timeout
    late = RenderError(
        f"transition module {module} did not finish {name} within {tools.timeout:g}s; "
        "increase --timeout"
    )
    running = []
    with ExitStack() as stack:

        def start(args, **pipes):
            log = stack.enter_context(tempfile.TemporaryFile())
            process = subprocess.Popen(args, cwd=workspace, stderr=log, **pipes)
            running.append((process, log))
            return process

        try:
            sources = [
                start(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE) for args in decoders
            ]
            sink = start(encoder, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
            state = {"size": (width, height), "fps": float(fps), "n_frames": frames, "seed": SEED}
            for index in range(frames):
                if time.monotonic() > deadline:
                    raise late
                pair = []
                for source, (_, log), end in zip(sources, running, ("tail", "head")):
                    raw = source.stdout.read(size)
                    if len(raw) != size:
                        raise RenderError(
                            f"{name}: the {end} of its neighbour ended at frame {index} of "
                            f"{frames}: {log_tail(log) or 'no more frames'}"
                        )
                    pair.append(np.frombuffer(raw, np.uint8).reshape(height, width, 3))
                state["frame_index"] = index
                try:
                    frame = render(pair[0], pair[1], bridge_progress(index, frames), state)
                except Exception as exc:  # the module is the plan author's code
                    raise RenderError(
                        f"transition module {module} failed at frame {index} of {frames}: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                data = checked_frame(np, frame, dimensions, module, index)
                try:
                    sink.stdin.write(data)
                except OSError as exc:
                    raise RenderError(
                        f"ffmpeg stopped encoding {name} at frame {index}: "
                        f"{log_tail(running[2][1])}"
                    ) from exc
            sink.stdin.close()
            for process, log in running:
                code = process.wait(timeout=max(1.0, deadline - time.monotonic()))
                if code:
                    raise RenderError(f"ffmpeg failed ({code}) on {name}: {log_tail(log)}")
        except FileNotFoundError as exc:
            raise RenderError(
                f"Tool not found: {tools.ffmpeg}; install FFmpeg/ffprobe or set its CLI path"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise late from exc
        finally:
            for process, _ in running:
                if process.poll() is None:
                    process.kill()
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        with suppress(OSError):
                            pipe.close()
                process.wait()
