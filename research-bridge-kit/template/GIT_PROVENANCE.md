# Git version control and experiment provenance

Companion policy to `.bridge/PROTOCOL.md` (V3.0) for **{{PROJECT_NAME}}**. Human instructions and
`.bridge/PROJECT_CONSTRAINTS.md` take priority. Git is local version control; agents communicate through
`.bridge/`, never through commits or commit messages.

## Local repository

- Repository: `{{PROJECT_ROOT}}`, branch `main` (or the branch that existed before deployment).
- The baseline commit (`chore: snapshot existing project before multi-agent research`) is made once,
  after a human-reviewed `git status`, before the first formal task. Find it with `git log --reverse`.
- No remote, push, extra branch or worktree is created by the system.
- `.bridge/` runtime files, raw data, weights, caches, environments and secrets are ignored.
  **Ignored data and weights are not protected by Git.** Keep their own backups; record their hashes
  and generation commands in experiment records; never force-add ignored material.

## Dispatcher boundary

Phase A (the watcher's probes) performs no Git operation. Git work starts only after a valid formal
claim in Phase B. R0000 is a read-only communication test: Git may be inspected, nothing is committed.

## Per-task procedure, R0001 onward

1. Before editing, record repository root, branch, full HEAD (Base Commit SHA) and
   `git status --porcelain=v1 --untracked-files=all`; inspect the staged diff separately. Preserve all
   pre-existing changes and record their paths/owners. A dirty file is not this task's merely because
   it sits in a task-related directory.
2. Scope the task's Codex-owned file list explicitly. Never edit a file another agent is editing;
   concurrent changes to one file need a conflict report, not an overwrite.
3. Before each experiment record HEAD, branch, dirty status, exact command, seeds, environment,
   data/model hashes and the SHA256 of every relevant code/config file (including imports and
   untracked implementation). Save a scoped diff and copies of relevant new code under
   `experiments/<task_id>/provenance/` (no secrets, no other agents' private work). Re-hash after the
   run; unexpected concurrent changes make attribution uncertain and must be reported.
4. Implement and verify. Experiments may run before the result commit; report that ordering and the
   dirty snapshot explicitly. A bare HEAD does not represent dirty code.
5. Confirm the index is empty or holds only this task's verified files. If another owner's content is
   staged, leave it intact, report the conflict and defer the commit.
6. Stage only explicit verified paths (`git add --dry-run -- <paths>`, then `git add -- <paths>`), then
   inspect `git diff --cached --name-status` and the staged diff. Never `git add .`, `git add -A` or
   force-add. HANDOFF belongs to Claude; Operator controls and all `.bridge/` files stay excluded.
   The engineering-lock owner serializes the whole stage/review/commit sequence; subagents never stage.
7. Recheck HEAD, branch, status and hashes right before committing; investigate unexpected changes,
   never reset to make a check pass. Commit with `[R####]` in the subject using the existing identity,
   e.g. `fix(eval): correct metric aggregation [R0002]`.
8. Read back `git rev-parse HEAD`, inspect `git show --stat --oneline HEAD`, compare committed hashes
   with the run snapshot, and record whether tests/experiments ran before or after the commit.
9. Add the Git Provenance section below to the result and publish through `dispatcher.py finish`.

No task-owned change → no empty commit; record the existing HEAD. Failed or unverified code is never
committed as accepted; a necessary partial-work commit says incomplete. Never fabricate a SHA or an
author identity; if Git fails or permission is missing, report it and its reproducibility risk.

## Mandatory result section

```markdown
## Git Provenance

- Repository Root: <actual root, or explicit unavailability reason>
- Branch: <actual branch>
- Base Commit SHA: <full SHA recorded before task edits>
- Result Commit SHA: <full verified SHA; same as base if no commit, or explain>
- Modified Files: <explicit task-owned paths and committed changes>
- Uncommitted Changes: <remaining staged/unstaged/untracked changes, owners and impact, or none>
- Experiment Timing: <before/after result commit, no experiment run, or per-run details>

Run-code snapshot, hashes and diff: <paths; distinguish executed dirty code from later commits>.
Tests/acceptance: <commands and results for the code actually committed>.
```

`dispatcher.py finish` checks that every label has a nonempty value; the worker is accountable for the
facts. A blocked preflight may state "not inspected / no experiment run". Historical results are
immutable; never backfill misleading provenance.

## Preservation and rollback

No remote, push, worktree, extra branch, amend, rebase, history rewrite, destructive
checkout/reset/clean, stash, or clearing another agent's changes. A performance regression is evidence
to report, not an instruction to roll back; a rollback is a new, auditable task decision.
