# Agent Instructions — video-editor-bot

Two humans (Kyle, Ramsey), two AI coding agents from different vendors, one repo.
You never talk to the other agent directly. You coordinate through **git** (code:
one branch and one PR per task) and **beads** (`bd`: the shared task list, synced
through this GitHub repo). These rules are the contract. README.md explains the
same rules for humans.

**Profile:** this repo opts into the **Team-maintainer** profile described in the
beads block below. You may commit to your own branch, push it, open and merge PRs
as the loop describes, close tasks, and run `bd dolt push`. Never push to `main`.

## 1. Your lane

| Lane label    | Agent                                      | You may edit                         |
|---------------|--------------------------------------------|--------------------------------------|
| `lane:bot`    | Kyle's                                     | `bot/`                               |
| `lane:render` | Ramsey's                                   | `render/`                            |
| `lane:shared` | nobody, until a human adds your lane label | `contract/`, `assets/`, root files   |

- If you don't know your lane, ask your human before doing anything else.
- Pick work only with `bd ready --label lane:<yours>`. Never pick from a bare `bd ready`.
- Every task you create gets exactly one lane label: `bd create ... --labels lane:<x>`.
- A human hands you a shared task by adding your lane label to it. Only then may you
  edit shared files, and only for that task. Keep that PR tiny and get it merged first.

## 2. The loop: every task, in this order

1. **Sync:** `git fetch origin && bd dolt pull`
2. **Pick:** `bd ready --label lane:<yours>`
3. **Claim and publish in one go, before writing any code:**
   `bd update <id> --claim && bd dolt push`
   - `issue already claimed by …` → it's taken. Pick another.
   - Push rejected (`tip of your current branch is behind`) → `bd dolt pull`, then `bd dolt push`.
     If that pull prints `auto-merged issue <id>; assignee …` for the task you just
     claimed, two claims collided. **Stop and tell your human.** Don't start the work.
4. **Branch:** `git switch -c <lane>/<id>-<slug> origin/main` (e.g. `render/veb-2rq-ffmpeg-runner`)
5. **Build** only in your lane's folder. Commit messages start with the task id.
6. **Open the PR:** `git push -u origin HEAD`, then
   `gh pr create --title "[<id>] <summary>" --body "Task <id>"`, then
   `bd comments add <id> "PR #<n> ready for review"` and `bd dolt pull && bd dolt push`.
7. **Review** (done by the *other* lane's agent): `gh pr diff <n>`, then
   `gh pr review <n> --approve` or `gh pr review <n> --request-changes --body "<why>"`.
   Never approve your own PR.
8. **Merge** (the author, after approval): `gh pr merge <n> --squash --delete-branch`
9. **Close:** `bd close <id> --reason "PR #<n>"`, then `bd dolt pull && bd dolt push`

## 3. Beads rules

- **Beads does not lock.** If two agents claim the same task, the *later* claim silently
  wins at the next sync and the first claimer gets no error. Lanes prevent this, so stay in yours.
- At every sync, confirm your in-progress task is still yours: `bd show <id>` must list
  you as `Assignee`. If it doesn't, stop and tell your human.
- Publish after every change: a claim, comment, create, or close is always followed by
  `bd dolt pull && bd dolt push`.
- Talk on the task, not in files: `bd comments add <id> "…"`. No HANDOFF.md, no TODO lists.
- Need something from the other lane? Create a task in *their* lane with a clear ask and
  don't edit their files. If you're blocked on it: `bd dep add <your-id> <their-id>`.
- Never: `bd init` in a clone (fresh clones use `bd bootstrap`), turning on `dolt.auto-push`,
  `git push --mirror` (it deletes the shared task list), or editing `.beads/issues.jsonl` by hand.

## 4. Code rules

- `contract/` is the only code the lanes share: the edit-plan format. Build against it and
  test against its examples. Changing it takes a `lane:shared` task and a PR the other lane reviews.
- Each lane keeps its own dependency file inside its own folder. No shared root package file.
- Rebase often: `git fetch origin && git rebase origin/main`. Resolve conflicts only in your
  own files. If a conflict touches the other lane, stop and comment on the task.
- `main` has no branch protection (free private repo), so nothing technical stops a push to it. Don't.
- Once `make check` exists (task veb-uv0), it must pass before you open a PR.

## 5. Session start, and "sync"

- **Session start:** run `bd prime`, then sync.
- **When your human says "sync":** run `git fetch origin && bd dolt pull`, then
  `bd list --status=in_progress`, `bd ready --label lane:<yours>`, and `bd comments <id>` for
  your in-progress task. Answer in three lines: what the other agent is doing, what's next
  for you, and anything addressed to you.

---

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:46cd31e7 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->
## Beads Issue Tracker

Use Beads (`bd`) for durable task tracking in repositories that include it. Use the `beads` skill at `.agents/skills/beads/SKILL.md` (project install) or `~/.agents/skills/beads/SKILL.md` (global install) for Beads workflow guidance, then use the `bd` CLI for issue operations.

### Quick Reference

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
bd prime                # Refresh Beads context
```

### Rules

- Use `bd` for all task tracking; do not create markdown TODO lists.
- Run `bd prime` when Beads context is missing or stale. Codex 0.129.0+ can load Beads context automatically through native hooks; use `/hooks` to inspect or toggle them.
- Keep persistent project memory in Beads via `bd remember`; do not create ad hoc memory files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.
<!-- END BEADS CODEX SETUP -->
