"""Command-line entry point; stdout is reserved for successful output records."""

import argparse
import sys
from pathlib import Path

from .renderer import RenderError, render_plan


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render a contract v1/v1.1 JSON edit plan")
    parser.add_argument("plan", type=Path)
    parser.add_argument("--root", type=Path, help="Repository root for relative plan paths")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--timeout", type=float, default=1800, help="Seconds per media process")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing results")
    args = parser.parse_args(argv)
    try:
        for clip_id, path, duration in render_plan(
            args.plan,
            root=args.root,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            timeout=args.timeout,
            overwrite=args.overwrite,
        ):
            print(f"{clip_id}\t{path}\t{duration:.6f}")
    except (RenderError, OSError, ValueError) as exc:
        print(f"cliprender: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cliprender: cancelled; temporary outputs removed", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
