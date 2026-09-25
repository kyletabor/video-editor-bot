"""Assemble and validate an edit plan (contract/edit-plan.schema.json)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema

from .probe import SourceInfo
from .select import Window

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "contract" / "edit-plan.schema.json"

PRESET_ASPECT = {"internal": "16:9", "linkedin": "16:9", "shorts": "9:16", "email": "16:9"}
PRESET_BOUNDS = {"internal": (15, 120), "linkedin": (15, 90), "shorts": (15, 60), "email": (15, 60)}


def load_schema(path: Path = SCHEMA_PATH) -> dict:
    return json.loads(path.read_text())


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "clip"


def build_plan(
    source: SourceInfo,
    windows: list[Window],
    out_dir: str,
    preset: str = "internal",
    captions_kind: str = "embedded",
    srt_path: str | None = None,
    summary_path: str | None = None,
) -> dict:
    src: dict = {"path": source.path.replace("\\", "/"), "duration_seconds": round(source.duration_seconds, 3)}
    if captions_kind == "srt":
        src["captions"] = {"kind": "srt", "path": srt_path}
    else:
        src["captions"] = {"kind": captions_kind}
    plan: dict = {
        "version": "1",
        "source": src,
        "output": {
            "dir": out_dir.replace("\\", "/"),
            "preset": preset,
            "aspect": PRESET_ASPECT[preset],
            "captions": "burn_in" if captions_kind != "none" else "none",
        },
        "clips": [],
    }
    for n, w in enumerate(windows, 1):
        plan["clips"].append(
            {
                "id": f"clip-{n:02d}-{slug(w.takeaway)}",
                "takeaway": w.takeaway,
                "segments": [{"start": round(w.start, 3), "end": round(w.end, 3)}],
                "trim_silence": True,
            }
        )
    if summary_path:
        plan["summary"] = {"path": summary_path.replace("\\", "/")}
    return plan


def validate(plan: dict, schema: dict | None = None) -> None:
    """Schema + the semantic rules the renderer will enforce. Raises ValueError."""
    schema = schema or load_schema()
    try:
        jsonschema.Draft202012Validator(schema).validate(plan)
    except jsonschema.ValidationError as e:
        raise ValueError(f"plan violates contract: {e.message}") from e
    ids = [c["id"] for c in plan["clips"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate clip ids")
    dur = plan["source"].get("duration_seconds")
    for c in plan["clips"]:
        for s in c["segments"]:
            if s["end"] <= s["start"]:
                raise ValueError(f"{c['id']}: end <= start")
            if dur and s["end"] > dur + 1e-6:
                raise ValueError(f"{c['id']}: segment ends after source duration")
