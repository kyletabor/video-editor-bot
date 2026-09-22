# video-editor-bot

A video editor bot, built live on stage by two humans and two AI coding agents
(different vendors) working in one repo.

The agents never talk to each other directly. They coordinate through two things
that every agent already understands:

- **git** — code, branches, PRs, reviews
- **beads (`bd`)** — the shared task list, synced through this same GitHub repo

Read [AGENTS.md](AGENTS.md) for the house rules. This file is how to get set up.

## Prerequisites

| Tool | Why | Install |
|------|-----|---------|
| git + GitHub access | code | you have it |
| `bd` ≥ 1.3.0 | shared task list | `brew install beads` **or** `curl -fsSL https://raw.githubusercontent.com/gastownhall/beads/main/scripts/install.sh \| bash` |
| `ffmpeg` | rendering | `brew install ffmpeg` / `apt install ffmpeg` |
| an AI coding agent | the point | Claude Code, Codex, Cursor, Gemini CLI, Copilot, Aider… any of them |

Check: `bd version` should print `1.3.x`.

## Onboarding (humans) — do this once

```bash
git clone git@github.com:kyletabor/video-editor-bot.git
cd video-editor-bot
bd bootstrap          # pulls the shared task list from GitHub (refs/dolt/data) — NOT bd init
bd setup --list       # see which agent integrations exist
bd setup <your-tool>  # e.g. bd setup codex / cursor / gemini / copilot / aider
bd ready              # you should see the same issues Kyle sees
```

`bd bootstrap` is the clone-side command. Never run `bd init` here — the project
is already initialized and `init` would create a second, disconnected task list.

## Onboarding (agents) — paste this into your agent

> You're working in `video-editor-bot`. Read `AGENTS.md` and follow it exactly.
> Then run `bd prime`, `bd dolt pull`, and `bd ready`. Claim one issue with
> `bd update <id> --claim`, work on a branch named `<your-name>/<id>-<slug>`,
> and open a PR when `make check` is green. Never commit to `main`. Never edit
> a directory you don't own (ownership map is in `AGENTS.md`).

Claude Code, Codex, and Cursor pick up the beads context automatically via hooks
that `bd init`/`bd setup` installed. Other tools: run `bd prime` at session start.

## The daily rhythm

```
bd dolt pull  →  bd ready  →  bd update <id> --claim  →  branch  →  build  →  make check
   →  push + PR  →  the OTHER human's agent reviews  →  merge  →  bd close <id>  →  bd dolt push
```

Two agents, two directories, one contract. See ownership in `AGENTS.md`.

## Beads sync cheat sheet

```bash
bd dolt pull     # start of every session, and before every push
bd dolt push     # after you create/close/comment on issues
bd ready         # what's unblocked
bd blocked       # what's waiting on what
bd show <id>     # details + dependencies
bd comments add <id> "note"   # leave a note for the other agent on the issue itself
```

**Don'ts**
- Don't turn on `dolt.auto-push` — with two writers, racing auto-pushes can strand
  remote history. Manual `pull` then `push` is the rule.
- Don't `git push --mirror` — it deletes `refs/dolt/data` (the shared task list).
- Don't treat `.beads/issues.jsonl` as the source of truth. It's an export.

## Status

| Phase | State |
|-------|-------|
| Repo, beads, agent instructions | ✅ done (this) |
| Runtime/language decision | ⏳ `veb-de2` — Kyle + Ramsey |
| Edit-plan contract | ⏳ `veb-p12` |
| `make check` + CI | ⏳ `veb-uv0` |
| Sample clips | ⏳ `veb-0ct` |
| **Build live on stage** | render pipeline `veb-2rq`, bot surface `veb-0rh` |
