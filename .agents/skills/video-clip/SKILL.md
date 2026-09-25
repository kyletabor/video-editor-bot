---
name: video-clip
description: Design or implement device-agnostic video upload, editing and clipping. Use when a user wants to keep highlights, remove sections, trim a recording or join selected parts into a shorter video from a phone, tablet or desktop.
---

# Video editing and clipping

Build the workflow: upload one video, identify sections to keep or remove,
preview the edit, and export a downloadable video containing the selected parts.
The goal is a shorter edit. File-size compression is a separate capability.

This skill supplies an implementation framework, not a running upload service,
UI or renderer. When asked to edit an actual file, first identify an available
implementation and its capabilities. Do not present the proposed interfaces as
live tools or claim an export exists without a verified output artifact.

## Repository integration

Read `AGENTS.md` and current Beads context before implementation. The runtime is
awaiting the decision in `veb-de2`; do not choose it implicitly by adding a web
framework or package manifest. Keep this framework language-neutral.

Kyle owns intake and user interaction in `bot/`; Ramsey owns rendering in
`render/`. The shared edit-plan schema belongs in `contract/` under `veb-p12`
and requires opposite-side review. The payloads here are proposals to map into
that contract, not a second authoritative schema. Assign cross-lane work through
Beads instead of editing the other lane. Store handoffs and remaining work in
Beads, not in skill reference files.

## Workflow

1. Accept one uploaded video from the device's native file picker or an existing
   attachment adapter. Preserve the original and probe the stored bytes. Do not
   require the browser to decode the source before it can upload it.
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
   original upload, not a timeline shifted by earlier removals. Reject invalid
   edits before queuing a render.
5. Use [the workflow framework](references/framework.md) to connect upload,
   preview, job processing and download. Offer a preview; an explicit request
   to export an unambiguous edit already authorizes rendering. Ensure an export
   uses the exact plan revision the user selected, including any later changes.
6. Render accurate cuts, join kept segments in source order and retain their
   synchronized audio. Verify the complete output before publishing it. Report
   the ranges used, original/edited duration, output format and actual file size.
   A shorter duration need not produce fewer bytes; never change resolution or
   degrade quality merely to force a smaller byte count.
7. Use [the acceptance scenarios](references/acceptance.md) to validate the actual
   implementation. Record tested devices, browser versions and FFmpeg build;
   framework validation alone does not establish device compatibility.

## Initial scope

One input video, one output video, source-order cuts and straight joins. Preserve
the selected content's orientation, display aspect ratio, frame timing and audio.
Handle silent input without adding audio. Captions, transitions, rearrangement,
multi-file composition and automatic highlight detection require explicit
extensions; do not silently approximate those requests with unsupported cuts.
