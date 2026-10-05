"""Apple Vision OCR worker for redact.vision_frame: one JSON request per line on stdin
({"pngs": [...], "tiles": [[x, y, w, h], ...]}), one JSON reply per line on stdout
({"words": [[text, box, conf, line], ...]} or {"error": "..."}). A separate process so
redact.py can replace it every few dozen frames (see vision_frame)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .redact import vision_words


def main() -> int:
    for line in sys.stdin:
        try:
            req = json.loads(line)
            words = []
            for n, (png, rect) in enumerate(zip(req["pngs"], req["tiles"])):
                words += [[w.text, list(w.box), w.conf, list(w.line)] for w in vision_words(Path(png), tuple(rect), tile=n)]
            reply = {"words": words}
        except Exception as e:  # noqa: BLE001 - reported to the parent, which fails closed
            reply = {"error": str(e)}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
