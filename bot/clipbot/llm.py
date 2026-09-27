"""Optional model-based moment selection (`clipbot reel --llm`).

Why: the heuristic scores word density; a model reads for meaning ("this is
where they decided X"). We send the outline (30 s blocks, outline.py) to Claude
in chunks of at most ~8k tokens, ask each chunk for candidate moments as JSON
(structured output, so the shape is guaranteed), validate the numbers, then
snap and fit them exactly like human `--moments` (reel.py). Since v1.2 every
proposal also carries a `lesson` and a `context` line, written for "a founder
who missed the session": the chapter card leads with the lesson, not with a
quote (lessons.py). Any failure, no key, no SDK, API error, refusal, bad
numbers, falls back to the heuristic with one printed line: a reel is never
blocked on the network.

Needs the `llm` extra and a credential:

    ANTHROPIC_API_KEY=... uv run --project bot --extra llm clipbot reel --llm ...

Requests opt into Anthropic's server-side refusal fallbacks (the API re-runs a
declined request on a fallback model inside the same call), so a transcript
that trips a classifier still gets an answer rather than an error.
"""

from __future__ import annotations

import json
import math
import os
from typing import Callable

MODEL = os.environ.get("CLIPBOT_LLM_MODEL", "claude-opus-5-5")  # Opus 5.5; override per run
CHUNK_CHARS = 28_000  # ~7-8k tokens of outline text at ~4 chars per token
MIN_LEN, MAX_LEN = 10.0, 90.0  # a proposal outside this is a model slip, not a moment

SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number", "description": "seconds from the start of the recording"},
                    "end": {"type": "number", "description": "seconds from the start of the recording"},
                    "title": {"type": "string", "description": "card title, at most 80 characters"},
                    "lesson": {
                        "type": "string",
                        "description": "what a viewer who missed the session learns from this moment, as a statement, at most 80 characters",
                    },
                    "context": {
                        "type": "string",
                        "description": "what the room already knew that the viewer needs first, at most 120 characters",
                    },
                    "why": {"type": "string", "description": "one line: Decision / Demo / Q&A ..., at most 120 characters"},
                    "score": {"type": "number", "description": "1 (weak) to 10 (must keep)"},
                },
                "required": ["start", "end", "title", "lesson", "context", "why", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}


def has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def chunk_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split on block boundaries (blank lines) so no 30 s block is cut in half."""
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for block in text.split("\n\n"):
        if cur and size + len(block) + 2 > limit:
            chunks.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(block)
        size += len(block) + 2
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def prompt(chunk: str, minutes: float, want: int, n: int, total: int) -> str:
    return (
        f"You are choosing moments for a {minutes:g}-minute summary reel of a recorded work session. "
        "The viewer is a founder who missed the session: each moment must teach them something they "
        f"can use. Below is part {n} of {total} of the transcript in 30-second blocks; each block starts "
        "with [h:mm:ss], the time from the start of the recording.\n\n"
        f"Pick up to {want} self-contained moments from THIS part that such a viewer would most want to "
        "see: decisions and the reasoning behind them, demos, quotable claims, a question together with "
        "its answer, lessons learned. Skip greetings, logistics, banter and screen-share fumbling. Each "
        "moment is 15-60 seconds, starts where a sentence starts, ends where one ends, and covers one "
        "idea. Give start and end in seconds from the start of the recording (convert the [h:mm:ss] "
        "stamps; edges may fall inside a block); a title of at most 80 characters; a lesson of at most "
        "80 characters (the one thing the viewer learns, as a statement they could repeat); a context "
        "line of at most 120 characters (what the room already knew that the viewer needs first); a "
        "one-line why of at most 120 characters (for example 'Decision', 'Demo', 'Q&A: who asked'); and "
        "a score from 1 (weak) to 10 (must keep). Return an empty list if nothing here is worth keeping."
        f"\n\nTranscript part {n}:\n\n{chunk}"
    )


def validate_specs(specs: list[dict], duration: float) -> list[dict]:
    """Keep only proposals with sane numbers; everything else is silently dropped."""
    out: list[dict] = []
    for m in specs:
        try:
            s, e = float(m["start"]), float(m["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= s < e <= duration + 1.0) or not (MIN_LEN <= e - s <= MAX_LEN):
            continue
        try:
            score = float(m.get("score", 5) or 5)
        except (TypeError, ValueError):
            score = 5.0
        spec = {
            "start": s,
            "end": min(e, duration),
            "title": str(m.get("title") or "")[:80],
            "why": str(m.get("why") or "")[:120],
            "score": score,
        }
        if m.get("lesson"):
            spec["lesson"] = str(m["lesson"])[:80]
        if m.get("context"):
            spec["context"] = str(m["context"])[:120]
        out.append(spec)
    return out


def propose_moments(
    outline_text: str,
    *,
    minutes: float,
    duration: float,
    client=None,
    model: str = MODEL,
    log: Callable[[str], None] | None = None,
) -> list[dict]:
    """Ask the model for candidate moments; returns validated --moments records.

    `client` is injectable for tests; by default it is `anthropic.Anthropic()`,
    which reads ANTHROPIC_API_KEY. Raises on any failure; the caller falls back.
    """
    log = log or (lambda msg: None)
    if client is None:
        try:
            import anthropic  # optional extra
        except ImportError as e:
            raise RuntimeError(
                "the anthropic SDK is not installed; install with: uv run --project bot --extra llm clipbot ..."
            ) from e
        client = anthropic.Anthropic()
    chunks = chunk_text(outline_text) or [""]
    k = max(2, round(minutes * 60 / 35))
    want = max(2, math.ceil(k / len(chunks)) + 1)
    specs: list[dict] = []
    for n, chunk in enumerate(chunks, 1):
        log(f"llm: part {n}/{len(chunks)} ({len(chunk)} chars) -> {model}")
        resp = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": prompt(chunk, minutes, want, n, len(chunks))}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("the model declined the request")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("the model's answer was cut off")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        data = json.loads(text)
        found = data.get("moments", []) if isinstance(data, dict) else []
        log(f"llm: part {n} proposed {len(found)} moments")
        specs.extend(found)
    return validate_specs(specs, duration)
