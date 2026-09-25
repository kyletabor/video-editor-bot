import json
import subprocess
import sys
from pathlib import Path

import pytest

from clipbot import cli
from clipbot.plan import build_plan, load_schema, validate
from clipbot.probe import SourceInfo, summarize
from clipbot.select import Window

REPO = Path(__file__).resolve().parents[2]
DEMO = REPO / "assets" / "demo-clip.mp4"


def test_summarize_ffprobe_json():
    j = {
        "format": {"duration": "72.000000"},
        "streams": [
            {"index": 0, "codec_type": "video"},
            {"index": 1, "codec_type": "audio"},
            {"index": 2, "codec_type": "subtitle"},
        ],
    }
    info = summarize(j, "x.mp4")
    assert info.duration_seconds == 72.0
    assert info.has_video and info.has_audio
    assert info.subtitle_stream_index == 0


def test_build_plan_validates_against_contract():
    info = SourceInfo("assets/demo-clip.mp4", 72.0, True, True, 0)
    plan = build_plan(info, [Window(36.0, 52.0, 9.0, "Questioning whether the clip bot idea will work.", (9, 10, 11, 12))], "out/demo")
    validate(plan, load_schema())
    assert plan["clips"][0]["id"].startswith("clip-01-")
    assert plan["output"]["aspect"] == "16:9"
    assert plan["source"]["captions"] == {"kind": "embedded"}


def test_validate_rejects_segment_past_duration():
    info = SourceInfo("a.mp4", 10.0, True, True, None)
    plan = build_plan(info, [Window(2.0, 12.0, 1.0, "x.", (0,))], "out", captions_kind="none")
    with pytest.raises(ValueError):
        validate(plan)


def _tools_present() -> bool:
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not (DEMO.exists() and _tools_present()), reason="needs ffmpeg + assets/demo-clip.mp4")
def test_cli_end_to_end_on_demo_clip(tmp_path):
    out = tmp_path / "plan.json"
    rc = cli.main([
        "plan", "--source", str(DEMO),
        "--request", "the part where I question whether Ramsey's clip bot idea will work",
        "--max-seconds", "45", "--out", str(out),
    ])
    assert rc == 0
    plan = json.loads(out.read_text())
    validate(plan)
    seg = plan["clips"][0]["segments"][0]
    assert seg["start"] <= 36 and seg["end"] >= 48
    assert 15 <= seg["end"] - seg["start"] <= 45
