"""clipbot CLI.

    uv run --project bot clipbot plan --source assets/demo-clip.mp4 \
        --request "the part where I question whether the clip bot will work" \
        --out out/demo/plan.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import captions as cap
from . import plan as planmod
from . import probe as probemod
from . import select as sel
from . import summarize as summ


def cmd_plan(a: argparse.Namespace) -> int:
    info = probemod.probe(a.source)
    lo, hi = planmod.PRESET_BOUNDS[a.preset]
    min_s = a.min_seconds if a.min_seconds is not None else lo
    max_s = a.max_seconds if a.max_seconds is not None else hi

    if a.srt:
        cues = cap.parse_srt(Path(a.srt).read_text(encoding="utf-8"))
        captions_kind, srt_path = "srt", a.srt
    elif info.subtitle_stream_index is not None:
        cues = cap.parse_srt(cap.extract_embedded_srt(a.source, info.subtitle_stream_index))
        captions_kind, srt_path = "embedded", None
    else:
        print("clipbot: no captions in source and no --srt given; transcription adapter not wired yet (veb-0rh.1)", file=sys.stderr)
        return 2

    windows = sel.best_windows(cues, a.request, min_s, max_s, max_clips=a.max_clips)
    if not windows:
        print("clipbot: nothing matched the request within the length bounds", file=sys.stderr)
        return 3

    out_path = Path(a.out)
    summary_path = out_path.with_name("summary.md") if a.summary else None
    plan = planmod.build_plan(
        info, windows, out_dir=str(out_path.parent), preset=a.preset,
        captions_kind=captions_kind, srt_path=srt_path,
        summary_path=str(summary_path) if summary_path else None,
    )
    planmod.validate(plan)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    if summary_path:
        md = summ.to_markdown(
            Path(a.source).stem, info.path, info.duration_seconds, cues,
            plan=plan, focus=sel.keywords(a.request),
        )
        summary_path.write_text(md, encoding="utf-8")
    for c in plan["clips"]:
        s = c["segments"][0]
        print(f"{c['id']}\t{s['start']:.1f}-{s['end']:.1f}s\t{c['takeaway']}")
    print(f"plan written: {out_path}")
    if summary_path:
        print(f"summary written: {summary_path}")
    return 0


def cmd_summarize(a: argparse.Namespace) -> int:
    info = probemod.probe(a.source)
    if a.srt:
        cues = cap.parse_srt(Path(a.srt).read_text(encoding="utf-8"))
    elif info.subtitle_stream_index is not None:
        cues = cap.parse_srt(cap.extract_embedded_srt(a.source, info.subtitle_stream_index))
    else:
        print("clipbot: no captions in source and no --srt given", file=sys.stderr)
        return 2
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8")) if a.plan else None
    md = summ.to_markdown(a.title or Path(a.source).stem, info.path, info.duration_seconds, cues, plan=plan)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"summary written: {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="clipbot", description="request -> edit plan")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("plan", help="produce a validated edit plan")
    pp.add_argument("--source", required=True)
    pp.add_argument("--request", required=True, help="what the clip should be about")
    pp.add_argument("--out", default="out/plan.json")
    pp.add_argument("--preset", default="internal", choices=sorted(planmod.PRESET_BOUNDS))
    pp.add_argument("--min-seconds", type=float, default=None)
    pp.add_argument("--max-seconds", type=float, default=None)
    pp.add_argument("--max-clips", type=int, default=1)
    pp.add_argument("--srt", default=None, help="sidecar SRT when the source has no captions")
    pp.add_argument("--summary", action="store_true", help="also write summary.md next to the plan")
    pp.set_defaults(fn=cmd_plan)
    ps = sub.add_parser("summarize", help="write executive summary + full transcript markdown")
    ps.add_argument("--source", required=True)
    ps.add_argument("--srt", default=None)
    ps.add_argument("--plan", default=None, help="edit plan JSON to list clips from")
    ps.add_argument("--title", default=None)
    ps.add_argument("--out", default="out/summary.md")
    ps.set_defaults(fn=cmd_summarize)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"clipbot: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
