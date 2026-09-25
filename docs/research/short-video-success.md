# Creating short videos and measuring success

Research checked: **2026-09-25**. Scope: organic YouTube Shorts, with applicable
Instagram Reels and TikTok guidance. Prepared for `video-editor-bot`.

Companion guide: [Clip guidelines](../../bot/research/clip-guidelines.md)
covers digestible excerpts from recorded knowledge-sharing sessions, including
screen shares and internal distribution. Read its duration and framing guidance
as project defaults for that context alongside this guide's social-video and
evaluation guidance.

The project should aim to produce a clear, satisfying clip for a particular
audience, then learn from its performance. A technically valid export, a good
edit, and a successful published post are three separate outcomes.

## Evidence and limits

This guide uses primary sources: platform help pages and announcements, an
official creator interview, and W3C accessibility guidance. Citations appear
beside the claims they support; source dates and links are collected below.

- **Platform documentation** describes published behavior or metric definitions;
  it does not disclose the complete recommendation algorithm.
- **Creator practice** supplies useful creative hypotheses, not causal proof.
- **Advertising guidance** concerns paid campaigns. Applying it to organic
  clips is an experiment, not an established organic ranking rule.
- **Project recommendations** below are editorial or evaluation choices proposed
  for this bot. They are not implemented features or platform requirements.

No channel analytics or controlled experiment was supplied for this research.
The reviewed sources do not establish a universal winning duration, retention
threshold, posting time, cut frequency, or formula that guarantees virality.

## What the evidence supports

| Finding | Evidence | Consequence for this project |
| --- | --- | --- |
| Audience fit, continued watching, and satisfaction all matter. | YouTube names choosing to watch, average view duration, average percentage viewed, likes, and surveys. Recommendations also reflect viewer interests. [YouTube discovery][yt-discovery] | Choose a specific audience and promise; evaluate the opening and the rest of the story separately. |
| A strong clip can receive limited distribution. | YouTube identifies topic demand, competition, and seasonality as external influences. It specifies no minimum posting cadence. [YouTube discovery][yt-discovery] | Avoid grading an editor solely on public views or prescribing daily uploads as an algorithm requirement. |
| An immediate opening and a complete small story are useful creative practices. | YouTube's interview with creator Jenny Hoyos and product lead Todd Sherman emphasizes hooks and concise narrative moments. [Shorts interview][yt-story] | Start with meaningful action, tension, a result, or a useful question. The interview's one-second advice is creator experience, not an algorithm cutoff. |
| Watch behavior matters on TikTok too. | TikTok names full watches, skips, likes, shares, and comments among For You signals; interaction signals, including time spent watching, generally carry more weight for most users. [TikTok recommendations][tt-ranking] | Inspect retention and audience response together; do not invent signal weights. |
| Clear presentation is a practical foundation. | TikTok's ad guidance recommends vertical framing, UI-safe placement, sound, and hook/body/close structure. [Creative Codes][tt-creative] W3C says automatic captions need accuracy checks. [Captions][wai-captions] | Treat production guidance as a starting point and caption correctness as a quality requirement, without promising a reach increase. |

## A practical creation workflow

The following workflow is a **project recommendation** informed by the evidence
above. Its timing, style, and amount of editing should vary with the material.

1. **Define one audience and one outcome.** Write a sentence explaining who the
   clip is for and what they should learn, feel, or do. A useful tutorial, a
   funny reaction, and a product demonstration need different payoffs.
2. **Select a moment that stands alone.** Keep the context needed to understand
   names, pronouns, claims, and reactions. A loud or surprising sentence is not
   automatically a useful clip. Reject cuts that alter the speaker's meaning.
3. **Make the opening specific.** Try showing the result, beginning at the
   action, or posing the exact question the clip answers. Remove greetings or
   logos when they delay the premise. The promise must match the actual payoff.
4. **Build a small story.** Establish the situation, show the evidence or change,
   then resolve it. Let the viewer understand why the moment matters. Avoid
   forcing a cliffhanger when the selected footage can deliver the answer.
5. **Trim for comprehension.** Remove repetition and inactive gaps while keeping
   natural speech, useful pauses, and emotional reactions. Add a cut, zoom,
   diagram, or supporting shot when it directs attention or explains something.
   Do not enforce a cut every fixed number of seconds.
6. **Make the content legible and audible.** Prioritize intelligible speech,
   accurate captions, and a crop that preserves the important action. Use music
   when it serves the scene. Check the result on a phone-sized display and with
   sound off; caption relevant non-speech information where needed.
7. **End after delivering value.** Finish at a natural sentence or action
   boundary. Use a relevant next step only when it supports the chosen outcome.
   A loop is optional; never sacrifice understanding to manufacture a replay.
8. **Package for the destination.** Use a truthful, specific title or post
   description. Keep an original master and preview the actual platform crop,
   caption placement, and interface overlays before publishing.

For example, an interview excerpt about an editing mistake could open on the
visible mistake, preserve the speaker's explanation, show the corrected result,
and end after the comparison. That is a proposed edit pattern, not an observed
successful post from this project.

YouTube describes text and voiceover as tools for context and accessibility.
Its visual guides help keep overlays clear of feed controls. These support
readable presentation, but do not establish one safe-zone rectangle for every
device. [Editing tips][yt-editing], [Shorts visual guides][yt-guides]

## Platform differences that affect decisions

| Platform | Verified guidance or behavior | How to apply it |
| --- | --- | --- |
| YouTube Shorts | For standard channels, square or vertical uploads up to three minutes, uploaded on or after October 15, 2024, are categorized as Shorts. [Three-minute Shorts][yt-duration] | Three minutes is a format ceiling, not an ideal duration. Preserve the story and test lengths within similar content. |
| YouTube discovery surfaces | Search considers metadata relevance and whether people click and watch. Feed discovery is personalized. [YouTube discovery][yt-discovery] | Separate search traffic from feed traffic when evaluating packaging. Thumbnail CTR and feed stayed-to-watch answer different questions. |
| TikTok | Organic creator guidance recommends relevant descriptions and hashtags; adding more hashtags or `#FYP` does not guarantee distribution. [Creator tips][tt-tips] | Describe the subject in audience language. This source dates to 2020; use it as creative guidance, not current UI documentation. |
| TikTok production | The 9:16, at-least-720p, sound, and hook/body/close recommendations come from advertising guidance. [Creative Codes][tt-creative] | Test these sensible presentation choices for organic posts. Ad-recall statistics are not organic retention benchmarks. |
| Instagram Reels | Meta's May 2024 rollout announcement describes original content and no visible watermark among recommendation-eligibility examples; matching reposts may be replaced with originals. [Originality announcement][ig-original] | Export from the original source without another app's watermark. This dated announcement is not a guarantee of current reach or a complete eligibility specification. |
| Instagram experiments | Trial Reels reach non-followers first. Meta describes metrics after roughly 24 hours and optional automatic sharing based on views in the first 72 hours. [Trial Reels][ig-trials] | Useful for exploring formats where available. It is not documented as randomized A/B testing, and those windows are not universal success deadlines. |

## Output quality before publishing

These are **proposed project acceptance criteria**, separate from post-publication
performance. Technical validation can catch some defects; editorial judgment and
a full playback are still necessary.

| Area | Proposed acceptance criterion | Suitable verification |
| --- | --- | --- |
| File integrity | The complete export decodes and plays; intended video and audio tracks exist. | Media probe plus full decode/playback. |
| Timing | Cuts match the approved selection; speech and visible action stay synchronized; no accidental frozen or black frames. | Inspect cut boundaries and play the complete clip. Intentional pauses or black frames are allowed. |
| Framing | The subject, demonstration, and essential text remain visible throughout. | Inspect moving scenes and preview the destination UI. Avoid blindly center-cropping screen recordings or two speakers. |
| Audio | Speech is understandable; no audible clipping, clicks, abrupt level jumps, or music masking the words. | Listen to the exported file. A loudness number alone cannot certify intelligibility. |
| Captions | Wording, names, numbers, negations, timing, line breaks, contrast, and placement are accurate and readable. | Compare with the audio and inspect at phone size. Include relevant speaker/sound information. [W3C guidance][wai-captions] |
| Editorial integrity | The clip stands alone, preserves the source's meaning, and delivers its opening promise. | Compare the selection against surrounding source context. |
| Ending | The payoff remains visible or audible long enough to understand; no unintended sentence or action cutoff. | Watch through the last frame and listen through the last word. |

**Proposed project framing defaults:** use a 9:16, 1080-by-1920 canvas for
vertical social clips when the source and destination suit it. Preserve 16:9
for screen recordings, slides, code, terminals, or multi-person layouts when
cropping would hide essential detail, as described in the companion guide.
Choose framing for the content and destination; these are project defaults,
not universal platform requirements or success rules. A landscape export does
not become a YouTube Short just because it is brief; see the format criteria
above. Do not claim upscaling repairs low-quality footage. Codec, bitrate, and
frame-rate presets should be chosen and validated in the renderer's own
implementation work.

## How to tell whether a published short succeeds

Choose the primary outcome before publishing: relevant discovery, useful
engagement, audience growth, or a specific measurable next action. Track the
stages separately; do not collapse them into an unexplained "viral score."

| Stage | Useful evidence | Interpretation |
| --- | --- | --- |
| Exposure | Platform views, reach where available, feed exposure, and traffic source. | Was it distributed, and where? These are different measures, not interchangeable denominators. |
| Opening | YouTube stayed-to-watch, plus early retention where available. | Did people continue beyond the opening? Do not substitute thumbnail CTR for this. |
| Sustained attention | Average view duration, average percentage viewed, and the retention curve. | Did the story hold attention? Compare similar durations and viewing contexts. |
| Audience value | Shares, saves where available, likes, and the substance of comments. | Look for usefulness, enjoyment, questions, or confusion. These are proxies, not direct access to viewer satisfaction. |
| Intended outcome | Video-attributed subscribers/follows, or a defined and attributable next action. | Did the clip serve its goal? Distinguish platform attribution from a separately instrumented business outcome. |

### Read the metric definitions correctly

YouTube defines **engaged views** as watching beyond the initial seconds,
excluding loops. **Stayed-to-watch** is the percentage of times people stay
beyond those initial seconds. **Average view duration** and **average percentage
viewed** use engaged views and their corresponding watch time. Thus strong
retention among people who stayed can coexist with a weak opening.
[YouTube metric definitions][yt-metrics]

Shorts public views have counted starts and replays without a minimum watch
duration since **March 31, 2025**. [Creating Shorts][yt-start] A subsequent
YouTube announcement says first-frame view counting extends across formats from
**August 24, 2026**, while most analytics remain anchored to engaged views and
the counting update does not change recommendations. Preserve definition dates
when comparing historical results. [Engaged-view update][yt-views]

**Average percentage viewed is not completion rate.** For example, 24 seconds
of average viewing on a 30-second clip is 80% average viewing; it does not prove
80% of viewers reached the end. Do not infer a completion percentage from an
average or infer unique viewers from replay-inclusive counts.

Meta's 2023 Reels announcement describes total watch time as including replays.
It describes average watch time using total plays; because this is historical
documentation, verify the current report/export definition before reproducing
the formula. Do not assume its denominator matches YouTube's.
[Reels watch-time announcement][ig-insights]

Use the native stayed-to-watch metric instead of estimating it by dividing
all-source engaged views by feed impressions. For a custom ratio, record both
counts, the denominator's definition, traffic scope, and time window. For
example, `1,000 * subscribers attributed to the clip / engaged views` can be a
project comparison metric when both inputs cover the same clip and window. It
is not a unique-person conversion probability. Return unavailable when the
denominator is zero or missing.

### Diagnose before changing the edit

The following are **hypotheses to investigate**, not automatic diagnoses.

| Observation | Inspect next | Candidate experiment |
| --- | --- | --- |
| People leave immediately. | First frame, premise clarity, audience match, and initial audio. | Open on the result or action and remove delayed setup. |
| People stay initially, then leave at one moment. | The scene at the drop: repetition, confusing jump, obscured text, or an early payoff. | Clarify or shorten that segment while preserving meaning. |
| Retention spikes around an explanation. | Whether people are enjoying it or replaying because it is hard to follow. | Make the explanation more legible before assuming the spike is desirable. |
| Attention is strong but shares or follows are weak. | Whether the clip delivers useful value and fits the channel's continuing promise. | Test a more relevant topic, payoff, or next step. |
| Good audience response but low reach. | Traffic source, topic demand, eligibility, and comparable distribution history. | Gather more comparable observations before treating editing as the cause. |

YouTube says retention data generally needs one to two days to process; dips
can reflect exits/skips, while spikes can also reflect confusion. Its automatic
key-moment highlights have availability conditions, including at least 60
seconds and 100 views, so do not promise these labels for every sub-minute
clip. These are reporting conditions, not success thresholds.
[Retention guidance][yt-retention]

## A repeatable learning plan

The process below is a **proposed evaluation method**, not a proven platform
growth recipe. YouTube recommends comparing the same content type and learning
from both stronger and weaker posts rather than treating every viral hit as the
baseline. [Shorts analytics tips][yt-analytics]

1. Establish a recent baseline of comparable posts from the same account,
   platform, topic family, language, approximate duration, and traffic source.
   Keep paid and organic results separate. If no comparable history exists,
   call the first batch exploratory.
2. Specify one hypothesis, one primary metric, and quality guardrails before
   the edit. Example: "Opening on the demonstration result will improve
   stayed-to-watch without reducing comprehension or changing the claim."
3. Change one main creative variable at a time: opening, context length,
   caption style, or payoff timing. Keep the source selection and other choices
   as similar as practical. Record unavoidable differences.
4. Set observation windows in advance. A practical starting schedule is 48
   hours, seven days, and 28 days after publication; these are project choices,
   not algorithm deadlines. Compare posts at the same age and recheck delayed
   analytics. Avoid declaring failure from the first hour.
5. Retain raw counts and cohort sizes alongside rates, medians, and spread.
   A high rate from very few observations is weak evidence. There is no
   universal sample size: uncertainty depends on the metric, baseline,
   variability, and size of change worth detecting.
6. Repeat across multiple comparable clips. Ordinary organic uploads do not
   randomize audiences: timing, distribution, topic, and follower mix can
   confound results. Call the result directional unless the design supports a
   stronger claim. Trial Reels do not remove this limitation.
7. Promote a pattern to a project default only when it repeatedly supports the
   intended outcome and passes quality checks. Keep unsuccessful experiments
   in the comparison so the record is not biased toward winners.

A minimal evaluation record should retain the source and output identifiers,
edit version, platform/post identifier, publish and observation times, duration,
audience/topic, hypothesis, changed variable, traffic scope, native metric
names and definitions, counts, missing values, and editorial observations.
This is a conceptual data requirement, not a new edit-plan schema.

## Implications for video-editor-bot

At the researched repository baseline, the runtime, edit-plan contract, renderer,
and shared quality gate are still pending. This document proposes behavior for
their owners to consider; it does not claim the bot already supports it.

| Component | Proposed responsibility |
| --- | --- |
| Request and planning (`bot/`, Kyle) | Capture audience, destination, intended outcome, essential context, and the clip's promise; identify a complete source moment. |
| Shared edit-plan design (`contract/`, joint review) | Consider how to express intended crop, timing, caption placement, and source provenance when the contract is designed. This guide introduces no fields or schema changes. |
| Rendering (`render/`, Ramsey) | Execute the approved plan, preserve intelligibility and framing, and report technical validation failures. A successful render is not a prediction of distribution. |
| Human evaluation | Judge meaning, pacing, caption accuracy, and payoff using the rubric above. |
| Performance learning | Associate approved edit versions with observations and outcome metrics; keep recommendations explainable and distinguish missing data from zero. |

The first useful product milestone is a coherent, correctly rendered clip with
a traceable edit decision. Measured audience learning can then inform creative
defaults. Runtime and ownership decisions remain governed by the existing
project process.

## Sources and refresh policy

All sources were checked on **2026-09-25**. "Undated" means no reliable
publication/update date was used; it does not mean the source is timeless.
Recheck platform definitions, format limits, and feature availability before
implementation or a new analytics comparison. Revisit the research when a
platform changes its published guidance.

| Source | Published date used | Evidence type / limitation |
| --- | --- | --- |
| [YouTube: Search and discovery tips][yt-discovery] | Undated | Official organic discovery guidance; no complete algorithm or universal thresholds. |
| [YouTube: Shorts deep dive][yt-story] | 2025-01-28 | Creator/product-lead interview; creative experience, not a controlled study. |
| [TikTok: How TikTok recommends content][tt-ranking] | Undated | Official ranking overview; indexed official text was available when the direct page yielded no body. |
| [TikTok: Creative Codes][tt-creative] | Undated | Paid-ad guidance; organic application requires testing. |
| [W3C WAI: Captions/Subtitles][wai-captions] | Undated | Accessibility guidance; no engagement-lift claim. |
| [YouTube: Shorts editing tips][yt-editing] | Undated | Product/creator guidance. |
| [YouTube: Enhance your Shorts][yt-guides] | Undated | Current UI-placement guidance; device presentation varies. |
| [YouTube: Understand three-minute Shorts][yt-duration] | Includes 2024-10-15 format change | Format documentation; linked page also contains changing music/claim rules outside this guide's scope. |
| [TikTok: 5 tips for creators][tt-tips] | 2020-07-30 | Historical organic advice; not current UI instructions. |
| [Meta: Helping creators find new audiences][ig-original] | 2024-05-02 | Official Spanish-language announcement; summarized in English; historical rollout. |
| [Meta: Trial Reels][ig-trials] | 2024-12-10; page also lists 2025-06-26 | Official feature announcement; check account availability. |
| [YouTube: Content performance][yt-metrics] | Includes 2026-08-24 counting change | Native metric definitions; retain report scope. |
| [YouTube: Get started creating Shorts][yt-start] | Includes 2025-03-31 counting change | Shorts view-count transition. |
| [YouTube: Engaged views explained][yt-views] | 2026-08-19 | Announces cross-format change effective 2026-08-24. |
| [Meta: Reels insights updates][ig-insights] | 2023-04-14; page also lists 2023-10-19 | Historical metric explanation; verify current denominators. |
| [YouTube: Audience retention][yt-retention] | Undated | Interpretation and report-availability guidance. |
| [YouTube: Shorts analytics tips][yt-analytics] | Undated | Comparative analytics guidance. |

[yt-discovery]: https://support.google.com/youtube/answer/11914225?co=YOUTUBE._YTVideoType%3Dshorts&hl=en-GB
[yt-story]: https://blog.youtube/creator-and-artist-stories/youtube-shorts-deep-dive/
[tt-ranking]: https://support.tiktok.com/en/using-tiktok/exploring-videos/how-tiktok-recommends-content
[tt-creative]: https://ads.tiktok.com/business/en/creative-codes
[wai-captions]: https://www.w3.org/WAI/media/av/captions/
[yt-editing]: https://support.google.com/youtube/answer/13380879?hl=en
[yt-guides]: https://support.google.com/youtube/answer/16215842?hl=en-GB
[yt-duration]: https://support.google.com/youtube/answer/15424877?hl=en
[tt-tips]: https://newsroom.tiktok.com/5-tips-for-tiktok-creators?lang=en
[ig-original]: https://about.fb.com/ltam/news/2024/05/ayudando-a-los-creadores-a-encontrar-nuevas-audiencias/
[ig-trials]: https://about.fb.com/news/2024/12/trial-reels-try-content-non-followers-first-see-what-perfoms-best/
[yt-metrics]: https://support.google.com/youtube/answer/12220281?co=GENIE.Platform%3DDesktop&hl=en
[yt-start]: https://support.google.com/youtube/answer/10059070?hl=en
[yt-views]: https://blog.youtube/inside-youtube/engaged-views-youtube-explained/
[ig-insights]: https://about.fb.com/news/2023/04/instagram-reels-trending-audio-and-gifts-updates/
[yt-retention]: https://support.google.com/youtube/answer/9314415?hl=en
[yt-analytics]: https://support.google.com/youtube/answer/12942217?co=YOUTUBE._YTVideoType%3Dshorts&hl=en
