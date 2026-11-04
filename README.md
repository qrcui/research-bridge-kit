# research-bridge-kit 3.2.0

English | [中文](README.zh-CN.md)

A reusable kit for a human-supervised Claude Code × Codex multi-agent research system (protocol V3.0).

Deploy it into any local project, then say "start research" in a chat:

- **Claude** designs experiments and reviews results;
- **Codex** writes code, runs experiments and commits to Git;
- they hand work over through files in the project's `.bridge/` folder, and each side starts within seconds of the other finishing;
- idle time costs no tokens;
- you can pause, give input or stop at any time from the chat.

Architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## What's inside

```
research-bridge-kit/
├── README.md / README.zh-CN.md     this guide (English / Chinese)
├── deploy.py                       deploy / upgrade / self-check (Python 3.8+, standard library only)
├── install_skill.cmd               installs the skill into local Claude Code (double-click)
├── skill/research-bridge/          the skill (SKILL.md)
├── configs/                        per-project deployment configs (JSON, kept out of Git)
├── docs/                           architecture specification (English / Chinese)
└── template/                       everything deployed into a project
    ├── .bridge/                    protocol, two helpers, tests, watcher, prompts, live view, subagent rules, R0000 template
    ├── .claude/agents/             4 read-only research subagents (generalization, statistics, leakage audit, critique)
    ├── GIT_PROVENANCE.md           Git provenance policy (an existing file of the same name is kept)
    └── HANDOFF.md                  handoff template (an existing HANDOFF is kept)
```

Files read by agents (protocol, prompts, subagent rules, skill) are in English; documents for people (README, architecture, quick reference) are bilingual.

## New project (recommended: all through chat)

1. On GitHub click **Code → Download ZIP**, extract it, rename the folder to `research-bridge-kit` and put it in `C:\Users\<you>\Documents\`.
2. Double-click `install_skill.cmd` to install the skill into local Claude Code. Once is enough.
3. Open the Claude desktop app, go to the **Code** tab, choose **Local**, select your new project folder and say: **"Deploy the multi-agent research system to this project."**
4. It inspects the project read-only, then asks a few questions at once: research goal, how results are judged, which files must not change, data rules, deadline, round budget, which Python to use.
5. Then it:
   - deploys;
   - reviews the Git baseline with you;
   - checks that both CLIs are installed and logged in;
   - runs the self-check;
   - shows you how to operate the system.
6. After deployment the system is **IDLE** and does nothing on its own. When ready, say "start research" in the Operator chat.

## Without chat (manual)

```powershell
python deploy.py --example-config --out configs\myproject.json   # config template; fill it in
python deploy.py --config configs\myproject.json --dry-run       # show the plan, write nothing
python deploy.py --config configs\myproject.json                 # deploy
python deploy.py --verify --project D:\path\to\project           # self-check
```

## After deployment (same for every project)

| To do | How |
|---|---|
| Run the watcher (keep it open) | double-click `.bridge\watcher\start_watcher.cmd` |
| Talk to the system | desktop app Code tab → Local → project folder; first message: "You are the Operator of this project. Read `.bridge/watcher/OPERATOR_PROMPT.md` completely, follow it, then tell me the current state." |
| Start / pause / resume / end / give input / check progress | just say it in the Operator chat |
| Watch the agents live | double-click `.bridge\watcher\live_view.cmd`, or ask the Operator to "open the live view": every message, reasoning step, command and output of both agents (including Codex subagents) in your browser. See [Live view](#live-view) |
| CLIs missing or not logged in | double-click `.bridge\watcher\install_clis.cmd` |
| Self-check | ask the Operator to run `bridgectl check` |

## Live view

A read-only page at `http://127.0.0.1:8765/` (the next free port if that one is taken) that shows what both agents are doing as it happens.

- **Always on:** the viewer (`.bridge/watcher/live_view.py`, Python standard library) runs in the background. The watcher starts it, checks it every 30 s and restarts it if it stopped. `live_view.cmd` / `bridgectl view` only open the page (and start the viewer if the watcher is not running).
- **No restarts needed:** the page reconnects by itself, and after a viewer restart it resyncs automatically. New Claude runs, new Codex sessions and Codex subagents appear without reloading; a Codex session that keeps working after its subagent finished stays followed.
- **Stays fast on long runs:** the viewer keeps the latest ~5000 events, the page the latest ~2000 rows; long command output is loaded only when you open its row. The complete output always remains in the agent logs.
- **Safe:** listens on 127.0.0.1 only, reads agent files without writing them, and never touches bridge state.
- **Settings:** `"live_view": {"enabled": true, "port": 8765}` in `.bridge\watcher\watcher_config.json` (restart the watcher after editing). Its own log is `.bridge\watcher\logs\live_view.log`.
- **Other commands:** `<project python> .bridge/watcher/live_view.py status | stop | open`. The viewer itself also runs on Linux and macOS (`python3 .bridge/watcher/live_view.py open`).

## Upgrading an existing deployment

```powershell
python deploy.py --upgrade-code --project "D:\path\to\project"
```

- Replaces code files only: the two helpers, tests, watcher, bridgectl and live-view files.
- Replaced files are backed up to `.bridge\deployment_backups\` first.
- Documents, prompts, configs, constraints and state are never touched.
- Refuses while a task is running.
- Restart the watcher window afterwards. Upgrading from 3.1: close the old live-view window if one is open; the restarted watcher starts the new background viewer by itself.

## Requirements

- Windows 10/11, PowerShell 5.1 or newer.
- The project's Python must be **3.12 or newer** (needed by the helpers); `deploy.py` itself needs only 3.8+.
- Claude Code CLI and Codex CLI installed and logged in.
- Git: optional, recommended.

## Safe defaults

- **Deploying is not starting:** it runs only after you explicitly say "start research". Rebooting, opening the watcher or an old task signal never start it.
- **Nothing is overwritten:** existing project files with the same names are kept; if `.bridge/` already exists, deployment refuses.
- **Protected paths:** no agent may change the protected paths listed in `PROJECT_CONSTRAINTS.md` without your authorization.
- **Permission prompts are bypassed:** both agents run unattended, so permission prompts are skipped by default — otherwise they would stall. Their limits come from `PROTOCOL.md` and `PROJECT_CONSTRAINTS.md`. To tighten this, edit the arguments in `.bridge\watcher\watcher_config.json`.
- **Nothing leaves your machine:** no archives, uploads, pushes or public repositories unless you ask.

## Versions

- 3.2.0: live view rewritten in Python: kept running by the watcher, page reconnects and resyncs by itself, follows every live Codex session including subagents, bounded memory, long output loaded on demand, localhost-only Host check; `bridgectl check` and `deploy.py --verify` also run its tests.
- 3.1.0: read-only live view (`live_view.cmd` / `bridgectl view`) and `bridgectl check`; bilingual documentation.
- 3.0.0: first generic release, extracted from a working V3.0 deployment.

## License

MIT, see [LICENSE](LICENSE).
