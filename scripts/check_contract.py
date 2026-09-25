"""Validate contract/edit-plan.schema.json and every example against it.

Also asserts the schema rejects a set of known-bad plans, so a loosened schema
fails the gate. Run via `make check` (uses `uv run --with jsonschema`).
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "contract" / "edit-plan.schema.json"
EXAMPLES = sorted(glob.glob(str(ROOT / "contract" / "examples" / "*.json")))


def minimal(**overrides: object) -> dict:
    plan = {
        "version": "1",
        "source": {"path": "assets/demo-clip.mp4"},
        "output": {"dir": "out"},
        "clips": [{"id": "c1", "takeaway": "t", "segments": [{"start": 0, "end": 1}]}],
    }
    plan.update(overrides)
    return plan


BAD_PLANS = {
    "wrong version": minimal(version="2"),
    "srt without path": minimal(source={"path": "a", "captions": {"kind": "srt"}}),
    "bad clip id": minimal(clips=[{"id": "Clip 1", "takeaway": "t", "segments": [{"start": 0, "end": 1}]}]),
    "unknown output field": minimal(output={"dir": "o", "fps": 30}),
    "empty clips": minimal(clips=[]),
    "missing takeaway": minimal(clips=[{"id": "c1", "segments": [{"start": 0, "end": 1}]}]),
    "negative start": minimal(clips=[{"id": "c1", "takeaway": "t", "segments": [{"start": -1, "end": 1}]}]),
}


def main() -> int:
    schema = json.loads(SCHEMA.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    failures = 0

    if not EXAMPLES:
        print("contract: FAIL no examples found")
        return 1
    for path in EXAMPLES:
        errors = list(validator.iter_errors(json.loads(Path(path).read_text())))
        rel = Path(path).relative_to(ROOT)
        if errors:
            failures += 1
            print(f"contract: FAIL {rel}: {errors[0].message}")
        else:
            print(f"contract: ok   {rel}")

    for name, plan in BAD_PLANS.items():
        if list(validator.iter_errors(plan)):
            print(f"contract: ok   rejects '{name}'")
        else:
            failures += 1
            print(f"contract: FAIL schema accepted bad plan '{name}'")

    # Semantic checks the schema cannot express.
    for path in EXAMPLES:
        plan = json.loads(Path(path).read_text())
        ids = [c["id"] for c in plan["clips"]]
        if len(ids) != len(set(ids)):
            failures += 1
            print(f"contract: FAIL duplicate clip ids in {Path(path).name}")
        dur = plan["source"].get("duration_seconds")
        for clip in plan["clips"]:
            for seg in clip["segments"]:
                if seg["end"] <= seg["start"] or (dur and seg["end"] > dur):
                    failures += 1
                    print(f"contract: FAIL bad segment {seg} in {Path(path).name}/{clip['id']}")

    print("contract: OK" if failures == 0 else f"contract: {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
