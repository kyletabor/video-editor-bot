# assets/styles/

Style packs: the music and the transition of a reel, chosen with `clipbot reel --style NAME`.
How they work and how to make one: [`docs/styles.md`](../../docs/styles.md). The format of
`style.json`: `bot/clipbot/styles.py`.

| Style | Files | What made it |
|---|---|---|
| `pipeline/` | `music/theme.mp3` (the whole tune, 63 s), nine cue blocks in `music/*.flac`, `music/synth.py` | "Stank", the Pipeline AI Talks theme. Written for this project as code on 2026-10-01; `synth.py` renders the tune and every block. Approved by Kyle by ear |
| `signal/` | `music/signal.mp3`, `music/synth.py` | Written as code by a Sonnet 5.5 agent, 2026-10-01 |
| `aurora/` | `music/aurora.mp3`, `music/synth.py` | Written as code by a Sonnet 5.5 agent, 2026-10-01 |
| `future/` | `style.json` only | Signal's bed with the token-stream transition |
| `classic/` | `style.json` only | Points at `../music/bed.mp3` (1924, US public domain; see `../music/LICENSE`) |
| `transitions/` | six `.py` files | Code-drawn transitions (`scripts/style_lab.py` has the contract). Five written by Sonnet 5.5 agents on 2026-10-01, plus `plain_dissolve.py` as the minimal example |

## Rights

Everything under `pipeline/`, `signal/`, `aurora/` and `transitions/` is original work made
for this project: the audio is the output of the `synth.py` beside it (numpy and scipy
oscillators, filters and noise; no samples, no recordings, no third-party material), and the
transitions are plain Python. Nothing here needs a licence from anyone outside the project.
`classic/` adds no audio of its own.

## Rebuilding the Pipeline theme

```
uv run --python 3.12 --with numpy --with scipy python assets/styles/pipeline/music/synth.py --out out/stank
for b in pickup riff-1 riff-2 riff-3 riff-4 sting-1 sting-3 hold outro; do
  ffmpeg -y -i out/stank/blocks/$b.wav -c:a flac -compression_level 8 assets/styles/pipeline/music/$b.flac
done
ffmpeg -y -i out/stank/theme.wav -c:a libmp3lame -b:a 192k assets/styles/pipeline/music/theme.mp3
```

The synth is seeded: the same code gives the same samples. If a block's length changes,
update `seconds` in `pipeline/style.json` (a test compares the outro's stated length with the
file). FLAC, not MP3, for the blocks: they are placed to the sample, and MP3 adds encoder
delay.

**The theme is the series' identity. Change it only when Kyle asks.**
