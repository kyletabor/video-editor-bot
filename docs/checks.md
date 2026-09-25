# Shared quality gate

From the repository root, use the same command in PowerShell, Bash or a terminal
on Linux/macOS:

```text
uv run --locked scripts/check.py
```

`make check` is an optional alias. GNU make and a POSIX shell are not required
for the Python entry point. Install Git, Python 3.11 or newer, and
[uv](https://docs.astral.sh/uv/getting-started/installation/). CI uses Python
3.12.10 and uv 0.8.22. Script dependencies are separate from each lane's project
and locked in `scripts/check.py.lock`; no root Python project is needed.

## Reproducible FFmpeg tools

For the reference toolchain used by Windows/Linux CI:

```text
python scripts/install_ffmpeg.py
uv run --locked scripts/check.py --require-pinned
```

The installer downloads **FFmpeg and ffprobe 7.0.2**, verifies the archive's
SHA-256 against `scripts/ffmpeg-lock.json`, and installs both under
`.tools/ffmpeg/`. The gate adds this directory to its subprocess PATH, so it
also applies to bot and renderer tests without changing the system installation.
The installer supports Windows x64 and Linux x64, ARM64 and ARM hard-float.
The pinned sources are [Gyan's Windows release](https://github.com/GyanD/codexffmpeg/releases/tag/7.0.2)
and [John Van Sickle's Linux builds](https://johnvansickle.com/ffmpeg/).
Archive and binary hashes in the installation receipt make repeated setup safe.

For other commands, add `.tools/ffmpeg` to PATH yourself. In PowerShell:

```powershell
$env:PATH = "$(Resolve-Path .tools/ffmpeg);$env:PATH"
```

In Bash:

```bash
export PATH="$PWD/.tools/ffmpeg:$PATH"
```

Existing installations, including Kyle's Ubuntu FFmpeg 4.4.2, may instead run
the default compatibility gate. Both executables must report the same release,
at least 4.4, and FFmpeg must expose `libx264`, AAC, `subtitles`, `trim`, `atrim`,
`setpts`, `asetpts`, `concat`, `scale` and `crop`. The generated smoke must also
pass. This is a compatibility check, not a claim that the binaries match CI.
On macOS use installed tools that meet these requirements; the reference
installer has no macOS build. Run with `--require-pinned` to reject any release
other than 7.0.2. Changing the reference requires updating the lock, version
constant and this guide, then validating both CI jobs.

## What the command checks

- Whitespace in the branch delta and staged/unstaged changes. `CHECK_BASE` can
  specify a Git revision; PR CI supplies the PR's base SHA. A failed diff fails
  the gate without a fallback that could hide it.
- Schema validity, all contract examples, and existing rejection cases.
- Gate regression tests and the demo asset's audio/video streams.
- A fresh five-second, 640x360, 30fps test pattern and 48kHz sine wave encoded
  with H.264/yuv420p and AAC, inspected with ffprobe and fully decoded.
- Bot tests once `bot/pyproject.toml` exists, preserving the bot lane's pytest hook.
- Renderer tests and `cliprender PLAN.json --root ROOT` once
  `render/pyproject.toml` exists. A temporary plan adapted from the canonical
  one-clip example trims and joins two ranges from generated five-second media
  into a three-second output, with captions disabled and silence trimming off.
  Checks require 640x360, 90 decoded frames, exactly one H.264 video stream and
  one AAC audio stream, and full decode success.

Duration checks inspect container **and both media streams**, allowing 0.1s
for frame boundaries and AAC padding; a subtitle track cannot mask short media.
Each run uses a new temporary directory, including spaces in its paths.
Generated media and temporary plans are removed after success or failure.

Before either lane's project lands, that lane explicitly prints `SKIPPED`.
An environment smoke passing does **not** complete renderer acceptance.
The actual bot-request-to-rendered-demo check remains `veb-t2b.4`; captions,
content order and video-only edge cases also belong to the renderer suite.

Run an individual check with `uv run --locked scripts/check.py ffmpeg` (or
`whitespace`, `contract`, `tests`, `assets`, `bot`, `render`). CI runs the whole
gate on Ubuntu 22.04 and Windows Server 2022 for PRs and pushes to main.
ARM builds are pinned for local use; CI does not claim ARM or macOS execution.
