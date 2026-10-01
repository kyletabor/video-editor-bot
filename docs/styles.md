# Styles

A style is the sound and the joins of a reel: its music and its transition, picked by name.
The cuts, the cards' text and the captions do not change with the style.

```
uv run --project bot clipbot styles
uv run --project bot clipbot reel --source talk.mp4 --style pipeline --render --out out/talk/plan.json
```

A `--framing` file can pin it instead (`"style": "pipeline"`), which is how a series keeps one
sound across every video. `--style` beats the framing file; an explicit `--music FILE`,
`--transition` or framing `transition` beats the style.

## What ships

| Style | Music | Transition | Use it for |
|---|---|---|---|
| `pipeline` | "Stank": funk organ, swung drums, 104 BPM. Cut as cues (below) | dip | **Every Pipeline AI Talks video.** This is the series theme; do not vary it per episode |
| `future` | "Signal" bed | token stream, drawn in code | Product and AI-explainer reels that want a tech look |
| `signal` | "Signal": interlocking plucks, 96 BPM, no drum kit | dissolve | Light forward motion |
| `aurora` | "Aurora": pad and bells, 85 BPM | dissolve | Quiet, reflective material |
| `classic` | 1924 Kansas City jazz (public domain) | dip | The first reels' sound |

All music is under the cards only. Speech never has music under it.

## How cue music is scored

A bed (`signal`, `aurora`, `classic`) is one file that fades in wherever the cards are. It
enters and leaves mid-phrase. `pipeline` is written to be cut instead: its music ships as
blocks that start on a beat, and `bot/clipbot/score.py` builds one cue for every run of cards.

```
reel:   [intro][opening][card 1] clip 1 [card 2] clip 2 ... clip N [closing][closing][outro]
music:  pickup, riff bars, held chord     sting+chord             pickup, riff bars, the tune's ending
        └──────── first run ────────┘     └ chapter ┘             └──────────── last run ───────────┘
```

- **First run.** Pickup fill, as many riff bars as fit, then a held chord that fades as the
  first clip arrives.
- **Chapter card.** One sting, then the chord for what is left of the card. The stings rotate,
  so ten cards are not ten copies. A card of five seconds or more gets riff bars.
- **Last run.** Back-timed. The outro block is placed so the tune's own ending finishes 0.2 s
  before the reel does; riff bars fill the time before it and the pickup leads in. The music
  ends. It is not faded out.
- The bar before a held chord or the outro is its `resolved` variant, so the harmony lands home.

`clipbot reel --style pipeline` writes the cues to `music-cues.wav` next to the plan and points
`output.reel.music` at it with `under: cards`. The renderer already takes one piece per card run
from a running position in the file, so cue *k* plays under run *k* with no renderer support
beyond what a bed uses. The file is cut to the cards as they are in the plan: **after editing
card seconds or the transition by hand, run `clipbot score out/<name>/plan.json --style pipeline`**.

Level: `gain_db` in the style (framing `music_gain_db` overrides it). `pipeline` sits about
4 LU under the speech.

## Make a new style

A style is a directory with a `style.json`; `bot/clipbot/styles.py` documents every key and
rejects anything it does not know. Put it in `assets/styles/<name>/`, or anywhere and pass the
directory to `--style`.

```json
{
  "name": "mine",
  "title": "Mine: one line a person reads in `clipbot styles`",
  "description": "What it sounds like and when to use it.",
  "music": {"kind": "bed", "file": "music/bed.mp3", "gain_db": -8, "fade_seconds": 1.0},
  "transition": {"kind": "module", "module": "../transitions/token_stream.py", "seconds": 1.0}
}
```

Everything shipped here was written as code by AI agents and judged by a person. That is the
recommended way to make more, and `.agents/skills/clipbot-style/` walks an agent through it.

### Music: write a synthesizer, not a prompt for a music model

The music is a Python program (numpy and scipy, seeded, no samples) that renders the tune.
Nothing to license, any length on demand, and a tune written in bars can be cut into cue
blocks by the same code (`assets/styles/pipeline/music/synth.py` is the worked example: the
tune comes out of `render_tune`, the blocks out of `render_block`, through one mix chain).

1. **Brief the feel, not the genre label.** "Calm, quietly confident keynote" produced music
   Kyle called elevator music. "A beat, a stanky organ melody" produced the theme. Ask for a
   groove, a hook and some dirt.
2. **One agent per candidate**, each with a different direction, and the brief in
   `.agents/skills/clipbot-style/references/music-brief.md`. Sonnet-class models write good
   synthesis code; give them the targets and make them verify.
3. **The agent cannot hear.** It verifies by measurement (integrated loudness, true peak, mono
   downmix, level across 10 s windows) and by looking at a spectrogram. That catches clicks,
   clipping, mud and silence. It does not catch boring.
4. **A person listens before anything is built on it.** Send one track. Expect to throw
   candidates away.
5. For cue music, render the blocks the scorer needs: `pickup`, the vamp bars, a `resolved`
   variant for each bar that does not lead home, a `hold`, and an `outro`. Each block starts on
   its first beat and carries its ring-out past its nominal length. Blocks must come out of the
   same gains as the tune, or the cue changes level at every join.

### Transitions: one function per file

A code-drawn transition is a Python file with `render(a, b, t, state)` that returns the frame
at progress `t` between the outgoing frame `a` and the incoming frame `b`. `scripts/style_lab.py`
holds the contract and three commands: `sheet` (a contact sheet to look at), `check` (the
contract and the speed budget) and `render`.

1. **One agent per transition, each with a distinct concept**, and the brief in
   `.agents/skills/clipbot-style/references/transition-brief.md`. Five agents given one line
   each ("neural mesh", "token stream", ...) returned five different, usable transitions.
2. **Make the agent look.** It must open its own contact sheets and iterate at least twice;
   first versions look flat.
3. Say what the footage is. Screen recordings and the cards are dark, so a transition only
   reads if it brings its own light.
4. `check` must print ALL PASS at 1920x1080, then a person watches it between a card and a clip.
5. Match the transition to the music. The glowing, technical ones suit `future`; they fight
   the funk of `pipeline`, which is why that style keeps the plain dip.

The files in `assets/styles/transitions/` are the ones written so far. Cost: 0.1 to 0.4 s per
frame at 1080p, so about ten seconds per join. Rendering a reel with a module transition needs
numpy in the renderer (`uv run --project render --extra styles cliprender ...`; `clipbot reel
--render` adds it for you).

## Provenance

`assets/styles/README.md` lists every file, what made it and why it may be used.
