---
name: research-bridge
description: Deploy or operate the Claude Code × Codex multi-agent research system (file bridge + event-driven watcher) in a project - set up a new project, start/pause/end research, check progress, upgrade.
---

# Research bridge (Claude Code × Codex, protocol V3.0)

A human-supervised research pipeline for one local Windows project:

- **Human** decides goal, budget, start/pause/end — by talking to an **Operator** chat.
- **Claude Research Lead** (`claude -p`) designs hypotheses and tasks and reviews results.
- **Codex Engineering Lead** (`codex exec`) implements, runs experiments, commits, reports.
- They communicate only through files in `<project>/.bridge/` (immutable `tasks/R####.md` and
  `results/R####.md`, small JSON signals, a human-owned `operator/CONTROL.json`).
- **`bridge_watcher.ps1`** (one PowerShell FileSystemWatcher window) runs zero-token Python probes on
  every bridge file change and launches the right agent within seconds; idle costs nothing.
- Project-specific rules live in `.bridge/PROJECT_CONSTRAINTS.md`; the protocol is `.bridge/PROTOCOL.md`.

All code and templates come from **research-bridge-kit** (`deploy.py`, `template/`). Never rebuild the
system from memory: if the kit cannot be found, say so and ask the user for it.

Talk to the user in their language, plainly and briefly. They prefer chat over typing commands: run
commands yourself and explain results; only hand them a `.cmd` to double-click when you cannot run
something yourself.

## 1. Find out where you are running

- **Claude Code on the user's Windows machine** (desktop app Code tab with a Local session, or the
  CLI): you can run PowerShell/Python/git directly. Best place to deploy.
- **A cloud chat linked to the user's computer** (tools named `mcp__remote-devices__*`): the shell
  (`device_bash`) is a Linux VM that sees only connected folders under `$HOME/mnt/<name>`. You can run
  `deploy.py` there with `python3` and `--target $HOME/mnt/<project>`, but not the project's Windows
  Python, git init, the watcher or bridgectl — ask the user to double-click the `.cmd` files named
  below, or suggest continuing in the desktop Code tab. Request access to both the kit folder and the
  project folder (`device_request_folder_access`) in one request if they are not connected.
- **No access to the computer**: explain that deployment needs the Claude desktop app (Code tab,
  Local) on that computer, and stop.

## 2. Locate the kit

Try in order and use the first folder that contains `deploy.py` and `template/`:
1. a path the user gives;
2. the path in `KIT_PATH.txt` next to this SKILL.md (written by the kit's `install_skill.cmd`);
3. `%USERPROFILE%\Documents\research-bridge-kit`, `%USERPROFILE%\research-bridge-kit`,
   `%USERPROFILE%\Downloads\research-bridge-kit*` (a folder, or a zip you extract next to it).
Otherwise ask the user to download/extract `research-bridge-kit` and tell you where it is.

## 3. Deploy to a project

Deployment is NOT a start. It leaves the system IDLE; nothing runs until the user explicitly says to
start research. Never overwrite or move existing project files.

1. **Confirm the project folder.** If `<project>/.bridge/` already exists, this is an existing
   deployment: go to section 5 (upgrade) or section 4 (operate) instead.
2. **Inspect read-only** (no edits): top-level layout, README/HANDOFF/task descriptions, data and
   weight files with sizes, notebooks, `git status` if it is a repository, Python environments
   (venvs, conda) and their versions. The project Python must be 3.12 or newer
   (`<python> --version`).
3. **Collect the configuration** in one short round of questions (AskUserQuestion when available),
   proposing defaults from what you found instead of asking open questions:
   - `research_goal`, `success_criteria`, `evaluation_protocol` (how generalization is judged);
   - `protected_paths` (relative paths: data, weights, submission/official files you found);
   - `data_rules`, `other_constraints` (allowed data/models, size or parameter limits, anything the
     task description forbids), `compute`, `deadline`;
   - `max_research_rounds` (default 8), `python` (absolute path);
   - models: `claude_model` (default `opus`), `codex_model` (empty = Codex CLI default),
     `codex_reasoning_effort` (default `high`);
   - `git_init_if_missing` (default true).
   Use the user's own words for goals and rules. Save the config as
   `<kit>/configs/<project-name>.json` (start from `python deploy.py --example-config --out <file>`).
4. **Dry run, then deploy**: `python <kit>/deploy.py --config <cfg> --dry-run`, summarize the plan
   (files created, existing files preserved, .gitignore change, git init), then run without
   `--dry-run`. In the cloud-linked case add `--target $HOME/mnt/<project>`. Any ERROR stops you:
   report it, do not hand-edit around it.
5. **Git baseline** (Windows only): if the repository is new or has no commits, show the user a
   summary of `git status --porcelain=v1 --untracked-files=all`, flag large files (>50 MB), data,
   weights and secrets and propose `.gitignore` additions, then — with their OK — commit only the
   reviewed files: `chore: snapshot existing project before multi-agent research`. Never push, add a
   remote, or force-add ignored files. If the repository already has history, change nothing.
6. **CLIs**: `claude` and `codex` must be installed and logged in. If `where.exe claude codex` misses
   one, have the user double-click `<project>\.bridge\watcher\install_clis.cmd` (installs from
   official sources and walks through login). Quick check: `claude -p "reply OK"` and
   `codex exec --skip-git-repo-check "reply OK"`.
7. **Verify**: `python <kit>/deploy.py --verify --project <project>` (or
   `.bridge\watcher\bridgectl check`): Python 3.12+, unit tests, both probes, CLIs.
8. **Hand over** briefly:
   - keep one window open: double-click `.bridge\watcher\start_watcher.cmd`;
   - talk to the system in the desktop app: Code tab → Local → this project folder, first message
     "You are the Operator of this project. Read `.bridge/watcher/OPERATOR_PROMPT.md` completely, follow
     it, then tell me the current state." (give it to the user in their language), or double-click
     `.bridge\watcher\operator.cmd`;
   - say "start research" there (in their language) when ready. To watch the agents work, double-click
     `.bridge\watcher\live_view.cmd` or ask the Operator to open the live view. The first run publishes the R0000 communication test, then
     formal rounds R0001… follow automatically until the budget, the goal, or NEEDS_USER.
   If HANDOFF.md was created from the template, the Research Lead fills it from the real project
   before designing R0001; an existing HANDOFF was preserved.

## 4. Operate an existing deployment

When the user, inside a project that has `.bridge/watcher/`, wants to start, pause, resume, end,
check progress, file a research idea, or understand what an agent did: read
`<project>/.bridge/watcher/OPERATOR_PROMPT.md` completely and act as that Operator. In short:
`powershell -NoProfile -ExecutionPolicy Bypass -File .bridge/watcher/bridgectl.ps1 <status|check|start|pause|pause-after|resume|end|request "...">`;
state-changing commands only on the user's explicit words; requests record the user's own words;
report "registered" vs "executed" separately; never edit tasks, results, state, code or CONTROL.json
directly. Older deployments may lack `bridgectl check`; suggest the upgrade in section 5.

## 5. Upgrade an existing deployment

`python <kit>/deploy.py --upgrade-code --project <project>` replaces only code files (helpers, tests,
watcher/bridgectl scripts) with backups under `.bridge/deployment_backups/`; it refuses while a lock
or agent is live. Documents, prompts, configs, constraints and state are never touched. Afterwards
the user restarts the watcher window. Run `bridgectl check` to confirm.

## Hard rules

- Deployment, installing CLIs, opening the watcher or a restart never authorize a start.
- Never overwrite, move or delete existing project files; never edit another role's bridge files.
- Never set CONTROL to ACTIVE except through `bridgectl start` on the user's explicit instruction.
- The agents run unattended with permission prompts bypassed (`bypassPermissions`,
  `--dangerously-bypass-approvals-and-sandbox`) because a prompt would stall them; their limits come
  from PROTOCOL.md and PROJECT_CONSTRAINTS.md. Tell the user this once during deployment; to tighten
  it, edit `agents.*.args` in `.bridge/watcher/watcher_config.json`.
- No zip, upload, push or public repository unless the user asks.
- Report only what files and command output show; never claim a step happened that did not.
