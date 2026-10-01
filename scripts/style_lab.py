#!/usr/bin/env python3
"""Style lab: write, look at and check a code-drawn transition before a style uses it.

    RUN="uv run --python 3.12 --with numpy --with scipy --with pillow python scripts/style_lab.py"
    $RUN sheet  assets/styles/transitions/token_stream.py --a A.mp4 --b B.mp4 --out out/lab/sheet.png
    $RUN check  assets/styles/transitions/token_stream.py --a A.mp4 --b B.mp4
    $RUN render assets/styles/transitions/token_stream.py --a A.mp4 --b B.mp4 --out out/lab/x.mp4

A transition is ONE Python file exposing:

    NAME = "neural-mesh"          # slug, lowercase-with-dashes
    TITLE = "Neural mesh"         # what a person calls it
    SECONDS = 1.0                 # the length it was designed for, 0.4 .. 1.5
    DESCRIPTION = "one sentence"

    def render(a, b, t, state):
        '''a, b: HxWx3 uint8 RGB numpy arrays. `a` is the outgoing video's frame at this
        instant, `b` the incoming video's frame at the same instant (both keep moving).
        t: progress, 0.0 on the first frame, 1.0 on the last.
        state: a dict that lives for one transition; precompute into it on the first call.
          state["size"] = (W, H), state["fps"], state["n_frames"], state["frame_index"],
          state["seed"] (use it for every random choice: output must be deterministic).
        Return an HxWx3 uint8 RGB array.'''

The renderer calls the same function for every join of a reel whose plan says
`"transition": {"kind": "module", "module": "<file>", "seconds": 1.0}` (contract/README.md),
which is what a style's `transition` becomes (docs/styles.md).

What `check` enforces:
  * t == 0 returns `a` and t == 1 returns `b` (max abs difference <= 3 levels)
  * right shape and dtype at the size asked for (run it at 640x360 and at 1920x1080)
  * under 1.0 s per frame on average at 1920x1080 on this machine
Only numpy, scipy and Pillow may be imported, plus the standard library.

`--a` is the outgoing video (its last frames are used), `--b` the incoming one (its first
frames). Any two videos work: `assets/demo-clip.mp4` for both is enough to start, and a card
against talking footage is the case a reel has most (cut both from a reel in out/).
`sheet` writes a contact sheet of 8 frames at 640x360: open it and LOOK before trusting it.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
_pinned = ROOT / ".tools" / "ffmpeg" / "ffmpeg"
FFMPEG = os.environ.get("CLIP_FFMPEG") or (str(_pinned) if _pinned.exists() else "ffmpeg")
FPS = 24
SEED = 7
REQUIRED = ("NAME", "TITLE", "SECONDS", "DESCRIPTION", "render")


def load(path):
    path = Path(path).resolve()
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    missing = [name for name in REQUIRED if not hasattr(mod, name)]
    if missing:
        sys.exit(f"{path.name}: missing {', '.join(missing)}")
    if not 0.4 <= float(mod.SECONDS) <= 1.5:
        sys.exit(f"{path.name}: SECONDS must be between 0.4 and 1.5 (the contract's limit), got {mod.SECONDS}")
    return mod


def decode(path, n, size, tail):
    """n frames of `path` at `size`: its last n when `tail`, else its first n."""
    w, h = size
    seconds = n / FPS + 0.5
    cmd = [FFMPEG, "-v", "error"]
    # A file's audio often outlasts its video, so the last `seconds` of the container can hold
    # fewer frames than that: reach two seconds further back and keep the last n.
    cmd += ["-sseof", f"-{seconds + 2:.3f}"] if tail else ["-t", f"{seconds:.3f}"]
    cmd += ["-i", str(path), "-an", "-vf", f"fps={FPS},scale={w}:{h}:flags=bicubic",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)
    if len(frames) < n:
        sys.exit(f"{path}: needed {n} frames, decoded {len(frames)}")
    return frames[-n:] if tail else frames[:n]


def frames_of(mod, a_path, b_path, size, indices=None, timings=None):
    """Yield (i, t, frame, a, b) for every frame of the transition, or only for `indices`.

    Frames are always rendered in order from 0 so `state` behaves exactly as in a full render.
    """
    n = max(2, round(float(mod.SECONDS) * FPS))
    a_frames = decode(a_path, n, size, tail=True)
    b_frames = decode(b_path, n, size, tail=False)
    state = {"size": tuple(size), "fps": FPS, "n_frames": n, "seed": SEED}
    wanted = set(range(n)) if indices is None else set(indices(n))
    last = max(wanted)
    for i in range(last + 1):
        t = i / (n - 1)
        state["frame_index"] = i
        a, b = a_frames[i].copy(), b_frames[i].copy()
        started = time.perf_counter()
        out = mod.render(a, b, t, state)
        if timings is not None:
            timings.append(time.perf_counter() - started)
        if not isinstance(out, np.ndarray) or out.shape != (size[1], size[0], 3) or out.dtype != np.uint8:
            got = f"{type(out).__name__} {getattr(out, 'shape', '')} {getattr(out, 'dtype', '')}"
            sys.exit(f"frame {i}: render must return uint8 array of shape {(size[1], size[0], 3)}, got {got}")
        if i in wanted:
            yield i, t, out, a_frames[i], b_frames[i]


def encode(frames, out_path, size):
    w, h = size
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-r", str(FPS), "-i", "-", "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "16",
           "-pix_fmt", "yuv420p", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    count = 0
    for frame in frames:
        proc.stdin.write(np.ascontiguousarray(frame).tobytes())
        count += 1
    proc.stdin.close()
    if proc.wait() != 0:
        sys.exit("ffmpeg failed while encoding")
    return count


def cmd_render(mod, args, size):
    count = encode((f for _, _, f, _, _ in frames_of(mod, args.a, args.b, size)), args.out, size)
    print(f"render: {mod.NAME} -> {args.out} ({count} frames, {count / FPS:.2f} s, {size[0]}x{size[1]})")


def cmd_sheet(mod, args, size):
    from PIL import Image, ImageDraw

    def pick(n):
        return sorted({round(k * (n - 1) / (args.n - 1)) for k in range(args.n)})

    shots = [(t, f) for _, t, f, _, _ in frames_of(mod, args.a, args.b, size, indices=pick)]
    cols = 4
    rows = -(-len(shots) // cols)
    tw = 480
    th = round(tw * size[1] / size[0])
    pad, label = 6, 18
    sheet = Image.new("RGB", (cols * (tw + pad) + pad, rows * (th + label + pad) + pad), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for k, (t, frame) in enumerate(shots):
        x = pad + (k % cols) * (tw + pad)
        y = pad + (k // cols) * (th + label + pad)
        draw.text((x + 2, y + 2), f"t = {t:.2f}", fill=(230, 230, 230))
        sheet.paste(Image.fromarray(frame).resize((tw, th), Image.BICUBIC), (x, y + label))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.out)
    print(f"sheet: {mod.NAME} -> {args.out} ({len(shots)} frames at {size[0]}x{size[1]})")


def cmd_check(mod, args, size):
    timings = []
    first = last = None
    n = 0
    for i, t, frame, a, b in frames_of(mod, args.a, args.b, size, timings=timings):
        n += 1
        if i == 0:
            first = int(np.abs(frame.astype(np.int16) - a.astype(np.int16)).max())
        if t == 1.0:
            last = int(np.abs(frame.astype(np.int16) - b.astype(np.int16)).max())
    avg, worst = sum(timings) / len(timings), max(timings)
    results = [
        (first is not None and first <= 3, f"t=0 equals a (max diff {first})"),
        (last is not None and last <= 3, f"t=1 equals b (max diff {last})"),
        (avg <= 1.0, f"speed: {avg:.3f} s/frame average, {worst:.3f} s worst at {size[0]}x{size[1]}"),
    ]
    for ok, text in results:
        print(("PASS  " if ok else "FAIL  ") + text)
    print(f"check: {mod.NAME} · {n} frames · {'ALL PASS' if all(ok for ok, _ in results) else 'FAILED'}")
    sys.exit(0 if all(ok for ok, _ in results) else 1)


def main():
    parser = argparse.ArgumentParser(description="Render, inspect and check a code-drawn transition")
    parser.add_argument("command", choices=["render", "sheet", "check"])
    parser.add_argument("transition", help="path to the transition .py file")
    parser.add_argument("--a", required=True, help="outgoing video (its last frames are used)")
    parser.add_argument("--b", required=True, help="incoming video (its first frames are used)")
    parser.add_argument("--out", default=None)
    parser.add_argument("--size", default=None, help="WxH (default 640x360 for sheet, 1920x1080 otherwise)")
    parser.add_argument("--n", type=int, default=8, help="frames on the contact sheet")
    args = parser.parse_args()
    default = "640x360" if args.command == "sheet" else "1920x1080"
    size = tuple(int(v) for v in (args.size or default).lower().split("x"))
    mod = load(args.transition)
    if args.command != "check" and not args.out:
        parser.error("--out is required")
    {"render": cmd_render, "sheet": cmd_sheet, "check": cmd_check}[args.command](mod, args, size)


if __name__ == "__main__":
    main()
