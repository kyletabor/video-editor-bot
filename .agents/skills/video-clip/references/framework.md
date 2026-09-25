# Device-agnostic upload and editing framework

This is a proposed implementation boundary, independent of language or hosting
provider. Operations and payloads below are illustrative, not live endpoints.

```text
Phone / tablet / desktop
  -> upload -> probe -> edit instructions or timeline selection
  -> canonical keep ranges -> preview -> render selected plan revision
  -> verify edited content -> download
```

## Device adapter

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

## Component boundaries

| Component | Responsibility |
| --- | --- |
| Intake/UI adapter (`bot/`) | Native upload, edit intent, range controls, cut-list/preview presentation, job status and download UI. |
| Shared edit-plan contract (`contract/`) | Source references, normalized ranges, versioning and result/error semantics agreed by both sides. |
| Renderer (`render/`) | Probe, compatible preview generation, accurate cuts and joins, audio sync, output verification. |
| Service infrastructure | Private storage, authorization, bounded job execution, cancellation and retention; assign its location during implementation. |

Keep these responsibilities separable even if one process initially hosts them.
The renderer accepts a staged source plus canonical plan and returns a verified
artifact; it does not depend on HTTP or the requesting device. A future chat
attachment adapter can use the same plan and renderer as the browser.

## Proposed service operations

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

## Limits, access and lifecycle

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
