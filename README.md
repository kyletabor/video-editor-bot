# video-editor-bot

A video editor bot, built live on stage by two humans and two AI coding agents
from different vendors, in one repo.

**How the agents coordinate:** they never talk to each other directly.
Code moves through **git** (one branch and one PR per task). Tasks move through
**beads** (`bd`), a shared task list that syncs through this same GitHub repo.
The rules the agents follow are in [AGENTS.md](AGENTS.md). This README explains
them for humans.

---

## 1. Setup (once per person)

### Get access

This repo is private. Kyle adds you as a collaborator, and you accept the invite
from your email or https://github.com/notifications. Use either SSH or HTTPS in
the clone instructions below; SSH keys are optional for HTTPS contributors.

### What you need

| Need | Check it works |
|------|----------------|
| git, with access to this GitHub repo | SSH: `ssh -T git@github.com`; HTTPS: `gh auth status` after HTTPS login below |
| GitHub CLI, logged in | `gh auth status` |
| beads 1.3.0 or newer | `bd version` |
| an AI coding agent | Claude Code, Codex, Cursor, Gemini CLI, Copilot… any of them |
| FFmpeg and ffprobe | `ffmpeg -version` and `ffprobe -version`; use both from the same build. Version policy is tracked in `veb-uv0`. |

### Install beads

| Your machine | Command |
|--------------|---------|
| Linux or macOS | `curl -fsSL https://raw.githubusercontent.com/gastownhall/beads/main/scripts/install.sh \| bash` |
| Windows (Intel / AMD) | In PowerShell: `irm https://raw.githubusercontent.com/gastownhall/beads/main/install.ps1 \| iex` (needs Git for Windows) |
| Windows on ARM (Snapdragon) | Do everything inside WSL (Ubuntu): the agent, GitHub authentication, and the clone. Then use the Linux line. The ARM build of beads for Windows lacks the built-in task database. |

If `bd version` then says "command not found" (on Windows: "is not recognized"),
add the folder the installer printed to your PATH and open a new terminal.
On Windows x64 the release installer uses `%LOCALAPPDATA%\Programs\bd` by default;
if it falls back to a Go install, use the Go binary directory it reports instead.

### Windows tools and PATH

Install [Git for Windows](https://gitforwindows.org/) and the
[GitHub CLI](https://cli.github.com/), then run the Windows Beads installer above.
For FFmpeg, choose a Windows build linked from the
[FFmpeg download page](https://ffmpeg.org/download.html#build-windows), such as
Gyan's release essentials ZIP. Extract it to a permanent location and locate
the `bin` directory containing **both** `ffmpeg.exe` and `ffprobe.exe`.

In Windows, search for **Edit environment variables for your account**. Under
**User variables**, edit **Path**, then add the Beads install directory and the
FFmpeg `bin` directory as separate entries. Add directories, not executable
filenames, and keep the existing entries. Open a new terminal (restart your
agent app too if it still sees the old PATH), then check PowerShell can find them:

```powershell
Get-Command git, gh, bd, ffmpeg, ffprobe | Select-Object Name, Source
```

If multiple versions are installed, `Get-Command bd -All` or `Get-Command ffmpeg -All`
shows which PATH entry wins. On Windows ARM using WSL, install and run these
tools inside the same WSL environment as the clone and agent.

**On Windows:** the built-in Windows PowerShell 5.1 can't run `cmd1 && cmd2`.
Run chained commands one at a time, or use PowerShell 7 or Git Bash.

### Join the project

Choose one clone method. With SSH already configured:

```bash
git ls-remote git@github.com:kyletabor/video-editor-bot.git
git clone git@github.com:kyletabor/video-editor-bot.git
cd video-editor-bot
```

For HTTPS, run these commands in PowerShell (or another shell):

```powershell
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git
gh auth status
git ls-remote https://github.com/kyletabor/video-editor-bot.git
git clone https://github.com/kyletabor/video-editor-bot.git
cd video-editor-bot
```

Before the first `bd bootstrap`, HTTPS contributors create
`.beads/config.local.yaml` in this fresh clone:

```powershell
@'
sync.remote: "git+https://github.com/kyletabor/video-editor-bot.git"
dolt.auto-push: false
'@ | Set-Content -Encoding ascii .beads/config.local.yaml
git check-ignore -v .beads/config.local.yaml
```

Keep the `git+https://` prefix: this is the Beads sync URL, separate from Git's
`origin`. The local file overrides `.beads/config.yaml` and is excluded by
`.gitignore`; the check must print a matching ignore rule. Do not put credentials
in either YAML file or change the tracked SSH setting for everyone. Keep
`dolt.auto-push` off; do not override it with `BD_DOLT_AUTO_PUSH=true`.

Your beads identity is your git name (`git config user.name`). It's stamped on
every claim and comment, so make sure it's yours.

For either clone method, connect to the **existing shared task database**:

```bash
git config user.name
git config beads.role maintainer
bd bootstrap --dry-run
```

The preview must select the existing remote task data (`refs/dolt/data`), not
create an empty database. If authentication fails or it proposes a fresh empty
database, stop and fix access/configuration. Never run `bd init` in this repo.
Once the preview is correct:

```bash
bd bootstrap
git diff -- .beads/config.yaml
```

Beads 1.3.0 bootstrap can copy the effective HTTPS URL into tracked
`.beads/config.yaml`. In this fresh clone, if the diff shows only that generated
remote-setting change, restore the tracked file; the local override remains:

```bash
git restore -- .beads/config.yaml
bd dolt pull
bd list
```

If the diff includes other edits, preserve them and undo only bootstrap's remote
change. The task list must contain the existing `veb-*` issues and match the team.
This bootstrap behavior is documented in the
[Beads 1.3.0 implementation](https://github.com/gastownhall/beads/blob/v1.3.0/cmd/bd/bootstrap.go).

Already onboarded or using a Git worktree? Run `bd where` to find the active
`.beads` directory and reuse that database with `bd dolt pull`. Worktrees share
the primary clone's Beads state; do not initialize a second database or overwrite
an existing local config. The HTTPS recipe above is for a fresh clone: editing
YAML alone may not change a remote already stored in an existing Dolt database.

### Verify your setup

After onboarding, run these commands from the repository root:

```bash
bd version
bd ready --label lane:render
ffmpeg -version
ffprobe -version
```

The version commands confirm the tools are available, and `bd ready` lists
unblocked project tasks (Kyle uses `--label lane:bot`). An empty ready list can
mean all tasks in your lane are claimed or blocked; use `bd list` and `bd blocked`
to distinguish that from a setup failure.

Before opening a PR, also run the repository's `make check` gate. The current
gate needs [uv](https://docs.astral.sh/uv/getting-started/installation/), GNU Make,
and a POSIX shell. Git for Windows supplies Git Bash, but not Make: install a
[Windows GNU Make build](https://github.com/mbuilov/gnumake-windows), add its
directory to PATH (name the downloaded executable `make.exe`), and run
`make check SHELL=sh` from Git Bash. The portable
Windows check entry point and FFmpeg requirements are being completed in
`veb-uv0`; the version checks above alone do not replace the PR gate.

### Connect your agent

- **Claude Code:** reads AGENTS.md automatically (through CLAUDE.md). Optional:
  `bd setup claude --global` so every session starts with the task context loaded.
- **Codex or Cursor:** the beads hooks are already committed in this repo.
- **Anything else:** `bd setup --list`, then `bd setup <tool>`. If that adds files
  to the repo, commit them in a small PR.

Then paste this into your agent (Kyle's lane is `lane:bot`, Ramsey's is `lane:render`):

> You're working in video-editor-bot. Your lane is `lane:<yours>`. Read AGENTS.md
> and follow it exactly. Start with `bd prime`, then sync and tell me what's ready
> in your lane.

---

## 2. How beads works here (60 seconds)

- Each clone keeps its own copy of the task list: a small database in `.beads/`
  that git doesn't track.
- The copies sync through this GitHub repo, on a hidden ref (`refs/dolt/data`)
  that sits apart from the code branches. Beads also keeps a branch called
  `__dolt_remote_info__` on GitHub. Leave it alone.
- Syncing is manual, like git. `bd dolt pull` gets the other side's changes and
  `bd dolt push` publishes yours. If a push is rejected, pull first, then push again.
- **Beads doesn't lock anything.** We tested it: when both agents claimed the same
  task, the *later* claim silently won on the next sync, and the first agent got
  no error. That's why we use lanes.

---

## 3. The collaboration protocol

### Lanes: who owns what

| Lane label | Owned by | Folder it may edit |
|------------|----------|--------------------|
| `lane:bot` | Kyle's agent | `bot/` (user request → edit plan) |
| `lane:render` | Ramsey's agent | `render/` (edit plan → ffmpeg → video file) |
| `lane:shared` | nobody until a human hands it out | `contract/`, `assets/`, files at the repo root |

- Every task starts with exactly one lane label. Agents only pick work from their own lane.
- To hand a shared task to an agent, add that agent's lane label to it:
  `bd label add <id> lane:render`.
- `contract/` is the one piece of code both lanes share: the edit-plan format.
  Changing it takes a shared task and a PR the other side reviews.
- Each lane keeps its own dependency file inside its own folder, so nobody
  fights over a shared package file.

### The loop: what each agent does for every task

```
sync → pick from own lane → claim + push → branch → build → PR
     → other agent reviews → author merges → close task + push
```

The exact commands are in AGENTS.md, section 2. The step that matters most:
**claim and push the claim before writing any code.** A claim only counts once
it's pushed.

### Talking to each other

- To reach the other agent, comment on a task in **its** lane
  (`bd comments add <their-task-id> "..."`), or create a new task in its lane.
  Each agent's sync reads the comments on its own lane's open tasks.
- PRs waiting for review show up in `gh pr list --search "-author:@me"`, which the
  sync also checks.
- Humans: say **"sync"** to your agent any time. It pulls, then tells you in three
  lines what the other agent is doing, what's next, and anything addressed to it.

### Hard rules

- Never push to `main`. GitHub can't block it on a free private repo, so this
  rule is the only guard.
- Never edit the other lane's folder, and never resolve a conflict in their files.
- Never run `bd init` in a clone, turn on `dolt.auto-push`, or `git push --mirror`
  (that last one deletes the shared task list).

---

## 4. Live session runbook

### Before the talk: pre-build

Both lanes stay blocked until the shared groundwork lands (`bd blocked` shows the
chain). Finish these first:

1. `veb-de2`: Kyle and Ramsey pick the language/runtime, then close the task.
2. `veb-p12`: the edit-plan contract (schema plus two examples).
3. `veb-uv0`: `make check` and CI, with one pinned ffmpeg version.
4. `veb-0ct`: two or three short sample clips.
5. Split `veb-0rh` (bot) and `veb-2rq` (render) into child tasks of 30 minutes or less
   (`bd create --parent veb-2rq ...`; children inherit the lane label). Then take the
   lane label off each parent (`bd label remove veb-2rq lane:render`) so agents pick
   the small tasks, not the whole feature.

### Pre-flight (10 minutes before going on)

1. Both: `gh auth status`, `bd version`, then `git ls-remote origin` from the clone.
   SSH users also run `ssh -T git@github.com`; HTTPS users follow the local override
   setup above.
2. Both: `git pull`, `bd dolt pull`, `bd list`. The two screens must match.
3. Round trip: Kyle's agent comments on any open `lane:render` task
   (`bd list --label lane:render`), for example
   `bd comments add <id> "preflight ping from Kyle's agent"`, then runs `bd dolt pull`
   and `bd dolt push`. Ramsey says "sync", and his agent must report the ping. Then do
   the same the other way, on a `lane:bot` task.
4. Ask each agent: "What's your lane, and what's the loop?" It should answer from AGENTS.md.
5. Mark a known-good point: `git tag demo-start`, then `git push origin demo-start`.

### If something goes wrong

| What you see | What to do |
|--------------|------------|
| `bd dolt push` rejected ("tip of your current branch is behind") | `bd dolt pull`, then `bd dolt push` |
| `issue already claimed by …` | It's taken. Pick another task. |
| A pull prints `auto-merged issue <id>; assignee …` | Two claims collided. The agent that saw it doesn't push, and the humans pick who keeps the task. **If that agent keeps it,** it runs `bd dolt push` and the other agent drops the task at its next sync. **If the other agent keeps it,** the agent that saw the notice runs `bd update <id> --assignee "<other person's git name>"`, then `bd dolt pull` and `bd dolt push`. |
| `bd ready --label lane:<x>` is empty | `bd blocked` shows what's in the way. Usually a shared task needs finishing or handing out. |
| A PR has a merge conflict | Its author rebases on `origin/main` and fixes only their own files. |
| An agent went sideways or crashed | Close its PR. On that person's machine, run `bd unclaim <id>`, then `bd dolt pull` and `bd dolt push`, and start the task over. |
| Beads sync is broken | Keep coding. Coordinate in PR comments and fix sync after the talk. |

---

## 5. Cheat sheet

```bash
bd dolt pull                     # get the other side's task changes
bd dolt push                     # publish yours (pull first, except right after a claim: see AGENTS.md §2)
bd ready --label lane:render     # what you can pick up (use your own lane)
bd blocked                       # what's stuck, and on what
bd list --status=in_progress     # what's being worked on right now
bd show <id>                     # details, assignee, dependencies
bd comments <id>                 # read the conversation on a task
bd comments add <id> "text"      # add to it
bd create --title "..." --labels lane:bot --description "..."   # new task in a lane
bd label add <id> lane:render    # hand a shared task to an agent
bd unclaim <id>                  # give back a task you hold
gh pr list --search "-author:@me"   # PRs waiting for your agent's review
gh pr view <n>                   # a PR's reviewers and approvals
```

Running two agents on one machine? Give each its own identity first:
`export BEADS_ACTOR=<name>` (in PowerShell: `$env:BEADS_ACTOR = "<name>"`). They share
one GitHub login, so they can comment on each other's PRs but can't approve them.
