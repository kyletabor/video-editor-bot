# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema==4.25.1"]
# ///
"""Portable shared gate. Run `uv run --script scripts/check.py` from any cwd."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
FFMPEG_VERSION = "7.0.2"


class CheckError(RuntimeError):
    pass


def run(*args: str, timeout: int = 180) -> str:
    try:
        result = subprocess.run(
            args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError(f"{args[0]}: {exc}") from exc
    if result.returncode:
        raise CheckError(
            f"Command failed ({result.returncode}): {subprocess.list2cmdline(args)}\n"
            f"{result.stdout}{result.stderr}"
        )
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    return result.stdout


def tool_environment() -> None:
    # The explicitly installed project toolchain wins over an unrelated system build.
    tool_dir = ROOT / ".tools" / "ffmpeg"
    if tool_dir.is_dir():
        os.environ["PATH"] = str(tool_dir) + os.pathsep + os.environ.get("PATH", "")


def check_versions(*, require_pinned: bool = False) -> None:
    versions = []
    for tool in ("ffmpeg", "ffprobe"):
        first = run(tool, "-version").splitlines()[0]
        match = re.search(r"version n?(\d+\.\d+(?:\.\d+)?)(?=[\s-])", first)
        if not match or tuple(int(n) for n in match.group(1).split(".")) < (4, 4):
            raise CheckError(
                f"Expected {tool} >=4.4; found {first}. "
                "Run python scripts/install_ffmpeg.py (see docs/checks.md)."
            )
        versions.append(match.group(1))
        print(f"tools: {first}", flush=True)
    if len(set(versions)) != 1:
        raise CheckError(f"ffmpeg and ffprobe versions must match; got {versions}")
    if require_pinned and versions[0] != FFMPEG_VERSION:
        raise CheckError(f"CI/reference run requires FFmpeg {FFMPEG_VERSION}; run python scripts/install_ffmpeg.py")
    if versions[0] != FFMPEG_VERSION:
        print(f"tools: compatibility run; reproducible reference is {FFMPEG_VERSION}", flush=True)
    encoders = run("ffmpeg", "-hide_banner", "-encoders")
    filters = run("ffmpeg", "-hide_banner", "-filters")
    for capability, listing in [(name, encoders) for name in ("libx264", "aac")] + [
        (name, filters) for name in ("subtitles", "trim", "atrim", "setpts", "asetpts", "concat", "scale", "crop")
    ]:
        if not re.search(rf"^\s+\S+\s+{re.escape(capability)}\s", listing, re.MULTILINE):
            raise CheckError(f"FFmpeg is missing required capability: {capability}")


def check_whitespace() -> None:
    # Never use a failing diff as a reason to silently run a less strict diff.
    base = os.environ.get("CHECK_BASE")
    if not base:
        history = run("git", "rev-list", "--max-count=2", "HEAD").splitlines()
        base = history[-1]
        if run("git", "branch", "--list", "--remotes", "origin/main").strip():
            ancestor = run("git", "merge-base", "origin/main", "HEAD").strip()
            if ancestor != history[0]:
                base = ancestor
    run("git", "diff", "--check", base, "HEAD")
    run("git", "diff", "--check", "--cached")
    run("git", "diff", "--check")
    print("whitespace: OK", flush=True)


def check_contract() -> None:
    print(run(sys.executable, str(ROOT / "scripts" / "check_contract.py")), end="", flush=True)


def probe(path: Path, *, count_frames: bool = False) -> dict:
    options = ["-count_frames"] if count_frames else []
    return json.loads(run("ffprobe", "-v", "error", *options, "-show_streams",
                          "-show_format", "-of", "json", str(path)))


def duration_near(value: object, expected: float, label: str) -> None:
    try:
        actual = float(value)
    except (ValueError, TypeError) as exc:
        raise CheckError(f"{label}: missing/invalid duration {value!r}") from exc
    # Three 30fps frames also accommodates AAC encoder padding.
    if not math.isfinite(actual) or abs(actual - expected) > 0.1:
        raise CheckError(f"{label}: expected {expected:.3f}s +/- 0.1s, got {actual}")


def assert_media(info: dict, *, seconds: float, width: int, height: int, frames: int) -> None:
    streams = info.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if len(streams) != 2 or len(videos) != 1 or len(audios) != 1:
        raise CheckError("Expected exactly one video and one audio stream")
    video, audio = videos[0], audios[0]
    if (video.get("codec_name"), video.get("pix_fmt"), video.get("width"), video.get("height")) != (
        "h264", "yuv420p", width, height
    ):
        raise CheckError(f"Unexpected video codec/pixel format/dimensions: {video}")
    if audio.get("codec_name") != "aac" or audio.get("sample_rate") != "48000":
        raise CheckError(f"Unexpected audio codec/sample rate: {audio}")
    if str(video.get("nb_read_frames")) != str(frames):
        raise CheckError(f"Expected {frames} decoded frames; got {video.get('nb_read_frames')}")
    duration_near(info.get("format", {}).get("duration"), seconds, "container")
    duration_near(video.get("duration"), seconds, "video")
    duration_near(audio.get("duration"), seconds, "audio")


def decode(path: Path) -> None:
    run("ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-")


def check_assets() -> None:
    info = probe(ROOT / "assets" / "demo-clip.mp4")
    types = {s.get("codec_type") for s in info.get("streams", [])}
    if not {"video", "audio"} <= types:
        raise CheckError("Demo asset must contain video and audio")
    print("assets: OK (demo-clip.mp4 has video and audio)", flush=True)


def generate_fixture(directory: Path) -> Path:
    source = directory / "generated source.mp4"
    run("ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=5",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "5", "-movflags", "+faststart", str(source))
    assert_media(probe(source, count_frames=True), seconds=5, width=640, height=360, frames=150)
    decode(source)
    return source


def check_ffmpeg() -> None:
    with tempfile.TemporaryDirectory(prefix="veb smoke ") as directory:
        generate_fixture(Path(directory))
    print("ffmpeg environment: OK (generated 5s, 640x360, 150 frames, H.264/AAC; decoded)", flush=True)


def uv() -> str:
    executable = shutil.which("uv")
    if not executable:
        raise CheckError("uv is required for lane checks; see docs/checks.md")
    return executable


def check_bot() -> None:
    if not (ROOT / "bot" / "pyproject.toml").is_file():
        print("bot: SKIPPED (implementation has not landed)", flush=True)
        return
    print(run(uv(), "run", "--quiet", "--project", "bot", "python", "-m", "pytest",
              "-q", "bot/tests", timeout=300), end="", flush=True)
    print("bot: OK", flush=True)


def check_render() -> None:
    if not (ROOT / "render" / "pyproject.toml").is_file():
        print("renderer acceptance: SKIPPED (veb-2rq CLI has not landed; environment check is separate)", flush=True)
        return
    print(run(uv(), "run", "--quiet", "--project", "render", "--extra", "dev", "python",
              "-m", "pytest", "-q", "render/tests", timeout=600), end="", flush=True)
    with tempfile.TemporaryDirectory(prefix="veb renderer smoke ") as directory:
        temp = Path(directory)
        source = generate_fixture(temp)
        # Adapt the canonical contract example; no tracked fixture/asset is changed.
        plan = json.loads((ROOT / "contract/examples/one-clip-trim.json").read_text(encoding="utf-8"))
        plan["source"] = {"path": source.as_posix(), "duration_seconds": 5, "captions": {"kind": "none"}}
        plan["output"] = {"dir": (temp / "output").as_posix(), "preset": "internal", "aspect": "16:9", "captions": "none"}
        plan["clips"] = [{"id": "smoke", "takeaway": "Generated trim and join check", "trim_silence": False,
                          "segments": [{"start": 0.5, "end": 2}, {"start": 3, "end": 4.5}]}]
        import jsonschema
        jsonschema.validate(plan, json.loads((ROOT / "contract/edit-plan.schema.json").read_text(encoding="utf-8")))
        plan_path = temp / "edit plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        print(run(uv(), "run", "--quiet", "--project", "render", "cliprender", str(plan_path),
                  "--root", str(ROOT), timeout=300), end="", flush=True)
        output = temp / "output" / "smoke.mp4"
        assert_media(probe(output, count_frames=True), seconds=3, width=640, height=360, frames=90)
        decode(output)
    print("renderer acceptance: OK (CLI trims and joins 5s input to 3s, 640x360, 90 frames, H.264/AAC)", flush=True)


def check_gate_tests() -> None:
    print(run(sys.executable, "-m", "unittest", "discover", "-s", "scripts/tests", "-v"), end="", flush=True)
    print("gate regression tests: OK", flush=True)


CHECKS = {
    "whitespace": check_whitespace,
    "contract": check_contract,
    "tests": check_gate_tests,
    "assets": check_assets,
    "ffmpeg": check_ffmpeg,
    "bot": check_bot,
    "render": check_render,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-pinned", action="store_true", help="require the reference FFmpeg version used by CI")
    parser.add_argument("checks", nargs="*", choices=[*CHECKS, "all"], default=["all"])
    args = parser.parse_args()
    selected = list(CHECKS) if "all" in args.checks else args.checks
    tool_environment()
    try:
        if {"assets", "ffmpeg", "bot", "render"}.intersection(selected):
            check_versions(require_pinned=args.require_pinned)
        for name in selected:
            CHECKS[name]()
    except (CheckError, ValueError, KeyError) as exc:
        print(f"check: FAIL {exc}", file=sys.stderr, flush=True)
        return 1
    print("check: OK (see individual SKIPPED lines for pending integration)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
