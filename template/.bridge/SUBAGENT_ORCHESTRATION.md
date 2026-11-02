# Engineering subagent operations (protocol V3.0)

Configuration, not permission. Lifecycle, ownership, Git and PROJECT_CONSTRAINTS rules stay binding.
IDLE, STARTING, STOPPING, STOPPED, Phase A and R0000 use zero subagents. Only the root worker of a
legitimately claimed formal task (R0001+) under ACTIVE + RUN may orchestrate.

## Capability and choice

Use only the native collaboration tools actually exposed in the current `codex exec` session (for
example spawn / send / wait / interrupt / list). If none exist, the root does the work and reports
zero agents with the reason. Another CLI process, a new chat, a service or a scheduled job is never a
substitute. Do not change global configuration or sandbox permissions.

Before delegating, weigh real independence, duplicated reads, context isolation, coordination cost and
compute. Small or sequential work stays with the root (zero or one child). Budget: at most THREE
concurrently running children, excluding root; no recursion without explicit human authorization.

| Role | Scope |
|---|---|
| Code Explorer | read-only module/data-flow inspection with path/symbol evidence |
| Implementation Agent | one independent module or exact exclusive files; minimal verified edits |
| Testing & Verification | assigned tests and checks; never changes the evaluation definition |
| Performance Agent | assigned profiling with bounded resources; no parallel large GPU training |
| Experiment Analysis | existing logs/metrics/errors; no invented metrics |
| Independent Reviewer | read-only correctness/compatibility/leakage review with reproducible findings |

Use a lighter supported model for narrow scans when beneficial and enough capability for complex
implementation and review; check availability at spawn and never claim switching that did not happen.

## Root procedure

1. Read the claimed task, decide whether delegation helps, keep the engineering lock for the whole
   task, record the starting Git state.
2. Assign explicit non-overlapping write scopes (root's included) and fill every field of
   `SUBAGENT_CONTRACT.md`. Notebooks, configs and weights also follow one-writer ownership.
3. Keep `experiments/<task>/orchestration/manifest.json`: parent task/execution/run, each contract,
   real native agent ID, role, write scope, model, resources, status/times, outputs, root acceptance;
   planned vs. actually started separately.
4. Right before each spawn/reactivation run `checkpoint` and then
   `subagent-check --execution-id <ID> --purpose spawn`; verify real native statuses and never exceed
   three. The gate is read-only: it neither spawns nor counts agents.
5. Spawn with minimal explicit context and the filled contract. Leaves check
   `subagent-check --purpose continue` before every stage. No new scope during PAUSE_AFTER_ROUND.
6. Inspect progress at meaningful boundaries with bounded waits; grant GPU work explicitly; by
   default one large training job across root and all children.
7. Reconcile findings, inspect diffs and artifacts, run integration checks. Leaf success is not parent
   acceptance.
8. Before committing or publishing, verify every child is done/stopped and no child process can still
   write. Root alone stages, commits and publishes the single formal report.

## Pause and cancellation

PAUSE_AFTER_ROUND: no new children; existing ones only close the current subtask. PAUSE_NOW, STOP, END,
run change or a failed gate: stop spawning, send each running leaf a stop message, use the supported
interrupt when needed, save permitted partial outputs and record unfinished subtasks. An interrupted
agent turn does not prove an independent process exited; check owned processes separately and never
kill unrelated ones. Children are session-scoped; never respawn an unfinished child blindly.

## Result section

Every result ends with `## Subagent Execution Summary`: number, roles/subtasks and native IDs,
parallel vs. sequential and peak concurrency, artifact paths, unresolved conflicts or live work, and
root's integration evidence; or one truthful sentence stating zero and why.
