---
name: clipbot-style
description: Choose, change or create a style for video-editor-bot reels. A style is the music (scored to the cards) and the transition. Use when a user asks for different music, a theme, a sting, an outro, transitions, a "look", a brand sound, or says a reel's music is wrong. Also use to make new music or a new transition by having agents write it as code.
---

# clipbot-style: the sound and the joins of a reel

Read `docs/styles.md` first. It is short and it is the source of truth. This file is the
procedure.

## The rule that is not negotiable

**Pipeline AI Talks videos always use `--style pipeline`** (or `"style": "pipeline"` in the
framing file). It is the series theme. Do not pick a different style for a Pipeline video, do
not regenerate the theme, and do not "improve" its blocks unless Kyle asks for exactly that.

## Use a style

```
uv run --project bot clipbot styles
uv run --project bot clipbot reel --source <file> --style <name> --render --out out/<name>/plan.json
```

- A user who has not said what they want gets a choice: run `clipbot styles`, show the titles
  and descriptions in their words, and ask once. For a series, put the answer in the framing
  file so the next video matches.
- After a hand edit of card seconds or the transition in a cue-style plan, re-cut the music:
  `uv run --project bot clipbot score out/<name>/plan.json --style <name>`, then render.
- Level too loud or quiet under the cards: framing `"music_gain_db"` (more negative is
  quieter). Music never plays under speech; if a user asks for that, say it is by design and
  why (the voices stay clear), then offer `--music FILE` with the contract's `under: all`.

## Check a reel's music after rendering

You cannot hear it. Measure it, and say that you measured rather than listened.

- Under a card run: `ffmpeg -ss <start> -t <len> -i reel.mp4 -af volumedetect -f null -` shows
  music (mean around -25 to -30 dB for `pipeline`).
- In the first second of every clip: the same level as the rest of the clip, no music tail.
- The last 0.3 s of the reel: below -50 dB. The ending finished; it was not cut off.
- Then tell the user which moments to listen to: the first card, one chapter card, the ending.

## Make new music

1. Get the feel in the user's words. Ask for a reference feeling, not a genre. Push for a
   groove and a hook: ambient pads were rejected here as "elevator music".
2. Spawn one agent per candidate (two or three, different directions) with
   `references/music-brief.md` filled in. Sonnet-class models are right for this. Each writes a
   `synth.py` (numpy and scipy only), renders it, measures it and looks at its spectrogram.
3. Verify each result yourself: loudness, peak, mono downmix, a spectrogram you open.
4. Send the user ONE track to hear. Do not build a style on music nobody has listened to.
5. Approved, and it should be cut to the cards? Add the blocks the scorer needs (pickup, vamp
   bars, resolved variants, hold, outro) the way `assets/styles/pipeline/music/synth.py` does,
   write `style.json`, and add a row to `assets/styles/README.md`. A bed needs only the file.

## Make a new transition

1. One agent per transition, each with one distinct concept, using
   `references/transition-brief.md`. The agent must open its own contact sheets
   (`scripts/style_lab.py sheet`) and iterate.
2. Run `scripts/style_lab.py check` yourself at 1920x1080 and open a sheet yourself.
3. Put the file in `assets/styles/transitions/` and name it in a style's `transition`
   (`{"kind": "module", "module": "../transitions/<file>.py", "seconds": 1.0}`).
4. Render a short reel with it and look at frames from the middle of two joins before you
   show the user.

## Report to the user

What changed (style, music, transition), where the files are, what you measured, and what
you could not judge: whether it sounds good. Ask them to listen to named moments.
