"""Caption timing behavior at cuts, joins, and repeated source intervals."""

import math

import pytest

from cliprender.captions import Cue, format_srt, parse_srt, retime


def test_parse_bom_crlf_multiline_and_nonsequential_indices():
    source = (
        "\ufeff7\r\n00:00:01,250 --> 00:00:02,500\r\nFirst line\r\n<i>Second line</i>\r\n"
        "\r\n3\r\n01:02:03,004 --> 01:02:04,005\r\nLater\r\n"
    )
    assert parse_srt(source) == [
        Cue(1.25, 2.5, "First line\n<i>Second line</i>"),
        Cue(3723.004, 3724.005, "Later"),
    ]


@pytest.mark.parametrize("source", ["", "\ufeff", " \r\n\t\n"])
def test_parse_empty(source):
    assert parse_srt(source) == []


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("1\n00:00:00,000 --> 00:00:01,000", "numeric index, timing line, and subtitle text"),
        ("index\n00:00:00,000 --> 00:00:01,000\nHello", "numeric index"),
        ("1\n00:60:00,000 --> 01:00:01,000\nHello", "below 60"),
        ("1\n00:00:60,000 --> 01:00:01,000\nHello", "below 60"),
        ("1\n00:00:02,000 --> 00:00:01,000\nHello", "start < end"),
        ("1\n00:00:01,000 --> 00:00:01,000\nHello", "start < end"),
        ("1\nNaN --> Infinity\nHello", "finite timestamps"),
        ("1\n-00:00:01,000 --> 00:00:01,000\nHello", "finite timestamps"),
        ("1\n00:00:01,000 --> 00:00:02,000 junk\nHello", "finite timestamps"),
        ("1\n00:00:01.000 --> 00:00:02.000\nHello", "finite timestamps"),
    ],
)
def test_parse_rejects_malformed_block_with_location(source, message):
    with pytest.raises(ValueError, match=message) as error:
        parse_srt(source)
    assert "SRT block 1:" in str(error.value)


def test_parse_rejects_extremely_large_nonfinite_time():
    with pytest.raises(ValueError, match="finite"):
        parse_srt(f"1\n{'9' * 400}:00:00,000 --> {'9' * 401}:00:00,000\nText")


def test_parse_continues_a_cue_across_blank_lines_inside_its_text():
    # Zoom-style embedded captions separate speakers with blank lines inside one cue; the
    # extracted SRT therefore has index-less blocks that belong to the cue before them.
    source = (
        "1\n00:00:08,000 --> 00:00:12,000\n(Kyle Tabor)\ncan start.\n-\n\n"
        "(Ramsey Jamoul)\nOh,\n-\n\n()\nAnd Jim joined\n\n"
        "2\n00:00:12,000 --> 00:00:16,000\nlate.\n"
    )
    cues = parse_srt(source)
    assert cues == [
        Cue(8, 12, "(Kyle Tabor)\ncan start.\n-\n(Ramsey Jamoul)\nOh,\n-\n()\nAnd Jim joined"),
        Cue(12, 16, "late."),
    ]
    assert parse_srt(format_srt(cues)) == cues
    # A continuation whose first line is a number is still not mistaken for a new cue.
    assert parse_srt("1\n00:00:01,000 --> 00:00:02,000\nA\n\n2 people\njoined\n") == [
        Cue(1, 2, "A\n2 people\njoined")
    ]


def test_format_roundtrips_multiline_and_over_hour_times():
    cues = [Cue(1.125, 2.25, "Hello\nworld"), Cue(360000.001, 360001.999, "Long video")]
    result = format_srt(cues)
    assert result == (
        "1\n00:00:01,125 --> 00:00:02,250\nHello\nworld\n\n"
        "2\n100:00:00,001 --> 100:00:01,999\nLong video\n"
    )
    assert parse_srt(result) == cues


def test_format_empty_and_unrepresentable_fragment():
    assert format_srt([]) == ""
    assert format_srt([Cue(1.0001, 1.0002, "short"), Cue(2, 3, "visible")]) == (
        "1\n00:00:02,000 --> 00:00:03,000\nvisible\n"
    )


@pytest.mark.parametrize(
    "cue",
    [
        Cue(math.nan, 2, "x"),
        Cue(0, math.inf, "x"),
        Cue(-1, 2, "x"),
        Cue(2, 1, "x"),
        Cue(1, 1, "x"),
        Cue(0, 1, ""),
        Cue(True, 2, "x"),
    ],
)
def test_helpers_reject_invalid_cues(cue):
    with pytest.raises(ValueError, match="Cue 1:"):
        format_srt([cue])
    with pytest.raises(ValueError, match="Cue 1:"):
        retime([cue], [{"start": 0, "end": 3}])


def test_retime_intersects_excludes_boundaries_and_removed_cues():
    cues = [
        Cue(0, 2, "before"),
        Cue(1, 3, "left cut"),
        Cue(3, 4, "inside"),
        Cue(4, 6, "right cut"),
        Cue(5, 6, "after"),
    ]
    assert retime(cues, [{"start": 2, "end": 5}]) == [
        Cue(0, 1, "left cut"),
        Cue(1, 2, "inside"),
        Cue(2, 3, "right cut"),
    ]


def test_retime_reordered_and_repeated_segments_follow_array_order():
    cues = [Cue(1, 3, "first"), Cue(10, 12, "payoff")]
    segments = [{"start": 10, "end": 12}, {"start": 1, "end": 2}, {"start": 10, "end": 11}]
    assert retime(cues, segments) == [Cue(0, 2, "payoff"), Cue(2, 3, "first"), Cue(3, 4, "payoff")]


def test_retime_spanning_cue_is_split_at_every_join():
    cues = [Cue(1, 15, "spanning"), Cue(5, 9, "deleted")]
    assert retime(cues, [{"start": 2, "end": 4}, {"start": 10, "end": 13}]) == [
        Cue(0, 2, "spanning"),
        Cue(2, 5, "spanning"),
    ]


def test_retime_orders_overlapping_cues_by_timestamp_stably():
    cues = [Cue(4, 7, "last"), Cue(1, 5, "first"), Cue(1, 3, "second")]
    assert retime(cues, [{"start": 2, "end": 6}]) == [
        Cue(0, 3, "first"),
        Cue(0, 1, "second"),
        Cue(2, 4, "last"),
    ]


def test_retime_fractional_bounds_remain_inside_joined_duration():
    output = retime(
        [Cue(0, 20, "all")], [{"start": 1.25, "end": 2.375}, {"start": 8.5, "end": 9.125}]
    )
    assert output == [Cue(0, 1.125, "all"), Cue(1.125, 1.75, "all")]


@pytest.mark.parametrize(
    "segment",
    [
        {"start": 1},
        {"end": 1},
        {"start": 1, "end": 0},
        {"start": 1, "end": 1},
        {"start": -1, "end": 1},
        {"start": math.nan, "end": 1},
        {"start": 0, "end": math.inf},
    ],
)
def test_retime_validates_segments_even_when_no_cues(segment):
    with pytest.raises(ValueError, match="Segment 1:"):
        retime([], [segment])


def test_retime_empty_inputs():
    assert retime([], [{"start": 0, "end": 1}]) == []
    assert retime([Cue(0, 1, "x")], []) == []
