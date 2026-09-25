import json
import subprocess
from pathlib import Path

import pytest

from clipbot import cli
from clipbot.captions import Cue
from clipbot.outline import blocks, clock, format_block, outline_markdown, skeleton_json, transcript_text

REPO = Path(__file__).resolve().parents[2]
DEMO_MP4 = REPO / "assets" / "demo-clip.mp4"


def test_clock_is_always_h_mm_ss():
    assert clock(0) == "0:00:00"
    assert clock(750) == "0:12:30"
    assert clock(3725.9) == "1:02:05"


def test_blocks_group_cues_by_thirty_seconds():
    cues = [Cue(0, 4, "a", "K"), Cue(4, 8, "b", "K"), Cue(31, 35, "c", "R"), Cue(64, 68, "d", "R")]
    assert [(t, len(cs)) for t, cs in blocks(cues)] == [(0, 2), (30, 1), (60, 1)]


def test_format_block_labels_speakers_and_continuation_lines():
    cues = [Cue(0, 4, "Okay, this is a", "Kyle Tabor"), Cue(4, 8, "recording.", "Kyle Tabor"), Cue(8, 12, "Nice.", "Ramsey Jamoul")]
    out = format_block(0, cues)
    assert out.splitlines() == [
        "[0:00:00] Kyle Tabor: Okay, this is a recording.",
        "          Ramsey Jamoul: Nice.",
    ]


def test_format_block_without_speakers_and_with_artifacts():
    cues = [Cue(30, 34, "So, um, real - quick", None), Cue(34, 38, "we ship it.", None)]
    assert format_block(30, cues) == "[0:00:30] So, real quick we ship it."


def test_transcript_text_and_markdown_sections():
    cues = [Cue(0, 4, "Hello there.", "K"), Cue(40, 44, "Second block.", "R")]
    text = transcript_text(cues)
    assert text.startswith("[0:00:00] K: Hello there.") and "\n\n[0:00:30] R: Second block." in text
    md = outline_markdown("Talk", "x.mp4", 90, cues)
    for h in ("# Talk — outline", "Speakers: K, R", "## Transcript", "## Moments skeleton", "```json"):
        assert h in md
    skeleton = json.loads(skeleton_json())
    assert isinstance(skeleton, list) and {"start", "end", "title", "lines", "why"} <= set(skeleton[0])
    assert skeleton_json() in md


def _tools_present() -> bool:
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not (DEMO_MP4.exists() and _tools_present()), reason="needs ffmpeg + assets/demo-clip.mp4")
def test_cli_outline_on_demo_clip(tmp_path, capsys):
    out = tmp_path / "outline.md"
    rc = cli.main(["outline", "--source", str(DEMO_MP4), "--out", str(out)])
    assert rc == 0
    md = out.read_text(encoding="utf-8")
    assert "[0:00:00] Kyle Tabor:" in md and "[0:00:30]" in md and "[0:01:00]" in md
    stdout = capsys.readouterr().out
    assert "outline written" in stdout
    assert json.loads(stdout.split("clipbot reel --moments`):", 1)[1])  # skeleton printed after the note
