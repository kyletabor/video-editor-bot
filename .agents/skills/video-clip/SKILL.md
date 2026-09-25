---
name: video-clip
description: Edit and clip local videos through an AI skill and CLI across Windows, macOS and ARM Linux. Use when a user wants to keep highlights, remove sections, trim a recording or join selected parts into a shorter video.
---

# Video editing and clipping

Use the workflow: accept one local video, identify sections to keep or remove,
preview the edit, and export local video files containing the selected parts.
The goal is a shorter edit. File-size compression is a separate capability.

This skill supplies an implementation framework for local execution. The AI is
the interface; v1 has no web page or packaged application. When asked to edit an
actual file, first identify an available implementation and its capabilities.
Do not present the proposed interfaces as
live tools or claim an export exists without a verified output artifact.

## Repository integration

Read `AGENTS.md` and current Beads context before implementation. Target the
Python 3.11+ / uv and FFmpeg/ffprobe stack recorded in `veb-de2`, with CLI entry
points and this repo skill on Pi (ARM Linux), Windows and macOS. Check that bead's
current decision status before changing runtime configuration.

Kyle owns intake and user interaction in `bot/`; Ramsey owns rendering in
`render/`. The shared edit-plan schema belongs in `contract/` under `veb-p12`
and requires opposite-side review. Normalize keep/remove requests into contract
v1 `clips[].segments` in seconds as described in the timeline reference; do not
create a second authoritative schema. Assign cross-lane work through
Beads instead of editing the other lane. Store handoffs and remaining work in
Beads, not in skill reference files.

## Workflow

1. Accept a local video path (or an attachment the agent already has). Resolve
   the attachment to a readable local file, preserve the original and probe the
   actual bytes with ffprobe. A browser upload service is not required.
2. Translate the user's edit into explicit source-time ranges. Support trimming
   to one range, keeping several ranges, and removing unwanted ranges. Resolve
   ambiguous instructions such as "cut 10 to 20" by asking whether to keep or
   remove that part. Reuse explicit intent without asking again.
3. For requests such as "remove the pauses" or "keep the best answer", inspect
   the media or timestamped analysis available to the implementation. A transcript
   can locate speech but cannot prove what is visible. Offer candidate cuts
   grounded in that evidence; ask for missing criteria or timestamps rather than
   inventing segments. Automatic content analysis is an optional adapter.
4. Normalize ranges using [the timeline rules](references/timeline.md). Show a
   compact cut list and expected edited duration. All timestamps refer to the
   original source, not a timeline shifted by earlier removals. Reject invalid
   edits before invoking the renderer.
5. Use [the workflow framework](references/framework.md) to connect local intake,
   preview, CLI rendering and output files. Offer a preview; an explicit request
   to export an unambiguous edit already authorizes rendering. Ensure an export
   uses the exact saved plan the user selected, including any later changes.
6. Render accurate cuts, join kept segments in source order and retain their
   synchronized audio. Verify the complete output before publishing it. Report
   the ranges used, original/edited duration, output format and actual file size.
   A shorter duration need not produce fewer bytes; never change resolution or
   degrade quality merely to force a smaller byte count.
7. Use [the acceptance scenarios](references/acceptance.md) to validate the actual
   implementation. Record tested operating systems, CLI commands and FFmpeg
   build; one Windows render does not establish Pi or macOS compatibility.

## Initial scope

One input video, one output video, source-order cuts and straight joins. Preserve
the selected content's orientation, display aspect ratio, frame timing and audio.
Handle silent input without adding audio. Captions, transitions, rearrangement,
multi-file composition and automatic highlight detection require explicit
extensions; do not silently approximate those requests with unsupported cuts.
