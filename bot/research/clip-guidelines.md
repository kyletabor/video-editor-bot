# Clip guidelines — what "digestible" means

Bead: `veb-t2b.5`. Owner: Kyle's lane (`bot/`). Feeds the edit-plan contract
(`veb-p12`) and highlight selection (`veb-0rh.1`).

Companion guide: [Creating short videos and measuring success](../../docs/research/short-video-success.md)
covers platform evidence (Shorts, Reels, TikTok), output-quality acceptance, and how
to measure a published clip. Read the two together: this guide sets the editing
defaults for knowledge-sharing recordings; that one covers social distribution.

Source material: 1–2 hour recorded knowledge-sharing sessions (Meet/Zoom, screen
shares, Q&A). Audience: busy professionals who will not watch the whole thing.

## The five rules

1. **One idea per clip.** A clip answers one question or lands one takeaway.
   If a segment needs "and also…" it is two clips.
2. **Payoff first.** Open on the conclusion, the surprising result, or the
   problem being solved. Cut greetings, intros, "can you see my screen", and
   setup. Most drop-off happens in the first few seconds. [1][7]
3. **30–90 s default.** Single tip: 15–30 s. Explanation with a demo: up to
   120 s. Never longer than 120 s without a human override. [2][9][10]
4. **Captions on, always.** Roughly 85 % of LinkedIn video is started muted
   (secondary-source figure, but the direction is not in dispute). Burn-in
   captions from the transcript. [3][9]
5. **Keep 16:9 when the screen matters.** Slides, code, terminals, and
   multi-person grids lose essential detail when cropped to 9:16. Only go
   vertical when a single talking head or one focal element fills the frame. [2]
   Both framings are context-dependent project defaults (16:9 for screen content,
   9:16 for vertical social clips), not platform requirements or success rules; the
   contract's `output.preset` picks between them.

## Length targets by destination

| Destination | Target | Why |
|---|---:|---|
| Slack / internal | 45–120 s | Colleagues want enough context to act; link the source + transcript. |
| LinkedIn | 30–90 s | Self-contained insight; 15–30 s for a single tip. [2][6][9] |
| YouTube Shorts | 20–40 s | Guidance conflicts (15–30 s vs 50–60 s); test on completion rate. [1][10] |
| Email | 30–60 s + text takeaway | Put the point in the email body, clip is optional depth. |

Retention benchmarks are broad: steep early drop, then gradual decline.
Under 5 min videos average 50–70 % watched. Treat these as priors, not targets,
and measure our own clips by completion rate, not views. [2][7]

## Text alongside the clip

Every clip ships with a one-sentence takeaway. The whole session ships with an
executive summary followed by the full transcript (Claudia's ask, `veb-t2b.6`).
No study in hand quantifies the lift, so this is a usability default, not a
claim. Cheap to produce, easy to A/B later.

## How this maps to the edit plan (`veb-p12`)

Proposed fields the contract must carry so the renderer can honor these rules:

| Guideline | Edit-plan field | Default |
|---|---|---|
| Length bounds | `clip.min_seconds`, `clip.max_seconds` | 15 / 120 |
| Payoff first | `clip.hook_offset_seconds` (where the payoff sentence starts; renderer may lead with it) | 0 |
| One idea | `clip.takeaway` (one sentence, also used as caption card / filename) | required |
| Captions | `output.captions` = `burn_in` / `sidecar_srt` / `none` | `burn_in` |
| Aspect | `output.aspect` = `16:9` / `9:16` / `1:1` | `16:9` |
| Silence / filler | `clip.trim_silence` (bool), `clip.remove_fillers` (bool) | true / false |
| Destination presets | `output.preset` = `internal` / `linkedin` / `shorts` / `email` | `internal` |

Presets set length bounds and aspect; explicit fields override presets.

## Highlight scoring (`veb-0rh.1`) — what "a good moment" looks like

Score transcript segments on:

- **Self-contained**: understandable without the previous 5 minutes.
- **Quotable**: a claim, a number, a decision, a "here's the trick".
- **Q&A pairs**: a question from the audience plus a complete answer is a
  natural clip (start at the question, end when the answer lands).
- **Not**: logistics, screen-share fumbling, greetings, "can you hear me".

Down-rank segments that only make sense with the screen visible unless the
clip keeps 16:9.

## Demo defaults (today's 72 s sample, `assets/demo-clip.mp4`)

`preset=internal`, `aspect=16:9`, `captions=burn_in`, `min=15`, `max=45`.
Expect 1–2 clips out of a 72 s source. Anything else is over-cutting.

## Sources

1. https://www.opus.pro/blog/ideal-youtube-shorts-length-format-retention
2. https://contentin.io/blog/linkedin-video-format/
3. https://www.zebracat.ai/post/linkedin-video-marketing
4. https://vidiq.com/blog/post/increase-audience-retention-youtube/
6. https://blog.hootsuite.com/linkedin-video/
7. https://humbleandbrag.com/blog/youtube-audience-retention-benchmarks
9. https://www.opus.pro/blog/ideal-linkedin-video-length-format-for-retention
10. https://piktochart.com/blog/how-long-youtube-shorts/
