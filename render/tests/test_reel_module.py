"""Code-drawn transitions (`transition.kind: module`): the schema rule, the module contract
(`render(a, b, t, state)`), the colour round trip of a bridge, the error paths, and a reel
through the real CLI that is timed exactly like the same plan with a dissolve.

The fixture transition is a hard left-to-right wipe: at progress `t` the columns left of
`t * width` show the incoming picture and the rest the outgoing one, so a test can tell which
frame went where. It also logs every call next to itself, so a test can read what the
renderer handed it. The reel is the one of the music tests: clip A (luma 200) meets clip B
(luma 100) at 15.4 s, which a 0.4 s transition at 10 fps bridges with frames 154 to 157.
"""

import importlib.util
import json
import sys
from fractions import Fraction
from pathlib import Path

import pytest
from test_integration import HEIGHT, RATE, ROOT, WIDTH, audio, cli, ffmpeg, probe
from test_reel import rows
from test_reel_music import music_plan, needs_tools

from cliprender.media import RenderError, Tools
from cliprender.reel import (
    CARD_MATRIX,
    COLOR_TAGS,
    Music,
    Style,
    effective_transition,
    headroom_gain,
    piece_encoder_flags,
    reel_style,
)
from cliprender.renderer import load_plan, render_plan
from cliprender.transitions import bridge_progress, encode_module_bridge, load_transition

needs_numpy = pytest.mark.skipif(
    importlib.util.find_spec("numpy") is None,
    reason="Module transitions need the renderer's styles extra (numpy)",
)

LOGGING = """
import json
from pathlib import Path


def note(a, b, t, state):
    fresh = "seen" not in state
    state["seen"] = True
    entry = {
        "t": t,
        "index": state["frame_index"],
        "frames": state["n_frames"],
        "size": list(state["size"]),
        "fps": state["fps"],
        "seed": state["seed"],
        "fresh": fresh,
        "a": a.reshape(-1, 3).mean(axis=0).tolist(),
        "b": b.reshape(-1, 3).mean(axis=0).tolist(),
        "shapes": [list(a.shape), list(b.shape), str(a.dtype), str(b.dtype)],
    }
    with open(Path(__file__).with_suffix(".log"), "a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\\n")
"""
WIPE = (
    LOGGING
    + """

def render(a, b, t, state):
    note(a, b, t, state)
    edge = round(t * a.shape[1])
    out = a.copy()
    out[:, :edge] = b[:, :edge]
    return out
"""
)
SWITCH = (
    LOGGING
    + """

def render(a, b, t, state):
    note(a, b, t, state)
    return a if t < 0.5 else b
"""
)
GOOD = "def render(a, b, t, state):\n    return b\n"


def calls(module):
    """What the fixture transition logged: one entry per `render` call, in order."""
    text = module.with_suffix(".log").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def transition_plan(tmp_path, transition):
    plan = {
        "version": "1",
        "source": {"path": (tmp_path / "source.mp4").as_posix()},
        "output": {"dir": (tmp_path / "outputs").as_posix(), "reel": {"transition": transition}},
        "clips": [{"id": "only", "takeaway": "One", "segments": [{"start": 0, "end": 1}]}],
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


# --- Contract ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transition",
    [
        {"kind": "module", "module": "assets/transitions/wipe.py"},
        {"kind": "module", "module": "/srv/transitions/wipe.py", "seconds": 1.5},
    ],
)
def test_schema_accepts_a_module_transition_with_a_path(tmp_path, transition):
    plan = load_plan(transition_plan(tmp_path, transition), ROOT)
    assert plan["output"]["reel"]["transition"] == transition


@pytest.mark.parametrize(
    "transition",
    [
        {"kind": "module"},
        {"kind": "module", "seconds": 0.4},
        {"kind": "module", "module": ""},
        {"kind": "module", "module": "transitions/wipe.txt"},
        {"kind": "module", "module": "wipe.py", "seconds": 2},
        {"kind": "module", "module": "wipe.py", "state": {}},
        {"kind": "dissolve", "module": "wipe.py"},
        {"module": "wipe.py"},
    ],
)
def test_schema_requires_the_module_path_exactly_when_the_kind_is_module(tmp_path, transition):
    with pytest.raises(RenderError):
        load_plan(transition_plan(tmp_path, transition), ROOT)


def test_module_style_resolves_the_path_and_is_timed_like_a_dissolve(tmp_path):
    reel = {"transition": {"kind": "module", "module": "transitions/wipe.py", "seconds": 1.5}}
    style = reel_style(reel, lambda value: tmp_path / value)
    assert style == Style("module", Fraction("1.5"), module=tmp_path / "transitions/wipe.py")
    assert reel_style({"transition": {"kind": "dissolve"}}, Path).module is None
    durations = [Fraction(4), Fraction(1), Fraction(5)]
    dissolve = Style("dissolve", Fraction("1.5"))
    assert effective_transition(style, durations) == effective_transition(dissolve, durations)
    assert effective_transition(style, durations) == Fraction(1, 2)

    class Hot:
        def audio_stats(self, path):
            return -0.5, 0

    # Speech cross-fades over a card's music at a module join as at a dissolve, so the
    # clipping guard counts the two together in both.
    bed = Music(Path("bed"))
    lowered = headroom_gain(Hot(), Style("module", music=bed, module=Path("wipe.py")), ["a"])
    assert lowered == headroom_gain(Hot(), Style("dissolve", music=bed), ["a"])
    assert lowered.gain < 1


def test_progress_runs_from_zero_to_one_and_a_single_frame_sits_in_the_middle():
    assert [bridge_progress(index, 4) for index in range(4)] == [0, 1 / 3, 2 / 3, 1]
    assert [bridge_progress(index, 2) for index in range(2)] == [0, 1]
    assert bridge_progress(0, 1) == 0.5


# --- Loading a module ----------------------------------------------------------------------------


def test_missing_module_is_rejected_before_tools_start(tmp_path):
    (tmp_path / "source.mp4").write_bytes(b"never opened")
    transition = {"kind": "module", "module": (tmp_path / "nowhere/wipe.py").as_posix()}
    with pytest.raises(RenderError, match=r"transition module.*nowhere.wipe\.py"):
        render_plan(
            transition_plan(tmp_path, transition),
            root=ROOT,
            ffmpeg="must-not-start",
            ffprobe="must-not-start",
        )
    assert not (tmp_path / "outputs").exists()
    with pytest.raises(RenderError, match="transition module not found"):
        load_transition(tmp_path / "nowhere/wipe.py")


@needs_numpy
@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("SECONDS = 1.0\n", "no callable `render"),
        ("render = 3\n", "no callable `render"),
        ("raise RuntimeError('broken on purpose')\n", "failed to load: RuntimeError: broken on"),
        ("import a_package_nobody_has\n", "--extra styles"),
    ],
)
def test_module_without_a_usable_render_is_rejected_before_tools_start(tmp_path, source, message):
    (tmp_path / "source.mp4").write_bytes(b"never opened")
    module = tmp_path / "broken.py"
    module.write_text(source, encoding="utf-8")
    transition = {"kind": "module", "module": module.as_posix()}
    with pytest.raises(RenderError) as failure:
        render_plan(
            transition_plan(tmp_path, transition),
            root=ROOT,
            ffmpeg="must-not-start",
            ffprobe="must-not-start",
        )
    assert message in str(failure.value) and "broken.py" in str(failure.value)
    assert not (tmp_path / "outputs").exists()


def test_missing_numpy_names_the_extra_that_installs_it(tmp_path, monkeypatch):
    # `None` in `sys.modules` makes `import numpy` raise ImportError, as on a base install.
    monkeypatch.setitem(sys.modules, "numpy", None)
    (tmp_path / "source.mp4").write_bytes(b"never opened")
    module = tmp_path / "fine.py"
    module.write_text(GOOD, encoding="utf-8")
    transition = {"kind": "module", "module": module.as_posix()}
    with pytest.raises(RenderError) as failure:
        render_plan(
            transition_plan(tmp_path, transition),
            root=ROOT,
            ffmpeg="must-not-start",
            ffprobe="must-not-start",
        )
    message = str(failure.value)
    assert "numpy" in message and "fine.py" in message
    assert "uv run --project render --extra styles cliprender" in message
    assert not (tmp_path / "outputs").exists()


# --- One bridge ----------------------------------------------------------------------------------

GREEN, ROSE = (48, 192, 96), (192, 48, 96)


def raw_end(path, rgb, frames):
    """A piece's end as `encode_piece` writes it: raw BT.709 limited-range frames of one colour."""
    colour = "0x" + bytes(rgb).hex()
    source = (
        f"color=c={colour}:s={WIDTH}x{HEIGHT}:r=10,format=rgb24,"
        f"scale=out_color_matrix={CARD_MATRIX}:out_range=tv,format=yuv420p"
    )
    ffmpeg("-f", "lavfi", "-i", source, "-frames:v", frames, "-f", "yuv4mpegpipe", path)


def plane_means(path):
    """(Y, U, V) plane means of every frame, read without any pixel-format conversion."""
    raw = ffmpeg("-i", path, "-map", "0:v:0", "-pix_fmt", "yuv420p", "-f", "rawvideo", "-")
    luma, chroma = WIDTH * HEIGHT, WIDTH * HEIGHT // 4
    assert len(raw) % (luma + 2 * chroma) == 0
    means = []
    for pos in range(0, len(raw), luma + 2 * chroma):
        planes = [(pos, luma), (pos + luma, chroma), (pos + luma + chroma, chroma)]
        means.append([sum(raw[start : start + size]) / size for start, size in planes])
    return means


def bridge(folder, source, frames=4):
    """Encode one bridge from a green tail into a rose head with the transition `source`."""
    module = folder / "transition.py"
    module.write_text(source, encoding="utf-8")
    raw_end(folder / "piece-00-tail.y4m", GREEN, frames)
    raw_end(folder / "piece-01-head.y4m", ROSE, frames)
    tools = Tools(timeout=60)
    encode_module_bridge(
        tools,
        folder,
        "bridge-00",
        "piece-00",
        "piece-01",
        frames,
        Fraction(10),
        (WIDTH, HEIGHT),
        load_transition(module),
        module,
        matrix=CARD_MATRIX,
        tags=COLOR_TAGS,
        encoder_flags=piece_encoder_flags(Fraction(10), tools.cfr_flags()),
    )
    return module, folder / "bridge-00.mp4"


@needs_tools
@needs_numpy
def test_bridge_hands_render_true_colours_and_returns_them_unshifted(tmp_path):
    module, output = bridge(tmp_path, SWITCH)
    seen = calls(module)
    assert [entry["index"] for entry in seen] == [0, 1, 2, 3]
    assert [entry["t"] for entry in seen] == pytest.approx([0, 1 / 3, 2 / 3, 1])
    assert [entry["fresh"] for entry in seen] == [True, False, False, False]
    for entry in seen:
        assert entry["shapes"] == [[HEIGHT, WIDTH, 3], [HEIGHT, WIDTH, 3], "uint8", "uint8"]
        assert (entry["frames"], entry["size"], entry["fps"], entry["seed"]) == (
            4,
            [WIDTH, HEIGHT],
            10,
            7,
        )
        # The raw ends are BT.709 limited range and say so nowhere: `render` must still see
        # the colours they were made from (to the rounding of two 8-bit conversions and 4:2:0),
        # not a BT.601 or full-range reading of them, which is off by ten levels and more.
        assert entry["a"] == pytest.approx(GREEN, abs=4)
        assert entry["b"] == pytest.approx(ROSE, abs=4)
    # And a frame returned unchanged comes back as the pixels it was: the bridge joins the
    # pieces without a colour step. Frames 0-1 are the tail, 2-3 the head.
    video = next(s for s in probe(output)["streams"] if s["codec_type"] == "video")
    assert (video["codec_name"], video["pix_fmt"]) == ("h264", "yuv420p")
    assert (video.get("color_space"), video.get("color_range")) == ("bt709", "tv")
    drawn = plane_means(output)
    tail = plane_means(tmp_path / "piece-00-tail.y4m")[0]
    head = plane_means(tmp_path / "piece-01-head.y4m")[0]
    assert len(drawn) == 4
    for frame, expected in zip(drawn, [tail, tail, head, head]):
        assert frame == pytest.approx(expected, abs=1)


IN_PLACE = """
def render(a, b, t, state):
    if t >= 0.5:
        a[:] = b  # draw into the outgoing frame: the lab hands out writable frames, so must we
    return a
"""


@needs_tools
@needs_numpy
def test_render_may_draw_into_the_frames_it_is_given(tmp_path):
    _, output = bridge(tmp_path, IN_PLACE)
    drawn = plane_means(output)
    tail = plane_means(tmp_path / "piece-00-tail.y4m")[0]
    head = plane_means(tmp_path / "piece-01-head.y4m")[0]
    for frame, expected in zip(drawn, [tail, tail, head, head]):
        assert frame == pytest.approx(expected, abs=1)


@needs_tools
@needs_numpy
def test_single_frame_bridge_is_drawn_at_the_midpoint(tmp_path):
    module, output = bridge(tmp_path, WIPE, frames=1)
    (entry,) = calls(module)
    assert (entry["t"], entry["index"], entry["frames"], entry["fresh"]) == (0.5, 0, 1, True)
    assert len(plane_means(output)) == 1


@needs_tools
@needs_numpy
@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("return a[:, :, 0]", r"returned a uint8 array of shape \(90, 160\) at frame 0"),
        ("return a.astype('float32')", r"returned a float32 array of shape .* at frame 0"),
        ("return None", r"returned NoneType at frame 0"),
        ("return a.tolist()", r"returned list at frame 0"),
        (
            "if state['frame_index'] == 2:\n        raise ValueError('boom')\n    return a",
            r"failed at frame 2 of 4: ValueError: boom",
        ),
    ],
)
def test_bad_frame_or_failure_inside_render_names_the_module_and_the_frame(tmp_path, body, message):
    with pytest.raises(RenderError, match=message) as failure:
        bridge(tmp_path, f"def render(a, b, t, state):\n    {body}\n")
    assert "transition.py" in str(failure.value)


# --- A whole reel --------------------------------------------------------------------------------


def set_transition(plan, transition):
    data = json.loads(plan.read_text(encoding="utf-8"))
    data["output"]["reel"]["transition"] = transition
    plan.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture(scope="module")
def reels(tmp_path_factory, talk):
    """The music reel rendered twice through the CLI: with a 0.4 s dissolve, and with the
    wipe module for the same 0.4 s."""
    made = {}
    for kind in ("dissolve", "module"):
        folder = tmp_path_factory.mktemp(f"reel {kind}")
        plan, output = music_plan(folder, talk, transition="dissolve")
        if kind == "module":
            module = folder / "wipe.py"
            module.write_text(WIPE, encoding="utf-8")
            set_transition(plan, {"kind": "module", "module": module.as_posix(), "seconds": 0.4})
            made["wipe"] = module
        result = cli(plan)
        records = rows(result)
        assert [fields[0] for fields in records] == ["first", "second", "reel"]
        made[kind] = (output / "reel.mp4", float(records[-1][2]))
    return made


def luma_frames(path, timing_flags):
    """Every frame's Y plane as a (frames, height, width) array, without conversion."""
    import numpy as np

    raw = ffmpeg(
        *("-i", path, "-map", "0:v:0", "-an", *timing_flags),
        *("-pix_fmt", "yuv420p", "-f", "rawvideo", "-"),
    )
    frames = np.frombuffer(raw, np.uint8).reshape(-1, HEIGHT * 3 // 2, WIDTH)
    return frames[:, :HEIGHT, :].astype(float)


@needs_tools
@needs_numpy
def test_module_reel_has_the_frames_and_length_of_the_same_dissolve(reels, timing_flags):
    (dissolve, dissolve_seconds), (module, module_seconds) = reels["dissolve"], reels["module"]
    # 31 s of segments less six 0.4 s overlaps, at 10 fps.
    assert len(luma_frames(module, timing_flags)) == 286
    assert len(luma_frames(dissolve, timing_flags)) == 286
    assert module_seconds == dissolve_seconds
    lengths = []
    for path in (dissolve, module):
        streams = {s["codec_type"]: s for s in probe(path)["streams"]}
        lengths.append((streams["video"]["duration"], streams["audio"]["duration"]))
    assert lengths[0] == lengths[1]
    # The audio is the dissolve's cross-fade, sample for sample in length and alike in level.
    ours, theirs = audio(module), audio(dissolve)
    assert len(ours) == len(theirs)
    for start in (1.5, 11.0, 13.0, 15.4, 22.0):
        window = slice(round(start * RATE), round((start + 0.4) * RATE))
        level = (sum(v * v for v in theirs[window]) / (0.4 * RATE)) ** 0.5
        assert (sum(v * v for v in ours[window]) / (0.4 * RATE)) ** 0.5 == pytest.approx(
            level, rel=0.05, abs=0.003
        )


@needs_tools
@needs_numpy
def test_wipe_shows_the_incoming_picture_on_one_side_and_the_outgoing_on_the_other(
    reels, timing_flags
):
    frames = luma_frames(reels["module"][0], timing_flags)
    # Clip A (luma 200) into clip B (luma 100) over frames 154-157: t = 0, 1/3, 2/3, 1, so the
    # edge stands at column 0, 53, 107 and 160. Stay 12 columns clear of it (H.264 ringing).
    assert frames[150].mean() == pytest.approx(200, abs=6)
    assert frames[154].mean() == pytest.approx(200, abs=6)
    assert frames[155][:, :41].mean() == pytest.approx(100, abs=6)
    assert frames[155][:, 65:].mean() == pytest.approx(200, abs=6)
    assert frames[156][:, :95].mean() == pytest.approx(100, abs=6)
    assert frames[156][:, 119:].mean() == pytest.approx(200, abs=6)
    assert frames[157].mean() == pytest.approx(100, abs=6)
    assert frames[160].mean() == pytest.approx(100, abs=6)
    # The same bridge of the dissolve is an even mix instead: no side is either picture.
    mixed = luma_frames(reels["dissolve"][0], timing_flags)[156]
    assert abs(mixed[:, :41].mean() - mixed[:, 119:].mean()) < 3
    # Six joins, four frames each, every join with a state of its own.
    seen = calls(reels["wipe"])
    assert len(seen) == 24
    for join in range(6):
        entries = seen[join * 4 : join * 4 + 4]
        assert [entry["index"] for entry in entries] == [0, 1, 2, 3]
        assert [entry["t"] for entry in entries] == pytest.approx([0, 1 / 3, 2 / 3, 1])
        assert [entry["fresh"] for entry in entries] == [True, False, False, False]
        assert all(entry["size"] == [WIDTH, HEIGHT] and entry["fps"] == 10 for entry in entries)
    # The fourth join is the one above: `a` is clip A, `b` clip B (grey, so R = G = B).
    assert seen[12]["a"] == pytest.approx([215, 215, 215], abs=4)
    assert seen[12]["b"] == pytest.approx([98, 98, 98], abs=4)


@needs_tools
@needs_numpy
def test_failure_inside_render_fails_the_reel_and_publishes_nothing(tmp_path, talk):
    plan, output = music_plan(tmp_path, talk, music=False, transition="dissolve")
    module = tmp_path / "fragile.py"
    module.write_text(
        "def render(a, b, t, state):\n"
        "    if state['frame_index'] == 3:\n"
        "        raise ZeroDivisionError('ran out of ink')\n"
        "    return a\n",
        encoding="utf-8",
    )
    set_transition(plan, {"kind": "module", "module": module.as_posix(), "seconds": 0.4})
    result = cli(plan)
    assert result.returncode == 1
    assert "fragile.py failed at frame 3 of 4: ZeroDivisionError: ran out of ink" in result.stderr
    assert list(output.iterdir()) == []
