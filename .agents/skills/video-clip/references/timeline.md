# Timeline and edit-plan rules

These rules describe the clipping capability to map into the agreed edit-plan
contract. The examples are illustrative; this reference does not replace the
schema in `contract/` or select an application runtime.

## Source time

Use integer milliseconds relative to the original video's normalized playback
timeline. Range boundaries are half-open: `[start_ms, end_ms)`. A range includes
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
union represents content selection, never repeated playback. Preserve source
order; a request to rearrange or repeat segments needs a future extension.

An empty `keep` list is invalid. An empty `remove` list or a full-duration keep
is `no_changes`; return that outcome without claiming an edit or launching an
unnecessary render. Removing the entire video is `empty_edit`, not a successful
zero-length export. An unchanged original may be offered clearly labeled as such.

The renderer receives only canonical, nonempty keep ranges. Expected duration is
the sum of `end_ms - start_ms` across them. A versioned edit binds source ID,
source fingerprint, probed duration and canonical ranges; changing any of these
creates a new plan revision. The source must remain immutable for that revision.

## Worked examples

For a 60-second source:

| Request | Canonical keep ranges (milliseconds) | Expected duration |
| --- | --- | --- |
| Keep 00:10 through 00:25 | `[10000, 25000)` | 15 seconds |
| Remove 00:10 through 00:20 and 00:40 through 00:50 | `[0, 10000)`, `[20000, 40000)`, `[50000, 60000)` | 40 seconds |
| Keep 00:05-00:15 and 00:10-00:25 | `[5000, 25000)` | 20 seconds; overlap is retained once |
| Remove 00:00-00:05 and 00:55-01:00 | `[5000, 55000)` | 50 seconds |
| Keep unordered selections 00:20-00:30 and 00:00-00:10 | `[0, 10000)`, `[20000, 30000)` | 20 seconds in source order |

An explicit request to play a later segment before an earlier one is unsupported
in v1; do not silently sort that requested playback order into a different edit.

Illustrative request to the future edit adapter:

```json
{
  "version": 1,
  "source_id": "source-123",
  "mode": "remove",
  "ranges": [
    {"start_ms": 10000, "end_ms": 20000},
    {"start_ms": 40000, "end_ms": 50000}
  ]
}
```

Illustrative canonical plan returned after probing and validation:

```json
{
  "version": 1,
  "plan_id": "plan-123",
  "revision": 1,
  "source_id": "source-123",
  "source_fingerprint": "sha256:4f1c2a9b0d6e8f735ab2c901e4d6f8079a3b5c7d1e2f4068ab9c0d1e2f3a4567",
  "source_duration_ms": 60000,
  "keep_ranges": [
    {"start_ms": 0, "end_ms": 10000},
    {"start_ms": 20000, "end_ms": 40000},
    {"start_ms": 50000, "end_ms": 60000}
  ],
  "expected_duration_ms": 40000
}
```

These identifiers and fingerprint are example values. The service computes the
real fingerprint and duration; it never trusts client-supplied media facts.
Reject unsupported versions, modes and fields instead of ignoring intent.

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
