# Claude Code × Codex research bridge — protocol V3.0

Project: **{{PROJECT_NAME}}** at `{{PROJECT_ROOT}}`.
Deployed {{DEPLOYED_AT}} from research-bridge-kit {{KIT_VERSION}}. After deployment this protocol is
read-only for both agents; changes require explicit human instruction. Human control has the highest
priority: no task may override the user's boundaries, `.bridge/PROJECT_CONSTRAINTS.md`, filesystem
permissions, or a STOP/PAUSE request.

## 1. Roles and authority

| Role | Runs as | Responsibility |
|---|---|---|
| Human / Project Owner | — | Goal, scope, permissions, budget, start/pause/end, major decisions |
| Claude Operator | interactive Claude chat (`.bridge/watcher/OPERATOR_PROMPT.md`) | Turns the user's explicit words into CONTROL changes and requests; reports what is registered vs. executed |
| Claude Research Lead | `claude -p`, launched by the watcher | Hypotheses, task design, result review, next direction (`.bridge/RESEARCH_BOOTSTRAP.md`) |
| Codex Engineering Lead | `codex exec`, launched by the watcher | Implements and verifies one claimed task, reports actual evidence (`.bridge/watcher/CODEX_WORKER_PROMPT.txt`) |
| Bridge watcher | `.bridge/watcher/bridge_watcher.ps1` | Event-driven dispatch only; never makes research or engineering decisions |

- SUCCESS means engineering acceptance passed, not that a hypothesis is true.
- Project-specific rules (goal, success criteria, data rules, protected paths, deadline, compute,
  other constraints) live in `.bridge/PROJECT_CONSTRAINTS.md` and bind every task and every agent.
- Python for every helper and for project work: `{{PYTHON}}` (3.12 or newer for the helpers).
- Communication is through files in this directory, never through another chat's context. A HANDOFF
  edit is not a task signal. Git is version control, not a message queue.
- Preserve the existing project: never reinitialize it, move/delete datasets, overwrite prior results
  or protected paths, or replace accepted artifacts without explicit human authorization. No archive,
  upload, remote, push or public repository unless the user explicitly asks.

## 2. File ownership (single writer)

| Writer | Exclusive paths |
|---|---|
| Codex | `CODEX_STATE.json`, `signals/codex_result.json`, `results/`, `locks/engineering.lock/`, `locks/codex_state.lock/`, `connectivity/`, Codex engineering code/tests/artifacts (normally `experiments/<task_id>/`) |
| Claude Research Lead | `HANDOFF.md` (append-only convention), `CLAUDE_STATE.json`, `PROJECT_STATUS.md`, `signals/claude_task.json`, `tasks/`, `operator/acknowledgements/`, `FINAL_REPORT.md`, `locks/research.lock/` |
| Claude Operator | `operator/CONTROL.json`, `operator/OPERATOR_STATE.json`, `operator/requests/` |
| Bridge watcher | `watcher/WATCHER_STATE.json`, `watcher/watcher.log`, `watcher/logs/` (the read-only live view writes only `watcher/LIVE_VIEW.json` and `watcher/logs/live_view*.log`) |

The bridge helpers (`dispatcher.py` for Codex, `claude_bridge.py` for Claude/Operator) are the only
way to change JSON state: temp file, fsync, parse check, atomic replace (or no-clobber rename for
immutable files). Nobody edits another role's files, even when they are missing or look wrong;
invalid or conflicting state blocks work instead of being reset.

## 3. Task identity and signals

- `R0000` is the read-only communication test; it is not a research round. `R0001`, `R0002`, …
  are formal rounds. IDs are never reused, including after FAILED/BLOCKED or across runs.
- Tasks are immutable `.bridge/tasks/<id>.md`; results are immutable `.bridge/results/<id>.md`.
- Claude writes and validates the complete task first, then atomically publishes READY. Formal tasks
  contain nonempty `Research Objective`, `Hypothesis`, `Methodology` and `Acceptance Criteria`
  sections (exact heading text), a `Task ID: R####` line, predecessor, data scope, output locations
  and the binding constraints.

```json
{"task_id":null,"status":"IDLE","task_file":null,"revision":0}
{"task_id":"R0001","status":"READY","task_file":".bridge/tasks/R0001.md","revision":2,"run_id":"run-20261101-120000"}
{"task_id":"R0001","status":"SUCCESS","result_file":".bridge/results/R0001.md","revision":1}
```

The READY revision only increases. Its `run_id` must equal CONTROL's current run, so an old READY can
never authorize work in a later run. Result statuses: SUCCESS, FAILED (error or incomplete
execution), BLOCKED (missing data/permission/environment/definition, or safely stopped partial work).
Codex's initial placeholder is `{"task_id":null,"status":"BLOCKED","revision":0,"reason_code":"AWAITING_FIRST_TASK"}`.

## 4. Human control

`operator/CONTROL.json`, written only through `claude_bridge.py control` (revision + 1, atomic, read back):

```json
{"revision":0,"lifecycle":"IDLE","mode":"STOP","run_id":null,"effective_task":null,"max_research_rounds":8,"updated_by":"operator"}
```

| lifecycle | meaning | mode | meaning |
|---|---|---|---|
| IDLE | deployed, never started | RUN | normal research |
| STARTING | new run_id issued, connectivity check pending | PAUSE_AFTER_ROUND | finish the already-claimed `effective_task`, claim nothing new |
| ACTIVE | research allowed | PAUSE_NOW | stop at the next safe checkpoint |
| STOPPING | END requested, live work finishing | STOP | no engineering work |
| STOPPED | run closed | | |

- Missing/malformed CONTROL, unknown values, or STARTING/ACTIVE without a run_id fail closed.
- A START always creates a new run_id with a newer revision. A closed run_id never restarts.
- Workers re-check CONTROL (`dispatcher.py checkpoint`) before every independent training/stage/
  child, at bounded safe checkpoints inside long loops (`Bridge(root).checkpoint(id, phase=...)`), and
  before completion. `proceed:false` (exit code 3) latches that execution stopped for good: save
  permitted partial artifacts, start no new stage, report BLOCKED/FAILED honestly, never SUCCESS.
- Formal round numbers must not exceed `max_research_rounds`.
- File controls cannot kill a blocking process instantly. Pausing dispatch, stopping an agent process
  and killing a training process are three different actions.

## 5. Event-driven dispatch (Phase A / Phase B)

One local PowerShell process, `.bridge/watcher/bridge_watcher.ps1` (`start_watcher.cmd`), started by
the human and single-instance per project. It replaces any scheduled task or polling agent.

**Phase A (no model, no tokens).** After a change under `signals/`, `operator/`, `locks/` or a
`*_STATE.json` (1.5 s debounce), after an agent process exits, and — only while STARTING/ACTIVE —
every 60 s as a safety net, the watcher runs `dispatcher.py probe` and `claude_bridge.py probe`.
Probes read only the small control/signal/state JSON files and path metadata; never HANDOFF, data,
weights, notebooks or history.

**Phase B.** Only `CANDIDATE` from the Codex probe launches `codex exec` with
`watcher/CODEX_WORKER_PROMPT.txt`; only `WORK` from the Claude probe launches `claude -p` with
`watcher/CLAUDE_WORKER_PROMPT.txt`. At most one process per role; each runs in its own hidden console,
so closing the watcher never interrupts a worker, and a restarted watcher re-adopts live workers.
A published READY therefore starts Codex within seconds, and a published result starts Claude.

| Codex probe | meaning | Claude probe | meaning |
|---|---|---|---|
| CANDIDATE | valid unclaimed READY task | WORK | new result, new requests, R0000 bootstrap, resignal or publish recovery |
| NO_WORK / DONE / BUSY | nothing to do / already terminal / lock held | NO_WORK / BUSY | nothing / research.lock held |
| LIFECYCLE_STOP / CONTROL_STOP | not allowed now | INACTIVE / PAUSED | not allowed now |
| CONNECTIVITY_ONLY | STARTING: one connectivity check | BLOCKED | inconsistent Claude state |
| AWAITING_R0000 / LIMIT_REACHED / RECOVERY_REQUIRED / BLOCKED | needs the Research Lead or the human | | |

**Repeat protection.** If a worker exits 0 while its probe state is unchanged, it is not relaunched
(stalled, human alerted). A non-zero exit (crash, quota, network) gets one retry after 120 s, then
stalls. A relevant file change or a watcher restart re-arms it. Locks, ledgers and immutable files
remain the authoritative duplicate protection.

**Deterministic bookkeeping by the watcher** (never by a model): `dispatcher.py record-watcher`
(idempotent lifecycle readback: verifies WATCHER_STATE identity, records ACTIVE runs, closes ended
runs, writes nothing when already current), `dispatcher.py connectivity-check` once per STARTING run,
and `claude_bridge.py lock → probe --ignore-lock → resignal → unlock` when re-pointing an unfinished
task at a new run is the only Claude work.

In IDLE/STOPPING/STOPPED nothing is launched and there are no periodic checks; file events only run
the read-only probes and the idempotent record. An old READY never resumes anything.

## 6. Claiming and the engineering worker

1. Re-run `probe`; continue only on CANDIDATE. Read this complete protocol, `PROJECT_CONSTRAINTS.md`
   and `GIT_PROVENANCE.md`.
2. `claim --task-id <id> --signal-revision <n> --control-revision <n> --run-id <run>` (values from
   probe). The lock is atomic `os.mkdir('.bridge/locks/engineering.lock')`; identity, revisions,
   result existence, ledger and active state are re-checked under the lock. Proceed only on CLAIMED
   and only with its `execution_id` and the immutable `task_snapshot.md` copied into the lock.
   An incomplete task is claimed and answered with one formal BLOCKED report (no retry).
3. Only a matching execution_id owns a lock. `helper_pid` is not worker liveness; never treat a lock
   as stale because of its age or a dead helper PID; never delete a lock to rerun.
4. Read only the context the task needs. Check existing implementations before editing. Check active
   project processes/GPU before training; an unmanaged or ambiguous running experiment is a blocker.
5. Implement the smallest appropriate change, test it, run only the specified experiments, keep the
   evaluation definition and data unchanged. Register a live experiment
   (`checkpoint --running-experiment "<desc>"`) before launch and clear it only after the process
   really exited (`checkpoint --phase stopped --clear-running-experiment`).
6. Record environment, dependencies, data scope, parameters, seeds, exact commands, outputs, failures
   and acceptance results. A flawed research design is explained to Claude (BLOCKED if it prevents a
   valid experiment). Never fabricate metrics.

## 7. Publication, reports and completion

Write `.bridge/locks/engineering.lock/result.draft.md` with these nonempty `##` sections:
Metadata; Summary; Code Changes; Experiment Configuration; Results; Validation; Artifacts; Issues
and Limitations; Feedback to Claude; Git Provenance; Subagent Execution Summary.

- Metadata: Task ID, status, time, predecessor. Failure/blocked reports use the same format and
  distinguish unrun tests from failed tests.
- Git Provenance: the literal bullets of `GIT_PROVENANCE.md` (Repository Root, Branch, Base Commit SHA,
  Result Commit SHA, Modified Files, Uncommitted Changes, Experiment Timing).
- Subagent Execution Summary: counts, roles, concurrency, artifacts, unresolved issues, root
  acceptance; or one sentence stating zero and why.

`dispatcher.py finish --execution-id <id> --status SUCCESS|FAILED|BLOCKED --report <draft>` performs
the mandatory order: (1) immutable `results/<id>.md` with a machine-readable header, (2) result
signal with a higher revision, (3) CODEX_STATE ledger, active task cleared, (4) release of only this
execution's lock. No report means no SUCCESS signal; a live running-experiment marker blocks final
publication. An existing report, terminal signal or ledger entry prevents duplicate work.

## 8. Research Lead cycle

Claude's full procedure is `.bridge/RESEARCH_BOOTSTRAP.md`: lock → re-probe (`--ignore-lock`) →
recover/resignal → review the new result (R0000 verification, then formal reviews in
`tasks/<id>.review.md`) → acknowledge Operator requests → decide the next step or NEEDS_USER →
publish at most one task → PROJECT_STATUS / HANDOFF → unlock. Claude never publishes while the
previous task lacks a terminal result, before R0000 is verified, outside ACTIVE/RUN, or beyond the
round budget (`claude_bridge.py publish` enforces this).

## 9. Recovery

- A report exists but signal/state is incomplete: never re-execute. After the human confirms the
  previous worker and its children stopped, `recover-publication --execution-id <id>
  --human-confirmation "<words>"` replays only signal/state/release for that exact report.
- No report but a lock or active state: preserve everything and investigate with the human. There
  is deliberately no automatic unlock/retry. A follow-up needs a new Task ID.
- Conflicting IDs, hashes, revisions or evidence of newer work stop reconciliation. Never roll back
  a newer signal. A failed/blocked R0000 needs human resolution before formal rounds.
- Repeated identical terminal results consume no further engineering work.

## 10. Git provenance

Idle Phase A performs no Git operations; R0000 is read-only. From R0001 on, follow
`GIT_PROVENANCE.md`: record starting HEAD/branch/dirty state, capture run-code hashes and scoped diffs
(including uncommitted implementation), stage only explicit verified task-owned paths, commit with
`[R####]`, read back the full SHA, and report the Git Provenance section. Never broad-add, absorb
another agent's staged work, commit `.bridge/` runtime files, push, add remotes/branches/worktrees,
rewrite history, stash, or reset/clean destructively.

## 11. Subagents

Allowed only in Phase B of a formal task (R0001+) while CONTROL is ACTIVE + RUN; never in Phase A,
STARTING or R0000. At most three concurrent children per root, no recursion, one writer per file,
one large GPU job at a time unless the constraints say otherwise. Engineering: fill
`SUBAGENT_CONTRACT.md`, follow `SUBAGENT_ORCHESTRATION.md`, gate each spawn with
`checkpoint` + `subagent-check --purpose spawn`; leaves use `subagent-check --purpose continue`.
Research: `RESEARCH_BOOTSTRAP.md` section 7 with the `research-allowed` gate. Only the root
integrates, commits and publishes. Child success is not parent success.

## 12. Start and end

- **START** (only on the user's explicit words): `bridgectl start` refuses unless the watcher runs and
  the previous run is closed; it writes STARTING with a new run_id; the watcher records the run and
  performs the connectivity check; then CONTROL becomes ACTIVE. The first Claude WORK publishes the
  prepared R0000; after its verified SUCCESS, formal rounds begin. Later runs never repeat R0000 and
  re-point an unfinished READY task at the new run.
- **PAUSE / RESUME**: `bridgectl pause` (PAUSE_NOW), `pause-after` (PAUSE_AFTER_ROUND for the current
  task), `resume` (RUN, same run_id).
- **END**: `bridgectl end` writes STOPPING/STOP. The watcher launches nothing new and records the run
  closed; live workers stop at their next checkpoint and report honestly. STOPPED is written only when
  no worker, agent process or research lock is live; otherwise report PARTIALLY_STOPPED and repeat
  later. STOPPED means no new tasks, training, subagents, commits or periodic checks.
- Emergency: the human stops the listed PID (`bridgectl status`) and any training process it started.

## 13. Helper commands (project root)

```powershell
$py = '{{PYTHON}}'
& $py .bridge/dispatcher.py probe
& $py .bridge/dispatcher.py claim --task-id R0001 --signal-revision 2 --control-revision 5 --run-id <run>
& $py .bridge/dispatcher.py checkpoint --execution-id <ID> --phase training --running-experiment "<desc>"
& $py .bridge/dispatcher.py checkpoint --execution-id <ID> --phase stopped --clear-running-experiment
& $py .bridge/dispatcher.py subagent-check --execution-id <ID> --purpose spawn
& $py .bridge/dispatcher.py finish --execution-id <ID> --status SUCCESS --report .bridge/locks/engineering.lock/result.draft.md
& $py .bridge/claude_bridge.py probe            # add --ignore-lock while holding research.lock
& $py .bridge/claude_bridge.py research-allowed
.bridge\watcher\bridgectl status | check | view | start | pause | pause-after | resume | end | request "<text>"
```

## 14. Safety boundaries

Without explicit human authorization: no deleting/overwriting raw data or protected paths, no
replacing accepted models/submissions, no uploads or public repositories, no pushes, no financial or
external actions, no hidden tasks or schedulers, no polling after STOPPED, no claimed agents,
subagents or schedules that do not exist, and no metrics, results or tests that were not actually
produced. Report real limitations instead of claiming full automation.
