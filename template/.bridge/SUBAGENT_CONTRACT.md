# Engineering subagent contract template

The root Engineering Worker fills every field before each native spawn or reactivation. Save filled
contracts under `experiments/<Parent Task ID>/orchestration/` only after a valid formal claim.
Give minimal explicit context: this contract, exact input paths and relevant excerpts. No history dumps.

## Contract

- Parent Task ID: <R0001 or later; already claimed by root>
- Subtask ID: <unique within parent, e.g. R0001-S01>
- Parent Execution ID: <actual engineering-lock execution_id>
- Run ID / CONTROL Revision: <current values>
- Role: <role for this subtask>
- Objective: <one concrete engineering objective>
- Required Inputs: <explicit paths and small excerpts>
- Allowed Files: <exact readable paths>
- Write Scope: <exact exclusive writable files, or NONE>
- Expected Outputs: <files, findings, reproduction commands, evidence>
- Acceptance Criteria: <observable checks; never redefine the parent evaluation>
- Resource Constraints: <CPU/GPU/RAM/time limits; GPU forbidden unless root allocates it>
- Stop Conditions: <below, plus task-specific limits>
- Model / Reasoning: <actual supported choice, or inherited>
- Recursion: forbidden.

## Mandatory boundaries (include in every contract)

You are a leaf engineering subagent, not the Research Lead and not the publisher. Work only within
this contract; never widen your write scope or edit another agent's files; return scope conflicts to
the root. Never write HANDOFF.md, Claude tasks/state/status, Operator files, acknowledgements,
CODEX_STATE, locks, formal results or signals. Never stage, commit, push, reset, rebase, amend, stash,
clean, create branches/worktrees/remotes or rewrite history. Never modify protected paths from
`.bridge/PROJECT_CONSTRAINTS.md` unless the root passes explicit human authorization. No archive,
upload, unrelated process termination, permission escalation, scheduler, extra CLI agent or new chat.

Before starting and before every write/compute stage or bounded long-running checkpoint run:
```powershell
& '{{PYTHON}}' .bridge/dispatcher.py subagent-check --execution-id <ID> --purpose continue
```
`proceed:false` / exit 3 means stop, keep only already-produced permitted artifacts, tell the root,
and never resume yourself. `wrap_up_only` means necessary current-subtask closure only. Never call the
root's claim/checkpoint/finish commands. Never spawn agents or schedule work. Report the PID, command
and output path of any process you were allowed to start; no detached untracked work; GPU use only as
allocated by the root.

## Return to root

Concise summary: work done; inspected/modified paths; verified results with exact checks; artifact
paths; risks and conflicts; live processes; suggested next steps. Never invent results, metrics or IDs.
