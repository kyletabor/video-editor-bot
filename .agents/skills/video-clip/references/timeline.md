# Timeline and edit-plan rules

Map to the contract v1 renderer interface proposed in
[PR #7](https://github.com/kyletabor/video-editor-bot/pull/7), using its
[`contract/edit-plan.schema.json`](https://github.com/kyletabor/video-editor-bot/blob/be000dfaed7a5e6255ce7cabf0370fb19060b970/contract/edit-plan.schema.json).
Once merged, use the repository's authoritative contract when implementing.
Its `clips[].segments` are keep ranges with numeric `start` and `end` values in
seconds. The integer-millisecond keep/remove model below is an adapter's
normalization model, not a second renderer schema or an application runtime.

## Source time

For user-input normalization, use integer milliseconds relative to the original
video's normalized playback timeline. Range boundaries are half-open:
`[start_ms, end_ms)`. A range includes
its start and excludes its end, so adjacent ranges do not duplicate a boundary.
Normalize the media's common presentation-time origin once, retaining relative
audio/video offsets. Do not reset each input stream independently before clipping.

Let `duration_ms` be the probed playable duration on that same timeline. Each
range must satisfy `0 <= start_ms < end_ms <= duration_ms`. Reject non-integers,
non-finite values, reversed or zero-length ranges and values outside the source.
Do not silently clamp them. Parse user-facing times such as `01:23.450` into
milliseconds explicitly; do not interpret them as frame numbers.

Cuts resolve to the decoded frames/audio samples available at the requested
boundary. Use a documented boundary policy, such as retaining frames with source
presentation timestamps in the half-open interval. Reject a selection containing
no decodable video frame. Report material frame-boundary adjustments and validate
duration with a tolerance derived from the actual boundary frames and audio
packets, rather than promising millisecond-exact video cuts.

## Edit modes and normalization

| Mode | Meaning | Normalized renderer input |
| --- | --- | --- |
| `keep` | Retain exactly the listed source ranges; a single range is a trim. | Validated ranges, sorted by source start and unioned. |
| `remove` | Cut out the listed source ranges. | Complement of their union within `[0, duration_ms)`. |

Validate first, then sort and merge overlapping or adjacent ranges. The merged
union represents content selection, never repeated playback. This normalization
preserves source order; rearrangement or repetition requires a separate adapter
capability, as distinguished from contract support below.

An empty `keep` list is invalid. An empty `remove` list or a full-duration keep
is `no_changes`; return that outcome without claiming an edit or launching an
unnecessary render. Removing the entire video is `empty_edit`, not a successful
zero-length export. An unchanged original may be offered clearly labeled as such.

The renderer receives only canonical, nonempty keep ranges through contract v1;
it does not receive a `remove` mode or compute complements. Expected selected
duration is the sum of `end_ms - start_ms` across those ranges.

## Mapping to contract v1

After normalization, emit one clip for the joined selection and convert each
boundary once: `start = start_ms / 1000` and `end = end_ms / 1000`. These are
floating-point seconds represented as JSON numbers; preserve fractional values
without rounding to whole seconds. Contract input itself need not have integer
millisecond precision. Both forms use the same half-open source timeline.

| Adapter value | Contract v1 field |
| --- | --- |
| Stored, probed source path | `source.path` (forward slashes) |
| Probed duration in seconds | `source.duration_seconds` |
| Canonical kept ranges, converted to seconds | `clips[0].segments[]` with `start` and `end` |
| Output destination and clip identity | `output.dir` and `clips[0].id` |
| One-sentence description of the selected content | `clips[0].takeaway` |

Use the string `"1"` for `version`, not the number `1`. Validate the resulting
plan against the authoritative schema and perform its semantic checks: unique
clip IDs, `end > start`, and ends within the source duration. Always probe and
validate against the actual source; omitting the optional duration field does
not permit out-of-bounds edits. Reject non-finite numbers and unknown fields.

For exact user-selected intervals, explicitly set `clips[0].trim_silence` to
`false`; contract v1 otherwise defaults it to `true` and permits shaving up to
0.5 seconds of silence at segment edges. Only enable that behavior when it is
part of the requested edit, and account for it in the reported duration.

Keep source IDs, fingerprints, plan IDs/revisions and calculated durations in
adapter/job metadata; they are not additional contract v1 fields. That metadata
binds the immutable stored source, probed duration and canonical plan to the
preview/export revision. Changing any of these creates a new revision. Handle
`no_changes` and `empty_edit` before dispatch instead of sending empty segment
lists or invented outcome fields to the renderer.

## Worked examples

For a 60-second source:

| Request | Canonical keep ranges (milliseconds) | Expected duration |
| --- | --- | --- |
| Keep 00:10 through 00:25 | `[10000, 25000)` | 15 seconds |
| Remove 00:10 through 00:20 and 00:40 through 00:50 | `[0, 10000)`, `[20000, 40000)`, `[50000, 60000)` | 40 seconds |
| Keep 00:05-00:15 and 00:10-00:25 | `[5000, 25000)` | 20 seconds; overlap is retained once |
| Remove 00:00-00:05 and 00:55-01:00 | `[5000, 55000)` | 50 seconds |
| Keep unordered selections 00:20-00:30 and 00:00-00:10 | `[0, 10000)`, `[20000, 30000)` | 20 seconds in source order |

These keep/remove operations preserve source order. Contract v1 itself also
permits segments out of source order and requires rendering them in array order.
An explicit rearrangement request must use an adapter capability that honors
that order or be reported as unsupported; do not silently turn it into a
different source-order edit. The renderer must not sort or union an already
validated contract plan.

Schema-valid contract v1 example for removing `[10000, 20000)` and
`[40000, 50000)` from a hypothetical 60-second source:

```json
{
  "version": "1",
  "source": {
    "path": "inputs/source-123.mp4",
    "duration_seconds": 60.0,
    "captions": {"kind": "none"}
  },
  "output": {
    "dir": "out/selection",
    "preset": "internal",
    "aspect": "16:9",
    "captions": "none"
  },
  "clips": [
    {
      "id": "selected-parts",
      "takeaway": "Selected parts of the source with two intervals removed.",
      "segments": [
        {"start": 0.0, "end": 10.0},
        {"start": 20.0, "end": 40.0},
        {"start": 50.0, "end": 60.0}
      ],
      "trim_silence": false
    }
  ]
}
```

The example paths and source facts are illustrative; probe the stored file and
derive the real metadata instead of trusting client-supplied facts. The expected
selected duration is 40 seconds, calculated outside the contract payload. A
boundary of `12345` milliseconds maps to `12.345` seconds, without changing the
source origin. Reject unsupported adapter modes instead of ignoring intent.

## Rendering invariants

Decode and re-encode for accurate arbitrary cuts. Keyframe-based stream copy is
not the default: its seek boundaries can retain unwanted frames. A future fast
mode must disclose approximate boundaries and be explicitly selected.

For each canonical range, trim video and corresponding audio on the common
source timeline. Shift both by the same cut-start offset onto the segment
timeline, preserving their relative timing; handle leading/trailing audio gaps
explicitly without shifting speech. Join the segments with compatible stream
parameters and monotonic timestamps. A silent source uses a video-only path.
The eventual implementation may use FFmpeg's trim/atrim, timestamp adjustment and
concat filters; verify the installed build's behavior. Trim filters retain
timestamps; concat expects zero-based segments and may pad shorter audio to the
longest stream. Account explicitly for boundary rounding and padding so repeated
joins do not introduce gaps or accumulate sync drift.

Keep output orientation and aspect ratio, avoid upscaling, and make only the
documented padding/pixel-format adjustments required by the export codec.
Probe rotation and HDR metadata before encoding. Apply autorotation once; HDR
requires a tested preservation or tone-mapping path, otherwise return a clear
unsupported-media result. Preserve the selected video and audio streams; reject
unsupported multitrack layouts rather than discarding tracks silently.

Useful implementation references:

- [FFmpeg seeking and stream-copy behavior](https://ffmpeg.org/ffmpeg.html#Main-options)
- [Video trim filter](https://ffmpeg.org/ffmpeg-filters.html#trim)
- [Audio trim filter](https://ffmpeg.org/ffmpeg-filters.html#atrim)
- [Timestamp adjustment](https://ffmpeg.org/ffmpeg-filters.html#setpts_002c-asetpts)
- [Concat filter requirements](https://ffmpeg.org/ffmpeg-filters.html#concat)
