# Local video editing framework

The v1 interface is the AI skill plus CLI entry points operating on local files.
Target Python 3.11+ managed with uv and FFmpeg/ffprobe, as recorded in `veb-de2`,
on Pi (ARM Linux), Windows and macOS. No web page or packaged application is
required. Browser-service concepts are optional future work in the appendix.

```text
Local file path (or an attachment already available to the agent)
  -> probe + edit instructions
  -> validated edit plan (contract/)
  -> render/ through CLI + FFmpeg
  -> verified local output files
```

## Local execution

Resolve the input to a readable local file and select a separate output directory.
Probe duration, streams and timing; preserve the original. Convert the user's
keep/remove request into the canonical keep-list described in [timeline.md](timeline.md),
then map it to contract v1 `clips[].segments` in seconds. Validate the saved JSON
plan against the agreed schema and semantic bounds before rendering. Do not add
service IDs or request metadata to the contract.

Show the cut list and expected duration; offer a local preview when available.
Render the saved plan with the repository's entry point, from the repository
root: `uv run --project render cliprender <plan.json> --root .` (add `--overwrite`
to replace earlier outputs). It validates the plan against the checked-in schema,
cuts and joins, verifies every output and prints one `id<TAB>path<TAB>seconds`
row per file; a non-zero exit publishes nothing from that run. Intake for reels,
outlines and summaries is `uv run --project bot clipbot …` (see the
[clipbot skill](../../clipbot/SKILL.md)). Do not fall back to hand-written FFmpeg
commands: that proves a media operation, not the documented CLI.

Keep the source and saved plan unchanged during a render. Record their hashes
alongside execution results when reproducibility is needed; these are execution
metadata, not new contract fields. A changed edit requires a new saved plan or
revision in the caller's records. Report each actual output path, selected ranges,
original/edited duration, format, file size and material warnings. Give a usable
local file link to the result; no hosted download URL is needed.

Use argument arrays for media tools and resolve filesystem paths without shell
interpolation. Quote paths through the chosen CLI's argument handling, support
spaces and Unicode, and never overwrite the source or an existing result without
explicit intent. Isolate temporary files per run, apply resource/time limits and
clean partial output after failure or cancellation. Preserve completed user
outputs; there is no automatic remote retention policy in v1.

## Component boundaries

| Component | Responsibility |
| --- | --- |
| Agent/CLI intake (`bot/`) | Local path and edit intent, cut-list presentation, normalization and validated JSON plan generation. |
| Shared edit-plan contract (`contract/`) | Authoritative v1 source/output specification and `clips[].segments` keep ranges in seconds. |
| Renderer (`render/`) | Probe, accurate cuts and joins, audio sync, verification and local output files. |

The renderer consumes the agreed plan; it does not interpret keep/remove language
or depend on HTTP. Future adapters must reuse this boundary. Existing caption or
transcript capabilities should be used only when available and requested; do not
invent an analysis tool or silently claim it ran.

## Rendering and verification

Use [timeline semantics](timeline.md) as the basis for plan validation and cut
mapping. A proposed compatible export is MP4 with H.264 video, AAC audio when
present, and fast-start metadata. Define a quality-preserving encoding policy
with the chosen runtime; there is no target-byte-size mode in this framework.
Re-encoding may produce a larger file even when duration is shorter.

Probe actual media instead of trusting extensions or MIME types. Test MP4/H.264,
phone MOV/HEVC and WebM/VP8/VP9 against the selected FFmpeg build. Pin compatible
FFmpeg tooling through `veb-uv0` before relying on version-specific options.
Handle unsupported codecs, corrupt files, video-less inputs and unsupported
HDR/stream layouts explicitly. See [ffprobe documentation](https://ffmpeg.org/ffprobe.html)
and [MP4 fast-start behavior](https://ffmpeg.org/ffmpeg-formats.html#mov_002c-mp4_002c-ismv).

Before publication, require successful rendering, a nonempty full-decodable
output, intended video/audio streams, expected orientation/dimensions, and
duration consistent with the kept ranges under the documented frame/packet
tolerance. Verification must fail if the tolerance would hide a missing segment.
Use generated timestamped video/audio fixtures to prove excluded sections are
absent and joins preserve sync; duration alone cannot establish correct editing.
Publish atomically only after verification. Preserve the original during editing
and export; never replace it with the clipped output.

## Optional future web adapter (out of scope for v1)

The following browser, job-service and retention proposals are not dependencies
of the local skill/CLI. They are illustrative rather than implemented APIs. Any
future web adapter would add private storage, authorization and bounded jobs
around the same local plan/renderer boundary.

### Device adapter

Use a labeled native file picker for existing videos, with `accept` hints for
supported video types and extensions. Browsing must work without drag-and-drop,
camera capture or precise mouse interaction. Support touch handles and editable
start/end fields with keyboard access. Offer text-based keep/remove instructions
alongside a visual timeline. Never depend solely on color to identify cut ranges.

The worker decodes the upload; a browser unable to preview HEVC must still be
able to upload bytes the worker supports. Generate a browser-playable proxy when
needed for preview. A proxy must retain a documented mapping to the original
timeline; save cuts in original source time and render from the source, not the
lower-quality proxy. See the [HTML file-input specification](https://html.spec.whatwg.org/multipage/input.html#file-upload-state-(type=file)).

Show upload progress, probe/preview preparation, editing, queued/rendering,
verification and terminal results as distinct states. Offer cancel, readable
errors and a normal download link. Native sharing is an optional enhancement.
If a mobile browser suspends an upload or the connection drops, explain that the
initial adapter restarts that upload; resumable/background upload is not promised.
Persist the scoped source/plan/job IDs so a refresh after upload can recover work.

### Proposed service operations

| Operation | Required behavior |
| --- | --- |
| Upload source | Stream one video to private storage, enforce byte limits while receiving, then probe; return opaque source ID, status and measured metadata. |
| Create/update edit | Validate intent against the immutable source; return a versioned canonical plan, kept ranges, expected duration or `no_changes`/`empty_edit`. |
| Preview edit | Show source-based cut list or playable preview for an exact plan revision; label pending or approximate preview status. |
| Export edit | Accept plan ID, revision and client request ID; queue one render of that immutable revision. |
| Read job | Return phase, progress when available, or a terminal result; unknown progress must not masquerade as a percentage. |
| Cancel job | Idempotently stop queued/active work, or return an already-completed terminal state. |
| Download output | Authorize access to a verified artifact; never expose partial files. |

Repeated export submissions with the same request ID and payload return the same
job within the session's retention window. Reusing that ID with a different
payload is a conflict. Reject expired/incomplete sources and stale or unknown
plan revisions before queueing. A new edit cannot mutate an in-flight export.
Cancel racing with completion returns the actual terminal state; a cancelled job
cannot later publish output.

Illustrative export request:

```json
{
  "version": 1,
  "plan_id": "plan-123",
  "revision": 1,
  "request_id": "export-123"
}
```

Illustrative verified result for the 60-to-40-second edit in the timeline guide:

```json
{
  "version": 1,
  "job_id": "job-123",
  "plan_id": "plan-123",
  "revision": 1,
  "status": "succeeded",
  "source_duration_ms": 60000,
  "output_duration_ms": 40000,
  "output_bytes": 16000000,
  "output": {
    "content_type": "video/mp4",
    "download_path": "/jobs/job-123/output",
    "expires_at": "2026-10-01T18:00:00Z"
  },
  "warnings": []
}
```

Resolve the example download path against the service origin. All result values
must be measured or supplied by the actual service; the sample is not a real
artifact. Include warnings for material boundary, codec or display adjustments.
Jobs transition `queued -> rendering -> verifying -> succeeded`, or terminate
as `failed`/`cancelled` with no downloadable partial output. Validation responses
distinguish `invalid_ranges`, `empty_edit`, `unsupported_media` and `no_changes`;
runtime failures distinguish `source_expired`, `resource_limit` and `render_failed`.
Give a useful next action instead of exposing raw process logs.

### Limits, access and lifecycle

Configure maximum upload bytes, source duration, pixel count, frame rate, stream
count, number of edit ranges, queued jobs, concurrent workers, temporary disk
budget, phase timeouts and retention duration. Show effective upload limits to
the user and enforce them on streamed bytes even without truthful Content-Length.
Bound preview/probe work as well as exports; reject over-capacity work cleanly.

Use argument arrays and server-generated absolute file paths for media tools,
without shell interpolation. A user filename is display metadata. Isolate each
job's private working directory, restrict media tools to required local protocols
and disable network access. Uploaded playlists/containers must not read unrelated
host files or fetch URLs. Do not accept arbitrary flags, paths or URLs as edits.

Authorize source, plan, preview, status, cancel and output operations within the
same user/session. Opaque IDs alone are not access control. Keep storage private
and use scoped, expiring download access. Avoid logging video contents or secrets.
Delete incomplete uploads and temporary artifacts on cancellation/failure; recover
abandoned work after crashes. Keep originals, proxies and outputs only for the
configured retention period and show expiration/re-upload behavior clearly.
