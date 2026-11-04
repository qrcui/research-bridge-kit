# Human-Supervised Multi-Agent Research System
## V3.0 Unified Architecture Specification

[中文版](ARCHITECTURE.zh-CN.md)

**Document type:** system architecture and operating protocol  
**Scope:** one local research project  
**Environment:** Windows desktop, shared local project folder  
**Goal:** let several AI agents with distinct responsibilities carry out research design, implementation, experiments, result review and multi-round iteration on their own, while the human keeps the highest authority.

---

# 1. Overview

The system is a multi-agent research architecture built on shared local files, a task state machine and event-driven dispatch (file watching).

Its basic loop:

**Human defines the goal → research planning → implementation → experiments → result review → next round of planning.**

After one setup it can run many research rounds without the user copying tasks, results or instructions between AI chats.

Design principles:

1. **Human final control:** goals, running time, resource budget, pause, stop and major changes of direction are decided by the human.
2. **Research separated from engineering:** research design and scientific judgment belong to the research unit; code and experiments belong to the engineering unit.
3. **Autonomous task decomposition:** a lead unit may create and manage temporary subagents according to task complexity.
4. **Lightweight dispatch:** with no new work, only a minimal state check runs; no full context is loaded.
5. **File-driven communication:** units exchange instructions, results and durable state through well-defined files, never through each other's chat history.
6. **Auditable and reproducible:** tasks, artifacts, metrics and code versions are traceable.
7. **Explicit lifecycle:** after setup the system does not run. Automatic dispatch starts only on the human's start command and stops on the end command.
8. **Protect existing assets:** deploying the system never rebuilds the project, overwrites training data, deletes past experiments or damages existing models.

This specification describes the target architecture. Whether a deployment actually works must be shown by real files, dispatcher state and end-to-end tests, never by an agent's claim alone.

# 2. Roles and responsibilities

The system has three logical layers.

## 2.1 Human Control Layer

The human owns the project and holds the highest authority.

Through a dedicated Operator chat, the human can, in natural language, give research input, change constraints, ask for stage reports, adjust the round budget, and issue start, pause, resume or end commands.

The Operator turns explicit intentions into structured control files and reports whether an instruction has been registered, received by the research system, and actually executed.

Ordinary research discussion is never treated as authorization to act.

The Operator chat is not scheduled; it runs only when the human uses it.

## 2.2 Research Layer

The research layer makes the scientific decisions:

- Read the background, past experiments and existing evidence.
- Propose, compare and revise hypotheses.
- Design experiments that can verify or falsify them.
- Define data scope, statistics, metrics and acceptance criteria.
- Review the results returned by engineering.
- Distinguish three different conclusions: the experiment ran successfully, model performance improved, and the hypothesis is supported.
- Decide whether to continue, revise the method, change direction or stop.
- Maintain progress, evidence records and the final report.

The research layer must not substitute more experiments for research progress, and must not change research definitions, samples or evaluation to obtain an expected result.

## 2.3 Engineering Layer

The engineering layer implements and runs:

- Read the formal research task.
- Inspect and reuse existing code.
- Write, modify and test programs.
- Run training, evaluation and data analysis.
- Manage experiment parameters, environments and artifacts.
- Check for implementation errors, data leakage, performance problems and reproducibility.
- Manage local Git history.
- Write the formal result report and hand results back to research.

Engineering may solve ordinary implementation problems on its own, but may not change the core research goal, experiment definition or evaluation criteria without authorization.

Research decisions, engineering execution and human control each have clear boundaries.

# 3. Protecting the existing project

The system is deployed into an existing local project, not a fresh repository.

Such a project typically contains a handoff document `HANDOFF.md`, task descriptions, submission rules, Python sources, Jupyter notebooks, training and validation data, model weights and past experiment folders.

Setup first inspects the real folder and the state of existing work; it never guesses research content from file names.

An existing `HANDOFF.md` is kept as the long-term research record. It can be maintained and appended to, but it never triggers a task.

Formal engineering tasks are published only through separate structured task signals, so ordinary edits to research notes are never mistaken for new tasks.

The system only adds a separate `.bridge/` communication folder; it does not force changes to existing code or data folders.

# 4. Shared-file communication

All units must reach the same real local Windows project folder. Normal collaboration does not use isolated Git worktrees, remote copies or temporary working folders.

Standard layout:

```text
PROJECT_ROOT/
├── HANDOFF.md
├── [existing project files]
│
└── .bridge/
    ├── PROTOCOL.md
    ├── PROJECT_CONSTRAINTS.md
    ├── CLAUDE_STATE.json
    ├── CODEX_STATE.json
    ├── PROJECT_STATUS.md
    ├── FINAL_REPORT.md
    │
    ├── signals/
    │   ├── claude_task.json
    │   └── codex_result.json
    │
    ├── tasks/
    ├── results/
    ├── locks/
    │
    ├── operator/
    │   ├── CONTROL.json
    │   ├── OPERATOR_STATE.json
    │   ├── requests/
    │   └── acknowledgements/
    │
    └── watcher/
        ├── bridge_watcher.ps1      # event-driven dispatcher
        ├── bridgectl.ps1           # human control commands
        ├── live_view.py/.html      # read-only live view of both agents in the browser (kept running by the watcher)
        ├── *_WORKER_PROMPT.txt     # launch prompts of the two workers
        └── OPERATOR_PROMPT.md      # role definition of the Operator chat
```

File names keep the conventions of earlier deployments for compatibility; the behavioral rules do not depend on any particular model vendor.

| File or folder | Purpose |
|---|---|
| `PROTOCOL.md` | Communication rules, state definitions, file ownership and recovery |
| `PROJECT_CONSTRAINTS.md` | This project's goal, evaluation, protected paths, data rules, deadline and compute limits (filled at deployment) |
| `CLAUDE_STATE.json` | Research layer state, processed results, rounds and decisions |
| `CODEX_STATE.json` | Engineering layer state, active task and execution ledger |
| `PROJECT_STATUS.md` | Human-readable progress and stage summaries |
| `signals/claude_task.json` | Publication signal of the latest formal task |
| `signals/codex_result.json` | Publication signal of the latest formal result |
| `tasks/` | Complete, immutable snapshot of every task |
| `results/` | Complete, immutable report of every round |
| `locks/` | Mutual exclusion and duplicate-execution protection |
| `operator/CONTROL.json` | Human lifecycle and resource control |
| `operator/requests/` | The human's research input and operation requests |
| `operator/acknowledgements/` | The research system's receipts for those requests |
| `FINAL_REPORT.md` | Summary report at the end of a stage or project |
| `watcher/` | Event-driven dispatcher, human controls, worker and Operator prompts, run logs |

## 4.1 File ownership

**Single-writer principle.**

Every shared file has exactly one authoritative writer. Others may read it but never modify it.

The research layer alone publishes research tasks, research state and research reports.

The engineering layer alone publishes results, engineering state and formal engineering versions.

The human control layer alone changes control files and request records.

Neither research nor engineering may change the human control state.

Every formal JSON signal and result file is written to a temporary file, validated, and published by an atomic replace on the same file system, so no reader ever sees a partial file.

# 5. Task and result handoff

## 5.1 Task IDs

Formal research tasks use unique increasing IDs:

`R0001 → R0002 → R0003 → ...`

`R0000` is reserved for the first communication test and does not count as a research round.

Each task has exactly one task snapshot and one result report:

```text
.bridge/tasks/R0001.md
.bridge/results/R0001.md
```

Published task snapshots and terminal reports are never overwritten.

## 5.2 Publishing a research task

Every task contains at least:

- Research Objective
- Existing Evidence
- Hypothesis
- Required Implementation
- Data & Methodology (data, methods and statistical constraints)
- Expected Outputs (code and experiment artifacts)
- Acceptance Criteria
- Constraints (data, compute and execution limits)
- Next Decision (the next research decision for each possible outcome)

Tasks have clear boundaries; unrelated questions are never piled into one task that cannot be accepted.

The research layer first writes the complete task snapshot, validates it, then atomically updates the task signal.

## 5.3 Engineering execution and report

On a valid new task, engineering claims it and carries out the implementation and experiments.

The formal report contains:

- Task ID and status
- Actual code changes
- Data and environment
- Parameters and random seeds
- Commands and important logs
- Actual metrics and statistics
- Generated figures, data, weights and paths
- Tests and acceptance results
- Git commit SHA and code state
- Failure reasons and open problems
- Suggestions for the next round

Terminal statuses:

**SUCCESS:** engineering finished; acceptance results are reported truthfully.

**FAILED:** execution failed or the task was not fully completed.

**BLOCKED:** missing data, permission, environment, dependency or research definition; it cannot safely continue.

SUCCESS does not mean the hypothesis holds.

Publication order is fixed: report first, then the result signal, then the engineering state.

## 5.4 Research feedback and the next task

On a new terminal result, research verifies the task ID, method, actual artifacts and reliability before deciding on the next round.

Important scientific conclusions are never accepted from engineering's text summary alone.

An unprocessed result must be reviewed first; while a task is still running, its task pointer is never overwritten.

Signal files are fast notifications; the full task and result snapshots are the authoritative record. If a signal is lost or states disagree, recovery uses the persistent snapshots.

# 6. Event-driven dispatcher (V3.0 implementation)

> Earlier versions used two scheduled tasks (research every 5 minutes, engineering every 3 minutes). That called a model periodically even with no work, and a handoff could wait a full period. V3.0 uses one local file-watching process instead: handoffs take seconds and idle costs no tokens.

The system has one local process, started by the human: `.bridge/watcher/bridge_watcher.ps1` (a Windows PowerShell `FileSystemWatcher`). One instance per project.

Dispatcher and Worker remain logical phases; no permanent AI chat is required.

## 6.1 Phase A: lightweight dispatch (no model call)

When a signal, control, lock or state file in `.bridge/` changes (about 1.5 s debounce), or a worker process exits, the watcher immediately runs two standard-library Python probes:

- `dispatcher.py probe`: is there a formal task engineering can claim?
- `claude_bridge.py probe`: does research have a new result, new user input, the R0000 bootstrap, or recovery work?

The probes read only the minimal control, task and state files and check the same things as the original design: does the lifecycle allow work, is there a new task, an unprocessed result, new input, is the task finished, is there a concurrency conflict.

With no new work, no agent starts and no tokens are spent. Only during STARTING / ACTIVE does the watcher also re-check every 60 s with Python to catch missed file events; this too calls no model.

Idle checks never load datasets, weights, notebooks or history, and never create subagents, train or commit.

## 6.2 Phase B: research / engineering worker

Only when a probe confirms valid work does the watcher start the matching unattended worker process:

- engineering `CANDIDATE` → `codex exec` (prompt: `watcher/CODEX_WORKER_PROMPT.txt`);
- research `WORK` → `claude -p` (prompt: `watcher/CLAUDE_WORKER_PROMPT.txt`).

At most one process per role. A worker finishes its work in that run, writes durable state back to the shared folder and exits. When one side publishes a task or result, the other side starts within seconds.

Workers run in their own hidden consoles: closing the watcher window does not interrupt a running task, and a reopened watcher adopts still-running processes instead of starting duplicates.

## 6.3 Duplicate and idle-loop protection

- If a worker exits normally but its work state is unchanged (it did not finish), it is not restarted; it is marked stalled and the human is alerted.
- If a worker exits abnormally (crash, quota, network), it is retried once after 120 s; a second failure stops retries (see 10.4).
- Locks, the completed-task ledger and immutable snapshots remain the authoritative duplicate protection.

## 6.4 Lifecycle bookkeeping

The watcher performs only deterministic, mechanical bookkeeping and never calls a model:

- record the start and closing of each run (`record-watcher`, idempotent);
- run one communication check per STARTING run (`connectivity-check`);
- when a new run starts, re-point an unfinished task of the previous run at the new `run_id`.

In IDLE, STOPPING and STOPPED no worker starts and there are no periodic checks.

# 7. Dynamic subagent orchestration

Both lead units may, within a formal task, create, manage and end temporary subagents according to task complexity.

There is no fixed roster.

By default each lead runs at most **3 subagents** at once, not counting itself.

## 7.1 When subagents may be created

Only in the formal Worker phase and only when the lifecycle allows work.

Simple tasks are done by the lead alone.

Complex tasks may be split into independent subtasks to gain parallelism, keep the lead's context clean and strengthen independent review.

Subagents are never created just to show off multi-agent capability.

## 7.2 Possible roles

Research subagents may handle:

- hypothesis analysis
- statistical review
- literature and record investigation
- experiment design
- generalization and overfitting analysis
- independent critique

Engineering subagents may handle:

- codebase analysis
- module implementation
- testing and verification
- performance optimization
- log analysis
- independent code review

These are options; the actual split depends on the round's task.

## 7.3 Subagent permissions

Every subagent receives an explicit Parent Task ID, subtask objective, allowed files, expected output, acceptance criteria and stop conditions.

Subagents never publish formal cross-system results, never change human control files, never commit and never rewrite the shared protocol.

Recursive subagents are not allowed unless the human authorizes them.

When several subagents change code, file ownership is split so no notebook, source file or model artifact is written concurrently.

## 7.4 Resources and cost

Parallel work must respect real CPU, GPU, memory and file-system limits.

By default several large training jobs never compete for one GPU without control.

Subagents must bring real parallel gain, context isolation or quality; more subagents do not necessarily lower total token cost.

The lead accepts and integrates every subtask.

# 8. Human Control Layer

The human interacts through a dedicated Operator chat instead of editing JSON files.

The Operator explains progress, registers input, adjusts the budget, updates control state and looks up results.

It is used on demand and does no long-term polling.

## 8.1 Lifecycle states

| State | Meaning |
|---|---|
| IDLE | set up, no start command yet |
| STARTING | checking conditions and starting dispatch |
| ACTIVE | automatic research and experiments allowed |
| STOPPING | new tasks forbidden, execution and dispatch stopping |
| STOPPED | this run has ended; the watcher dispatches nothing |

Control modes:

- RUN: normal research.
- PAUSE_AFTER_ROUND: pause after the specified current task.
- PAUSE_NOW: pause at the next safe checkpoint.
- STOP: no further automatic research.

Lifecycle and mode together decide whether a new task may start.

## 8.2 Start command

**Automatic monitoring and research start only after the human explicitly says "start research".**

Setting up the project, creating a Git repository or the communication folder, configuring subagents, deploying and opening the watcher — none of these authorize a start.

Start procedure:

1. The Operator confirms the user's authorization.
2. It checks that the watcher is running and the previous run is closed (otherwise it refuses).
3. It creates a unique run identifier `run_id` and sets the lifecycle to STARTING.
4. The watcher records the new run and runs the communication check, confirming both sides reach the same project folder.
5. The state becomes ACTIVE.
6. The first run performs the R0000 communication test.
7. Formal research starts after the test passes.

`bridgectl start` does all of this at once. If the watcher is not running, changing the control file alone would start nothing, so `start` refuses.

## 8.3 End command

**When the human says "end research", the system stops publishing new tasks and also stops periodic dispatch.**

End procedure:

1. Set the lifecycle to STOPPING.
2. Forbid claiming and publishing formal tasks.
3. Stop creating subagents.
4. Ask running tasks to stop or end at a safe checkpoint.
5. Save necessary partial results and recovery state.
6. The watcher stops dispatching at once and records the run as closed.
7. Verify that no worker process, engineering lock or research lock is still live.
8. Set the lifecycle to STOPPED (`bridgectl end`; while work is still finishing it reports PARTIALLY_STOPPED — run it again later).

If both sides cannot be confirmed stopped, report a partial stop; never claim the system is fully stopped.

Note: stopping dispatch, stopping a running agent process and killing a training process are three different operations.

Emergency stop: the human ends the process IDs shown by `bridgectl status` and, if needed, the separate training process.

## 8.4 After STOPPED

In STOPPED:

- no automatic polling;
- no new research tasks;
- no model training;
- no new experiments;
- no executing subagents;
- no automatic Git commits;
- no self-resume from an old READY signal.

Code, models, data, task records, Git history and research conclusions are all kept.

Restarting needs the human's explicit authorization again.

## 8.5 Human research input

Ordinary research input is recorded as separate request files, for example:

`.bridge/operator/requests/U0001.md`

Each request has a unique ID, type, target round and handling status.

The system distinguishes:

- registered
- received
- executed
- deferred
- needs a further user decision
- not adopted, with reasons

New input is considered in the next round's planning unless the user explicitly asks to change the current task.

User instructions are never silently dropped.

# 9. Git version control

The project uses one local Git repository with a simple, traceable, linear history.

## 9.1 Initialization

At first deployment, check whether a repository exists.

If not, initialize one with a `.gitignore` suited to Python, Jupyter and model-training projects.

Create a reviewed initial baseline commit preserving the code state before the multi-agent system.

By default no remote is created, nothing is pushed, and nothing is made public.

## 9.2 Excluded files

Excluded by default:

- Python caches
- notebook checkpoints
- virtual environments
- secrets and sensitive configuration
- large raw training and validation data
- large model weights
- large regenerable intermediate artifacts
- `.bridge/` runtime state, signals and locks

Code, configuration, task specifications and reproducible experiment scripts are reviewed and tracked as appropriate.

Files excluded by `.gitignore` get no Git protection, so large raw data, weights and communication history need their own backups.

## 9.3 One commit per round

In a formal round, after verified code changes, engineering commits them with the Task ID.

Each report records:

- Base Commit SHA
- Result Commit SHA
- modified files
- the code state the experiments actually used
- uncommitted changes
- reproduction notes

If an experiment changed no code, the existing commit SHA is recorded; no empty commit.

If experiments ran on an uncommitted working tree, that is stated truthfully; a commit is never claimed to represent code it does not contain.

## 9.4 Git safety

Never stage everything unconditionally, so other agents' records or human control files never slip into an engineering commit.

No destructive reset, clean, forced checkout, history rewrite or push without authorization.

A drop in model performance does not mean code should be rolled back automatically.

A rollback is a new, auditable engineering decision.

Git tracks code and experiment versions; it is not a message queue between agents.

# 10. Concurrency, idempotence and recovery

## 10.1 No duplicate execution

Each formal task is claimed by exactly one valid engineering worker.

Likewise, research reviews never publish the next task from several instances at once.

Mutual exclusion uses atomic exclusive file-system operations.

File modification times alone never decide whether to re-run a task.

## 10.2 Task idempotence

For one Task ID:

- a completed task is never retrained;
- a published result is never overwritten;
- a processed result is never re-analyzed into another next round;
- after a partial write, the current transaction is inspected and recovered first;
- when it is unclear whether something already ran, pause and report instead of retrying blindly.

## 10.3 Leftover locks

If a lock exists but the task state is unclear, check what is actually running.

A lock is never considered safe to delete just because it is old.

If the owner's liveness cannot be confirmed, block and ask for handling.

## 10.4 Failure recovery

After a failure, distinguish:

- the hypothesis was refuted
- a code error
- an environment problem
- a data-quality problem
- insufficient compute
- an incomplete experiment definition
- a failed handoff

Different failures need different handling.

Not every failure becomes "run it again".

When the same unrecoverable error repeats, stop automatic retries, keep the evidence and ask the human.

# 11. Tokens, context and compute budget

The main efficiency strategy is not more agents but less pointless work.

Principles:

1. The main research chat does no high-frequency file checking.
2. The watcher checks the small state files with Python, without any model.
3. With no new work, exit at once.
4. Load the full research material only when there is work.
5. Subagents get only the local context they need.
6. Large logs and artifacts stay on disk, not in the lead's context.
7. Do not re-read history unrelated to the current task.
8. No unnecessary subagents for simple tasks.
9. No uncontrolled parallel training of large models.
10. No unlimited hyperparameter search without statistical control just to chase a metric.

The default formal budget is **8 rounds**; R0000 does not count.

After each round, judge whether further research is worthwhile.

At the budget limit, stop publishing tasks, write a stage summary and wait for the human's decision.

# 12. Project status and research audit

The research layer maintains `PROJECT_STATUS.md` for the Operator to read and explain.

It records at least:

- current research goal
- current run
- current task ID
- completed rounds
- research layer state
- engineering layer state
- most recent experiments
- main metrics
- accepted conclusions
- refuted or still-open hypotheses
- latest human input and its status
- next plan
- current risks or blockers
- time of the last valid update

Every new research decision leaves a short Decision Record stating:

1. what evidence this round produced;
2. which conclusions are accepted;
3. what remains uncertain;
4. why the next experiment was chosen;
5. why the alternatives were not;
6. which uncertainty the next experiment should remove.

This keeps the research logic continuous over long multi-agent runs.

At the end of a project or stage, a complete `FINAL_REPORT.md` links all important tasks, artifacts, model changes, Git versions and conclusions.

# 13. First deployment and acceptance

Five stages.

## Stage 1: inspect and protect the project

Confirm the real folder, existing data, models, past experiments and research documents.

No formal training.

## Stage 2: Git and communication layer

Initialize or reuse the local repository, with safe ignore rules and a baseline commit.

Create `.bridge/`, the protocol and state files; check ownership and access.

## Stage 3: agents and dispatcher

Prepare research, engineering, the human entry point and the subagent configuration.

Configure the watcher, worker prompts and `PROJECT_CONSTRAINTS.md` (generated at once by research-bridge-kit's `deploy.py`). The lifecycle stays IDLE.

The system reports READY_TO_START, not ACTIVE.

## Stage 4: human authorization and the R0000 test

Only the "start research" command moves the system to ACTIVE, through `bridgectl start`.

The first step is the read-only R0000 communication test, verifying that:

- both sides use the same project folder;
- a research task is published correctly;
- engineering claims it automatically;
- the result is written back;
- research recognizes the result automatically;
- repeated checks cause no repeated execution;
- both sides read the human control state correctly.

The test changes no model, raw data or formal code.

## Stage 5: formal research

After R0000 passes, R0001 is published from the existing evidence.

Then the loop runs:

**research planning → formal task → implementation → experiments → result handoff → research review → next task.**

Every round has its task ID, report, research decision and reproducible record.

It continues until the goal is met, the budget is used, an unrecoverable blocker appears, or the human pauses or ends it.

# 14. Safety boundaries and practical limits

Without authorization the system never:

- deletes or overwrites raw training data;
- overwrites existing model weights;
- uploads private research material to external services;
- submits coursework automatically;
- creates public repositories or pushes code;
- trades or moves money;
- starts hidden tasks around the human control state;
- keeps polling after STOPPED;
- pretends a scheduled task, chat or subagent exists when it does not;
- pretends a file control can instantly kill a running process;
- invents model performance, conclusions or test results without real artifacts.

Shared control files carry lifecycle instructions but cannot instantly interrupt a running process; workers respond to a stop only at safe checkpoints.

If a start, pause or stop cannot be completed automatically, say so plainly and ask the human for the needed manual step.

The human entry point can be any Claude chat able to run commands in the project folder (the desktop app's Code tab or the CLI); background research runs in workers started by the watcher, so this never blocks the system.

The system never hides real technical limits to claim "full automation".

# 15. Operating principles

Authority and responsibility:

**The human owns goals, boundaries, authorization and final decisions.**

**The research layer owns scientific judgment, hypothesis design, result review and direction.**

**The engineering layer owns implementation, experiments, quality verification and version tracking.**

**The dispatcher finds new work during an authorized run and enters the worker phase when needed.**

**Subagents complete bounded, temporary specialist tasks under a lead agent.**

**The shared communication layer owns durable state, handoff, result archives, duplicate protection and recovery.**

The core is not letting AIs talk to each other forever, but building **a controllable, auditable, recoverable, reproducible research pipeline with manageable compute cost**.

It stays light with no work, organizes complex research and engineering on its own when needed, adjusts when the human intervenes, and truly stops periodic work after an explicit end.

The final goal is to turn the human from a mechanical task forwarder into the supervisor and decision-maker of the research, while every autonomous AI action stays inside explicit authorization.

---

**Protocol version: V3.0 — Unified Architecture Specification (section 6 is the event-driven watcher implementation, introduced with research-bridge-kit 3.0.0)**

**Core design: Human-Supervised · File-Driven · Event-Gated · Multi-Agent · Git-Traceable · Explicit Start/Stop**
