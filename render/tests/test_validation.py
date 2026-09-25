"""Plan rejection, geometry and transactional publication checks."""

import json
from pathlib import Path

import pytest

from cliprender.captions import Cue
from cliprender.media import RenderError, geometry, inspect_media
from cliprender.renderer import (
    RecoveryError,
    decode_window,
    load_plan,
    normalize_embedded,
    publish,
    render_plan,
    staged_job,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def plan():
    return {
        "version": "1",
        "source": {"path": "not-opened.mp4", "duration_seconds": 5},
        "output": {"dir": "not-created"},
        "clips": [{"id": "one", "takeaway": "One thing", "segments": [{"start": 0, "end": 1}]}],
    }


@pytest.mark.parametrize(
    "case", ["version", "extra", "reverse", "duplicate", "bounds", "nan", "inf"]
)
def test_invalid_plan_rejected_before_tools_or_outputs(tmp_path, plan, case):
    if case == "version":
        plan["version"] = "2"
    elif case == "extra":
        plan["unrecognized"] = True
    elif case == "reverse":
        plan["clips"][0]["segments"][0]["end"] = 0
    elif case == "duplicate":
        plan["clips"].append(plan["clips"][0].copy())
    elif case == "bounds":
        plan["clips"][0]["segments"][0]["end"] = 6
    else:
        plan["clips"][0]["segments"][0]["end"] = float(case)
    plan["output"]["dir"] = str(tmp_path / "outputs")
    file = tmp_path / "plan.json"
    file.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(RenderError):
        render_plan(file, root=ROOT, ffmpeg="must-not-start", ffprobe="must-not-start")
    assert not (tmp_path / "outputs").exists()


@pytest.mark.parametrize("example", list((ROOT / "contract/examples").glob("*.json")))
def test_authoritative_examples_validate(example):
    assert load_plan(example, ROOT)["version"] == "1"


@pytest.mark.parametrize(
    "aspect,focus,expected",
    [
        ("16:9", "center", (640, 360)),
        ("9:16", "left", (202, 360)),
        ("1:1", "right", (360, 360)),
    ],
)
def test_geometry_crops_without_upscaling(aspect, focus, expected):
    graph, dimensions = geometry({"width": 640, "height": 360}, aspect, focus, 1080)
    assert dimensions == expected
    if aspect != "16:9":
        assert "crop=" in graph


def test_orientation_anamorphic_and_ceiling():
    _, dimensions = geometry({"width": 1920, "height": 1080}, "16:9", "center", 720)
    assert dimensions == (1280, 720)
    _, dimensions = geometry(
        {"width": 640, "height": 360, "side_data_list": [{"rotation": 90}]}, "16:9", "center", 1080
    )
    assert dimensions == (360, 640)
    _, dimensions = geometry(
        {"width": 720, "height": 576, "sample_aspect_ratio": "16:15"}, "16:9", "center", 1080
    )
    assert dimensions == (720, 540)


@pytest.mark.parametrize("change", ["hdr", "multitrack", "audio-only", "surround", "interlace"])
def test_unsupported_media_is_explicit(change):
    video = {"codec_type": "video", "width": 640, "height": 360}
    audio = {"codec_type": "audio", "channels": 2}
    data = {"streams": [video, audio]}
    if change == "hdr":
        video["color_transfer"] = "smpte2084"
    elif change == "multitrack":
        data["streams"].append(audio.copy())
    elif change == "audio-only":
        data["streams"].remove(video)
    elif change == "surround":
        audio["channels"] = 6
    else:
        video["field_order"] = "tt"
    with pytest.raises(RenderError, match="Unsupported"):
        inspect_media(data)


def test_failed_publication_rolls_back_new_files(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    first, second = stage / "first", stage / "second"
    first.write_text("new one")
    second.write_text("new two")
    target_one, target_two = tmp_path / "one.mp4", tmp_path / "two.mp4"
    target_two.write_text("other job won")
    with pytest.raises(FileExistsError):
        publish([(first, target_one), (second, target_two)], False, stage)
    assert not target_one.exists()
    assert target_two.read_text() == "other job won"


def test_failed_overwrite_restores_prior_results(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    first = stage / "first"
    first.write_text("new one")
    target_one, target_two = tmp_path / "one.mp4", tmp_path / "two.mp4"
    target_one.write_text("old one")
    target_two.write_text("old two")
    with pytest.raises(FileNotFoundError):
        publish([(first, target_one), (stage / "missing", target_two)], True, stage)
    assert target_one.read_text() == "old one"
    assert target_two.read_text() == "old two"


def test_input_cannot_be_overwritten(tmp_path, plan):
    source = tmp_path / "one.mp4"
    source.write_bytes(b"original source")
    plan["source"]["path"] = str(source)
    plan["output"]["dir"] = str(tmp_path)
    file = tmp_path / "plan.json"
    file.write_text(json.dumps(plan))
    with pytest.raises(RenderError, match="overwrite an input"):
        render_plan(file, root=ROOT, overwrite=True)
    assert source.read_bytes() == b"original source"


def test_decode_window_seeks_between_frames_and_never_past_selected_audio():
    from fractions import Fraction

    times = [Fraction(k, 10) for k in range(60)]  # 10 fps, origin 0
    audio = {"sample_rate": 48000}

    def selected(*segments):
        return [
            (int(s * 10 + 0.999999), int(e * 10 + 0.999999), Fraction(str(s)), Fraction(str(e)), 0)
            for s, e in segments
        ]

    # Midpoint 1.25 sits 50 ms from both frames and no later than the segment start.
    flags, base, audio_base = decode_window(selected((1.25, 1.65)), times, Fraction(0), audio, True)
    assert flags == ["-ss", "1.250000", "-t", "1.400000"]
    assert (base, audio_base) == (13, 60000)
    # A start just after a frame would put the midpoint past it: keep that frame instead.
    flags, base, audio_base = decode_window(
        selected((1.2001, 1.6)), times, Fraction(0), audio, True
    )
    assert flags[:2] == ["-ss", "1.150000"] and (base, audio_base) == (12, 55200)
    # Reordered segments seek to the earliest one; a segment from frame 0 only bounds the end.
    flags, base, _ = decode_window(
        selected((3.25, 3.65), (1.25, 1.65)), times, Fraction(0), None, True
    )
    assert flags == ["-ss", "1.250000", "-t", "3.400000"] and base == 13
    assert decode_window(selected((0, 1)), times, Fraction(0), audio, True) == (
        ["-t", "2.000000"],
        0,
        0,
    )
    # A nonzero origin is subtracted from the flags but kept in the indices.
    flags, base, audio_base = decode_window(
        selected((1.25, 1.65)), [t + 5 for t in times], Fraction(5), audio, True
    )
    assert flags == ["-ss", "1.250000", "-t", "1.400000"] and (base, audio_base) == (13, 60000)
    assert decode_window(selected((1.25, 1.65)), times, Fraction(0), audio, False) == ([], 0, 0)


def test_preroll_embedded_captions_are_clipped_to_common_origin():
    assert normalize_embedded(
        [Cue(1, 4, "before"), Cue(4, 6, "crosses"), Cue(6, 7, "after")], 5
    ) == [Cue(0, 1, "crosses"), Cue(1, 2, "after")]


def test_incomplete_rollback_preserves_recovery_files(tmp_path, monkeypatch):
    import os

    replace = os.replace
    target = tmp_path / "old.mp4"
    target.write_text("precious original")

    def fail_restore(source, dest):
        if Path(source).name == "previous-0":
            raise PermissionError("simulated locked result")
        replace(source, dest)

    monkeypatch.setattr(os, "replace", fail_restore)
    with pytest.raises(RecoveryError, match="Recovery files retained"), staged_job(tmp_path) as job:
        new = job / "new"
        new.write_text("replacement")
        publish([(new, target), (job / "missing", tmp_path / "second")], True, job)
    assert (job / "previous-0").read_text() == "precious original"
