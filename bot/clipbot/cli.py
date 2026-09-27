"""clipbot CLI.

    uv run --project bot clipbot plan --source assets/demo-clip.mp4 \\
        --request "the part where I question whether the clip bot will work" \\
        --out out/demo/plan.json
    uv run --project bot clipbot outline --source talk.mp4 --out out/talk/outline.md
    uv run --project bot clipbot reel --source talk.mp4 --minutes 4 --out out/talk/plan.json --render

Captions come from the source's subtitle stream, `--srt FILE`, or
`--transcribe` (faster-whisper; needs `--extra whisper`). `--speakers gemini.txt`
labels unlabelled cues from a Gemini transcript (speakers.py). Word timings
(`--words FILE`, or the `.words.json` that `--transcribe` writes) let `reel` cut
fillers and long pauses without ever clipping a word (cuts.py). `--framing FILE`
sets every card around the moments from one JSON file (framing.py).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import audit as auditmod
from . import captions as cap
from . import cuts
from . import framing as framingmod
from . import llm
from . import outline as outl
from . import plan as planmod
from . import probe as probemod
from . import reel as reelmod
from . import select as sel
from . import silence
from . import speakers as spk
from . import summarize as summ
from . import words as wordsmod


def tool_environment() -> None:
    """Prefer the repo's pinned ffmpeg (scripts/install_ffmpeg.py) over a system
    build, the same rule scripts/check.py applies. cliprender inherits it."""
    tools = planmod.REPO_ROOT / ".tools" / "ffmpeg"
    if tools.is_dir():
        os.environ["PATH"] = str(tools) + os.pathsep + os.environ.get("PATH", "")


def load_cues(a: argparse.Namespace, info: probemod.SourceInfo, out_dir: Path) -> tuple[list[cap.Cue], str, str | None]:
    """(cues, captions_kind, srt_path) from --srt, --transcribe, or the embedded stream."""
    if a.srt:
        return cap.parse_srt(Path(a.srt).read_text(encoding="utf-8")), "srt", a.srt
    if getattr(a, "transcribe", False):
        from . import transcribe as tr  # lazy: only this path needs the optional extra

        srt = Path(out_dir) / (Path(a.source).stem + ".srt")
        if srt.is_file():
            print(f"clipbot: reusing {srt} (delete it to transcribe again)", file=sys.stderr)
            return cap.parse_srt(srt.read_text(encoding="utf-8")), "srt", str(srt)
        return tr.transcribe(a.source, srt, model=a.whisper_model, language=a.language), "srt", str(srt)
    if info.subtitle_stream_index is not None:
        return cap.parse_srt(cap.extract_embedded_srt(a.source, info.subtitle_stream_index)), "embedded", None
    raise RuntimeError("no captions in the source; pass --srt FILE or --transcribe (needs --extra whisper)")


def load_words(a: argparse.Namespace, srt_path: str | None) -> list[wordsmod.Word]:
    """Word timings from --words, else the `.words.json` next to the SRT
    (what --transcribe writes), else none: embedded Meet captions have no words."""
    path = Path(a.words) if getattr(a, "words", None) else None
    if path is None and srt_path:
        sibling = wordsmod.words_path(srt_path)
        if sibling.is_file():
            path = sibling
    if path is None:
        return []
    words = wordsmod.load_words(path)
    print(f"words: {len(words)} timed words from {path}", file=sys.stderr)
    return words


def add_speakers(a: argparse.Namespace, cues: list[cap.Cue]) -> list[cap.Cue]:
    """--speakers: best effort by design (speakers.py); only a missing file is fatal."""
    if not getattr(a, "speakers", None):
        return cues
    text = Path(a.speakers).read_text(encoding="utf-8", errors="replace")
    try:
        offset = spk.parse_clock(a.speakers_offset)
        unlabelled = sum(1 for c in cues if not c.speaker)
        cues, n = spk.align(cues, spk.parse_gemini(text), offset=offset)
        print(f"speakers: labelled {n} of {unlabelled} unlabelled cues from {a.speakers} (offset {offset:g} s)",
              file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - alignment must never sink the run
        print(f"speakers: skipped ({e})", file=sys.stderr)
    return cues


def cmd_plan(a: argparse.Namespace) -> int:
    info = probemod.probe(a.source)
    lo, hi = planmod.PRESET_BOUNDS[a.preset]
    min_s = a.min_seconds if a.min_seconds is not None else lo
    max_s = a.max_seconds if a.max_seconds is not None else hi
    out_path = Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cues, captions_kind, srt_path = load_cues(a, info, out_path.parent)

    windows = sel.best_windows(cues, a.request, min_s, max_s, max_clips=a.max_clips)
    if not windows:
        print("clipbot: nothing matched the request within the length bounds", file=sys.stderr)
        return 3

    summary_path = out_path.with_name("summary.md") if a.summary else None
    plan = planmod.build_plan(
        info, windows, out_dir=str(out_path.parent), preset=a.preset,
        captions_kind=captions_kind, srt_path=srt_path,
        summary_path=str(summary_path) if summary_path else None,
    )
    planmod.validate(plan)
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
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cues, _, _ = load_cues(a, info, out.parent)
    cues = add_speakers(a, cues)
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8")) if a.plan else None
    md = summ.to_markdown(a.title or Path(a.source).stem, info.path, info.duration_seconds, cues, plan=plan)
    out.write_text(md, encoding="utf-8")
    print(f"summary written: {out}")
    return 0


def cmd_outline(a: argparse.Namespace) -> int:
    info = probemod.probe(a.source)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cues, _, _ = load_cues(a, info, out.parent)
    cues = add_speakers(a, cues)
    md = outl.outline_markdown(
        a.title or Path(a.source).stem, info.path, info.duration_seconds, cues, block_seconds=a.block_seconds
    )
    out.write_text(md, encoding="utf-8")
    print(f"outline written: {out} ({len(cues)} cues, {len(outl.blocks(cues, a.block_seconds))} blocks)")
    print("moments skeleton (fill it and pass to `clipbot reel --moments`):")
    print(outl.skeleton_json())
    return 0


def _hms(t: float) -> str:
    """h:mm:ss.s — tenths matter once cuts land on word edges."""
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s + (t - int(t)):04.1f}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _cut_summary(rep: cuts.CutReport) -> str:
    if rep.intact:
        return rep.note
    kept = f"; {_plural(rep.embedded, 'filler')} kept (no pause beside {'it' if rep.embedded == 1 else 'them'})" if rep.embedded else ""
    if not rep.removed_seconds:
        return "nothing to cut" + kept
    parts = []
    if rep.fillers:
        parts.append(_plural(rep.fillers, "filler"))
    if rep.silence_seconds:
        parts.append(f"{rep.silence_seconds:.1f} s silence")
    return f"-{rep.removed_seconds:.1f} s ({', '.join(parts)}) -> {_plural(len(rep.segments), 'segment')}{kept}"


def cmd_reel(a: argparse.Namespace) -> int:
    minutes = a.minutes if a.minutes is not None else 4.0  # target is advisory
    info = probemod.probe(a.source)
    out_path = Path(a.out)
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    cues, captions_kind, srt_path = load_cues(a, info, out_dir)
    cues = add_speakers(a, cues)
    if not cues:
        print("clipbot: the transcript is empty", file=sys.stderr)
        return 2
    words = load_words(a, srt_path)
    snapper = reelmod.snapper_for(cues, words)  # word edges with --words / --transcribe, cue sentences without

    low, _, high = reelmod.target_band(minutes)
    moments: list[reelmod.Moment] = []
    how = "heuristic"
    if a.moments:
        specs = json.loads(Path(a.moments).read_text(encoding="utf-8"))
        if not isinstance(specs, list) or not specs:
            raise ValueError(f"{a.moments}: expected a non-empty JSON list of moments")
        moments = reelmod.moments_from_specs(specs, cues, snapper=snapper)
        how = f"--moments {a.moments}"
    elif a.llm:
        if not llm.has_credentials():
            print("llm: ANTHROPIC_API_KEY is not set; using the heuristic selector", file=sys.stderr)
        else:
            try:
                specs = llm.propose_moments(
                    outl.transcript_text(cues), minutes=minutes, duration=info.duration_seconds,
                    log=lambda msg: print(msg, file=sys.stderr),
                )
                moments = reelmod.fit_moments(reelmod.moments_from_specs(specs, cues, snapper=snapper), minutes)
                if len(moments) < reelmod.MIN_MOMENTS:
                    raise RuntimeError(f"the model proposed only {len(moments)} usable moments")
                how = f"llm ({llm.MODEL})"
            except Exception as e:  # noqa: BLE001 - documented: any failure -> heuristic
                print(f"llm: {e}; using the heuristic selector", file=sys.stderr)
                moments = []
    hand_picked = bool(moments)
    if not moments:
        moments = reelmod.pick_moments(cues, minutes, preset=a.preset, snapper=snapper)
    if not moments:
        print("clipbot: could not find any usable moments", file=sys.stderr)
        return 3
    moments, notes = reelmod.resolve_overlaps(moments, info.duration_seconds)
    for note in notes:
        print(f"snap: {note}", file=sys.stderr)
    if not moments:
        print("clipbot: no moment lies inside the source", file=sys.stderr)
        return 3

    # Lead/tail air, then filler + pause removal (cuts.py). A pause is a gap between words that
    # the audio confirms is silent (silence.py); without word timings the audio is all there is.
    # Every silence of at least cuts.MIN_PAUSE is collected (fillers and the moments' edges anchor
    # to those; --max-silence decides which are shortened), out past the moments' edges so the pause
    # beside a first or last word is seen whole (reel.detection_spans).
    silences: list[list[tuple[float, float]]] | None = None  # None: audio not checked
    if not a.keep_fillers:
        spans = reelmod.detection_spans(moments, lead=a.lead_seconds, tail=a.tail_seconds, duration=info.duration_seconds)
        try:
            silences = silence.detect_silences(info.path, spans, min_seconds=min(cuts.MIN_PAUSE, a.max_silence))
        except RuntimeError as e:
            print(f"cuts: silence detection skipped ({e}); "
                  + ("pauses follow the word timing alone" if words else "pauses are kept"), file=sys.stderr)
        if not words:
            print("cuts: no word timestamps (--words FILE, or --transcribe writes them); fillers kept, "
                  "only pauses longer than the limit are shortened", file=sys.stderr)
    moments, reports = reelmod.cut_moments(
        moments, words, silences, lead=a.lead_seconds, tail=a.tail_seconds, duration=info.duration_seconds,
        fillers=bool(words) and not a.keep_fillers, pauses=not a.keep_fillers,
        max_silence=a.max_silence, keep_pause=a.keep_pause,
    )
    if a.keep_fillers:
        reports = []  # padded only: nothing to report per moment

    given = None
    if a.takeaways:
        given = Path(a.takeaways).read_text(encoding="utf-8").splitlines()
    takeaways = reelmod.takeaway_lines(moments, cues, given=given, hand_picked=hand_picked)

    # --framing FILE (framing.py) overrides the flags below wherever it says something; the
    # card seconds it sets are in place before the runtime line is computed.
    framing = framingmod.load_framing(a.framing) if a.framing else None
    if framing and not a.music and (framing.music_gain_db is not None or framing.music_fade_seconds is not None):
        print(f"framing: music settings in {a.framing} ignored (no --music)", file=sys.stderr)
    title = a.title or Path(a.source).stem
    date = a.date or datetime.date.fromtimestamp(Path(a.source).stat().st_mtime).isoformat()
    summary_path = out_dir / "summary.md"
    plan = reelmod.build_reel_plan(
        info, moments, out_dir=str(out_dir), title=title, date=date, preset=a.preset,
        captions_kind=captions_kind, srt_path=srt_path, summary_path=str(summary_path),
        lead=a.lead_seconds, tail=a.tail_seconds, takeaways=takeaways, transition=a.transition,
        music=planmod.contract_path(a.music) if a.music else None, framing=framing,
    )
    planmod.validate(plan)
    title = plan["output"]["reel"]["intro"]["title"]  # what the viewer sees, framing applied
    out_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    moments_path = out_dir / "moments.json"
    moments_path.write_text(json.dumps([m.spec() for m in moments], indent=2) + "\n", encoding="utf-8")
    summary_path.write_text(
        summ.to_markdown(title, info.path, info.duration_seconds, cues, plan=plan), encoding="utf-8"
    )
    for n, m in enumerate(moments, 1):
        print(f"{n}\t[{outl.clock(m.start)}-{outl.clock(m.end)}]\t{m.lesson or m.title}\t{m.why}")
        detail = []
        if m.requested and (abs(m.requested[0] - m.start) > 0.05 or abs(m.requested[1] - m.end) > 0.05):
            detail.append(f"asked {_hms(m.requested[0])}-{_hms(m.requested[1])} -> snapped {_hms(m.start)}-{_hms(m.end)}")
        if reports:
            detail.append(f"cuts: {_cut_summary(reports[n - 1])}")
        if detail:
            print("\t\t" + " · ".join(detail))
    total = reelmod.plan_runtime(plan)  # intro + opening + cards + kept + closing + outro, as on the intro card
    shown = summ._ts(round(total))
    removed = sum(r.removed_seconds for r in reports)
    cut_line = ""
    if reports:
        fillers = sum(r.fillers for r in reports)
        embedded = sum(r.embedded for r in reports)
        sil = sum(r.silence_seconds for r in reports)
        cut_line = f"; cuts removed {removed:.1f} s ({fillers} fillers, {sil:.1f} s silence)"
        if embedded:
            cut_line += f"; {_plural(embedded, 'filler')} kept: no pause beside {'it' if embedded == 1 else 'them'} to cut in"
    if a.minutes is None and how.startswith("--moments"):
        print(f"reel: {len(moments)} moments via {how}; runtime {shown} incl. all cards{cut_line} "
              "(length follows your moments; pass --minutes for a ±20% target check)")
    else:
        band = "within" if low <= total <= high else "OUTSIDE"
        print(f"reel: {len(moments)} moments via {how}; runtime {shown} incl. all cards{cut_line} "
              f"({band} {minutes:g} min ±20%, best effort)")
    print(f"plan written: {out_path}")
    print(f"moments written: {moments_path}")
    print(f"summary written: {summary_path}")
    if a.render:
        return render(out_path)
    return 0


def cmd_audit_plan(a: argparse.Namespace) -> int:
    """Measure the audio on both sides of every segment edge (audit.py). Exit 1
    when any edge has speech on both sides, so the check works in a script."""
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    words = wordsmod.load_words(a.words) if a.words else None
    readings = auditmod.audit_plan(plan, a.audio, window=a.window_ms / 1000.0, words=words)
    print(auditmod.report(readings, threshold_db=a.threshold_db, window=a.window_ms / 1000.0, show_all=a.all))
    return 1 if auditmod.offenders(readings, a.threshold_db) else 0


def render(plan_path: Path) -> int:
    """Hand the plan to Ramsey's renderer in its own project and echo what it reports."""
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv not found on PATH; run: uv run --project render cliprender <plan> --root <repo>")
    cmd = [uv, "run", "--project", "render", "cliprender", str(plan_path.resolve()),
           "--root", str(planmod.REPO_ROOT), "--overwrite"]
    print("render: " + subprocess.list2cmdline(cmd), file=sys.stderr)
    print("render: probing the source and cutting clips; on a 1-2 h recording the first clip line "
          "can take a few minutes and the whole reel about ten", file=sys.stderr)
    # `uv run --project bot` exports VIRTUAL_ENV=bot/.venv; the nested uv for render/
    # would warn about it on every run. The renderer must resolve its own venv.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    reel_path = None
    with subprocess.Popen(cmd, cwd=planmod.REPO_ROOT, env=env, stdout=subprocess.PIPE, text=True,
                          encoding="utf-8", errors="replace") as proc:
        assert proc.stdout is not None
        for line in proc.stdout:  # stream: clip encodes take a while on a long source
            print(line, end="")
            if line.startswith("reel\t"):
                reel_path = line.rstrip("\n").split("\t")[1]
    if proc.returncode:
        print(f"render: cliprender exited {proc.returncode}", file=sys.stderr)
        return proc.returncode
    if reel_path:
        print(f"reel written: {reel_path}")
    else:
        print("render: clips written; this renderer did not report a reel (render/ reel support pending)")
    return 0


def add_caption_args(p: argparse.ArgumentParser, speakers: bool = True) -> None:
    p.add_argument("--srt", default=None, help="sidecar SRT when the source has no captions")
    p.add_argument("--words", default=None,
                   help="word timings JSON [{start,end,word}] for --srt (--transcribe writes <name>.words.json itself)")
    p.add_argument("--transcribe", action="store_true",
                   help="no captions? transcribe with faster-whisper (uv ... --extra whisper); SRT is kept next to --out")
    p.add_argument("--whisper-model", default="base", help="faster-whisper model size (default: base)")
    p.add_argument("--language", default=None, help="language code for --transcribe (default: auto)")
    if speakers:
        p.add_argument("--speakers", default=None, help="Gemini 'Notes by Gemini' transcript to take speaker names from")
        p.add_argument("--speakers-offset", default="0",
                       help="subtract this from the notes' clock (seconds or h:mm:ss) when it started before the video")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="clipbot", description="request -> edit plan")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("plan", help="produce a validated edit plan for one request")
    pp.add_argument("--source", required=True)
    pp.add_argument("--request", required=True, help="what the clip should be about")
    pp.add_argument("--out", default="out/plan.json")
    pp.add_argument("--preset", default="internal", choices=sorted(planmod.PRESET_BOUNDS))
    pp.add_argument("--min-seconds", type=float, default=None)
    pp.add_argument("--max-seconds", type=float, default=None)
    pp.add_argument("--max-clips", type=int, default=1)
    add_caption_args(pp, speakers=False)
    pp.add_argument("--summary", action="store_true", help="also write summary.md next to the plan")
    pp.set_defaults(fn=cmd_plan)

    ps = sub.add_parser("summarize", help="write executive summary + full transcript markdown")
    ps.add_argument("--source", required=True)
    add_caption_args(ps)
    ps.add_argument("--plan", default=None, help="edit plan JSON to list clips from")
    ps.add_argument("--title", default=None)
    ps.add_argument("--out", default="out/summary.md")
    ps.set_defaults(fn=cmd_summarize)

    po = sub.add_parser("outline", help="timestamped transcript in 30 s blocks + a moments skeleton")
    po.add_argument("--source", required=True)
    add_caption_args(po)
    po.add_argument("--title", default=None)
    po.add_argument("--block-seconds", type=int, default=outl.BLOCK_SECONDS)
    po.add_argument("--out", default="out/outline.md")
    po.set_defaults(fn=cmd_outline)

    pr = sub.add_parser("reel", help="N-minute summary reel plan: intro + chapter cards + moments + takeaways")
    pr.add_argument("--source", required=True)
    pr.add_argument("--minutes", type=float, default=None,
                    help="target reel length incl. cards (±20 %%, best effort; default 4). With --moments the reel is as long as your moments")
    pr.add_argument("--out", default="out/reel/plan.json")
    pr.add_argument("--preset", default="internal", choices=sorted(planmod.PRESET_BOUNDS))
    add_caption_args(pr)
    pr.add_argument("--moments", default=None,
                    help="JSON list of {start,end,title,lesson,context,lines,why}; see clipbot outline")
    pr.add_argument("--llm", action="store_true", help="pick moments with Claude when ANTHROPIC_API_KEY is set")
    pr.add_argument("--title", default=None, help="intro slide title (default: source file stem)")
    pr.add_argument("--date", default=None, help="'Recorded' date on the intro (default: source file date)")
    pr.add_argument("--takeaways", default=None,
                    help="text file, one takeaway per line, for the closing cards (default: the moments' lessons)")
    pr.add_argument("--framing", default=None,
                    help="JSON {title, date, what_you_will_learn, takeaways, outro, *_seconds, music_gain_db, "
                         "music_fade_seconds, transition}: the cards around the moments, overriding the flags "
                         "(clipbot/framing.py)")
    pr.add_argument("--keep-fillers", action="store_true",
                    help="do not cut filler words or long pauses inside moments")
    pr.add_argument("--max-silence", type=float, default=cuts.MAX_SILENCE,
                    help=f"pauses longer than this many seconds are shortened (default {cuts.MAX_SILENCE})")
    pr.add_argument("--keep-pause", type=float, default=cuts.KEEP_PAUSE,
                    help=f"what a shortened pause becomes, in seconds (default {cuts.KEEP_PAUSE}; needs word timings)")
    pr.add_argument("--lead-seconds", type=float, default=cuts.LEAD_SECONDS,
                    help=f"air before the first word of a moment (default {cuts.LEAD_SECONDS})")
    pr.add_argument("--tail-seconds", type=float, default=cuts.TAIL_SECONDS,
                    help=f"air after the last word of a moment (default {cuts.TAIL_SECONDS})")
    pr.add_argument("--transition", default="dip", choices=["cut", "dip", "dissolve"],
                    help="join between cards and clips (default dip, 0.4 s)")
    pr.add_argument("--music", default=None,
                    help="music bed under the cards (audio file you have the rights to; see contract/README.md)")
    pr.add_argument("--render", action="store_true", help="also run cliprender on the plan")
    pr.set_defaults(fn=cmd_reel)

    pa = sub.add_parser("audit-plan", help="measure the audio on both sides of every segment edge of a plan")
    pa.add_argument("plan", help="edit plan JSON (clipbot reel --out)")
    pa.add_argument("--audio", required=True,
                    help="the source's audio: a 16-bit PCM WAV is read directly, anything else is decoded with ffmpeg")
    pa.add_argument("--words", default=None, help="word timings JSON, to name the word whisper puts at an offending edge")
    pa.add_argument("--threshold-db", type=float, default=auditmod.SPEECH_DB,
                    help=f"peak above this on both sides of an edge = the cut runs through sound (default {auditmod.SPEECH_DB:g})")
    pa.add_argument("--window-ms", type=float, default=auditmod.WINDOW * 1000,
                    help=f"milliseconds measured on each side (default {auditmod.WINDOW * 1000:g})")
    pa.add_argument("--all", action="store_true", help="print every edge, not only the offenders")
    pa.set_defaults(fn=cmd_audit_plan)

    a = p.parse_args(argv)
    tool_environment()
    for stream in (sys.stdout, sys.stderr):
        # Titles are transcript text; a Windows cp1252 console must not crash the run.
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, AttributeError):
                pass
    try:
        return a.fn(a)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"clipbot: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
