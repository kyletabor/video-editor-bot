# Acceptance scenarios

This is a validation plan for the local skill/CLI implementation. Record tested
operating systems, exact CLI commands, FFmpeg build, canonical plans and measured
results. A one-off FFmpeg edit does not prove the planned CLI exists. Use generated
fixtures or approved samples; keep shared `assets/` read-only outside its assigned task.

## Editing behavior

Generate a source with visible time labels and distinct audio markers at known
times. Check retained content and cut boundaries, not just total file duration.

| Scenario | Expected result |
| --- | --- |
| Keep 10-25 seconds of a 60-second source | Approximately 15-second output containing only that interval under the documented frame-boundary tolerance. |
| Remove 10-20 and 40-50 seconds | Keep 0-10, 20-40 and 50-60 in source order; approximately 40 seconds with corresponding audio joined in sync. |
| Overlapping/adjacent keep or remove ranges | Normalize to a union; no duplicate frames or repeated content at boundaries. |
| Two removals after the first edit shifts displayed time | Both refer to the original source timeline; later boundaries do not drift. |
| Reversed, out-of-bounds, empty ranges or non-integer millisecond adapter values | Reject before rendering; do not silently clamp or reinterpret. Contract segment seconds may be fractional. |
| Full keep / empty removal / removal of everything | `no_changes` / `no_changes` / `empty_edit`; no misleading success or zero-length export. |
| Cut between keyframes or select less than one frame | Accurate re-encoded boundary; reject a range with no decodable frame instead of retaining unwanted surrounding content. |
| "Cut 10 to 20" with no other context | Clarify keep versus remove before constructing the edit. |
| "Remove pauses" without timestamped media analysis | Obtain analysis or explicit ranges; never invent cut points. |
| Edit changed after preview or while rendering | Export references one immutable plan revision; result shows that revision and cannot silently use newer ranges. |

## Media coverage

| Scenario | Expected result |
| --- | --- |
| Portrait phone MOV/HEVC, landscape MP4, WebM | Supported local inputs render even without a local preview player; exports retain correct orientation/aspect ratio. |
| Proxy preview with different encoding/frame rate | Selections still map to original source times; render uses original media. |
| Variable frame rate, delayed audio, nonzero starting timestamps | Correct cut content, valid monotonic output timestamps and preserved audio/video timing across joins. |
| Silent, odd-dimension or low-resolution video | No invented audio, unintended cropping, stretching or upscaling. |
| Unsupported HDR, multitrack, corrupt or audio-only file | Actionable unsupported/invalid-media response without partial output or silent stream loss. |
| Shorter export has more bytes than input | Report actual duration and size; do not degrade quality to satisfy an unrequested compression goal. |

## Local CLI acceptance on Pi, Windows and macOS

Run the documented CLI against `assets/demo-clip.mp4` on Pi (ARM Linux), Windows
and macOS. Keep the source read-only and export to a separate local directory.
Verify contract-valid plans and expected clip content, duration, orientation,
audio synchronization and output paths on each platform. Report actual results
per platform; do not infer success on Pi/macOS from a Windows run. No browser or
upload service is needed for v1 acceptance.

Test local paths containing spaces and Unicode, missing/unreadable inputs,
invalid plans, existing output paths, cancellation, process timeout and disk
exhaustion. Failures must preserve the source and completed results and remove
only this run's partial artifacts. An explicit re-run must use the intended saved
plan. Record output and source hashes where reproducibility is being checked.

Run the shared check gate (`uv run --locked scripts/check.py`, documented in
[`docs/checks.md`](../../../../docs/checks.md)) plus relevant integration scenarios
for an implementation PR, and report the exact checks. For a framework-only PR,
validate skill metadata, reference links, JSON examples and timeline examples; do
not claim these media or cross-platform CLI scenarios passed.

## Optional future web adapter (out of scope for v1)

These checks apply only if the optional adapter in the framework is implemented.

Test upload, range selection, preview, export and download on iOS Safari,
Android Chrome, desktop Safari, Windows Edge/Chrome and Firefox. Include a tablet
viewport and keyboard-only operation. Record actual device coverage separately
from viewport emulation. A single desktop test does not prove device independence.

### Transfer, job and storage behavior

Test interrupted upload/backgrounded mobile tabs, refresh after upload, duplicate
export requests, stale plan revisions, cancellation in each phase, worker timeout,
full queue, disk exhaustion and worker restart. Expected outcomes are recoverable
status, no duplicate exports, no partial downloads and isolated cleanup. Completed
sources/plans remain recoverable only within the documented retention window.

Verify upload limits while streaming without a trustworthy Content-Length;
validate media limits after probing. Filenames with spaces, Unicode and shell/path
characters remain metadata. Uploaded network references cannot access external
URLs or unrelated local files. Another session cannot view, export, cancel or
download a guessed source, plan or job ID. Expired artifacts produce a clear
re-upload action instead of a broken or unauthorized download.
