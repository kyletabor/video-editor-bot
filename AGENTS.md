# Agent Instructions — video-editor-bot

Two humans (Kyle, Ramsey), two AI agents from different vendors, one repo.
Agents don't talk to each other. They coordinate through **git** (code) and
**beads** (`bd`, the shared task list). These house rules are the contract.

## House rules

**Profile:** this repo opts into the **Team-maintainer** profile (see the beads
block below). You MAY commit to your own branch, push it, open PRs, close beads,
and run `bd dolt push`. You may NOT push to `main`.

### 1. Sync first, sync last
- Your identity is `git user.name` (or `$BEADS_ACTOR`). Claims and comments carry it — make sure it's yours, not the other human's.
- Session start: `bd dolt pull` then `bd ready`.
- After creating / closing / commenting on issues: `bd dolt pull` **then** `bd dolt push`.
- `dolt.auto-push` stays **off** (two writers race → stranded remote history).

### 2. Ownership — stay in your lane

| Directory   | Owner           | What lives there                                   |
|-------------|-----------------|----------------------------------------------------|
| `contract/` | shared          | edit-plan schema + examples. Change only via PR reviewed by the *other* side. |
| `render/`   | Ramsey's agent  | edit plan → ffmpeg → output file                    |
| `bot/`      | Kyle's agent    | user request → edit plan                            |
| `assets/`   | shared          | sample clips, read-only during the build            |

Need something changed in a directory you don't own? File a bead
(`bd create`), assign it (`bd update <id> --assignee=<name>`), and move on.
Don't edit it yourself.

### 3. One bead, one branch, one PR
- Claim before coding: `bd update <id> --claim`.
- Branch: `<your-name>/<bead-id>-<short-slug>` (e.g. `ramsey/veb-2rq-ffmpeg-runner`).
- Commits reference the bead id. Small commits, pushed often.
- PR title starts with the bead id. The **other human's agent** reviews — never self-review.
- `make check` must be green before you open the PR (once `veb-uv0` lands).
- After merge: `bd close <id>`, then `bd dolt push`.

### 4. Talk on the bead, not in files
Handoffs, questions, and "heads up, I changed X" go in
`bd comments add <id> "..."`. The other agent sees it on its next `bd dolt pull`.
No HANDOFF.md, no chat logs in the repo.

### 5. When stuck
Don't invent a workaround in someone else's directory. Post a comment on the
bead, mark it blocked if it truly is (`bd dep add`), and pick the next `bd ready` item.

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
