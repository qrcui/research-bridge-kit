"""One-shot file bridge, standard library only. Never launches an agent or training.

V3.0: .bridge/watcher/bridge_watcher.ps1 (a FileSystemWatcher) calls probe after each bridge
file change and launches `codex exec` only on CANDIDATE; the worker uses probe -> claim ->
engineering work -> finish. This helper itself is NOT a daemon, watcher, CLI wrapper, or
worker-liveness PID. `record-watcher` is the watcher's idempotent lifecycle readback.
"""
import argparse
import hashlib
import json
import os
import re
import socket
import sys
import tomllib
import uuid
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

TERMINAL = {'SUCCESS', 'FAILED', 'BLOCKED'}
PROTOCOL_VERSION = '3.0'
MODES = {'RUN', 'PAUSE_AFTER_ROUND', 'PAUSE_NOW', 'STOP'}
LIFECYCLES = {'IDLE', 'STARTING', 'ACTIVE', 'STOPPING', 'STOPPED'}
RUN_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
TASK_RE = re.compile(r'R[0-9]{4}\Z')
SECTIONS = ['Metadata', 'Summary', 'Code Changes', 'Experiment Configuration',
            'Results', 'Validation', 'Artifacts', 'Issues and Limitations', 'Feedback to Claude',
            'Git Provenance', 'Subagent Execution Summary']
PROVENANCE_FIELDS = ['Repository Root', 'Branch', 'Base Commit SHA', 'Result Commit SHA',
                     'Modified Files', 'Uncommitted Changes', 'Experiment Timing']
HEADER = '<!-- BRIDGE_RESULT_JSON '


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path, limit=131072):
    if path.stat().st_size > limit:
        raise ValueError(f'Oversized state file: {path.name}')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError(f'Expected JSON object: {path.name}')
    return value


def atomic_write(path, text, new=False):
    """Publish a complete UTF-8 file. New immutable files can never replace a target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('x', encoding='utf-8', newline='\n') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if tmp.read_bytes() != text.encode('utf-8'):
            raise IOError('Temporary-file verification failed')
        if path.suffix == '.json':
            json.loads(tmp.read_text(encoding='utf-8'))
        if new:
            if os.name == 'nt':
                os.rename(tmp, path)  # Windows rename fails if destination exists.
            else:
                os.link(tmp, path)  # atomic no-clobber publication on test hosts.
                tmp.unlink()
        else:
            os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_json(path, value, new=False):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n', new)


class Bridge:
    def __init__(self, project_root):
        self.root = Path(project_root).resolve()
        self.base = self.root / '.bridge'
        self.lock = self.base / 'locks' / 'engineering.lock'

    @contextmanager
    def state_transaction(self):
        """Short bounded single-writer transaction; never used by idle reads or held over work."""
        lock = self.path('locks/codex_state.lock')
        deadline = time.monotonic() + 1.0
        while True:
            try:
                os.mkdir(lock)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ValueError('Codex state transaction is busy or interrupted; do not steal its lock')
                time.sleep(0.02)
        identity = uuid.uuid4().hex
        try:
            write_json(lock / 'owner.json', dict(transaction_id=identity, helper_pid=os.getpid(), started_at=now()), new=True)
            state = read_json(self.path('CODEX_STATE.json'))
            yield state
            write_json(self.path('CODEX_STATE.json'), state)
        finally:
            owner = lock / 'owner.json'
            if owner.exists():
                if read_json(owner).get('transaction_id') != identity:
                    raise ValueError('State transaction lock ownership changed; preserve for recovery')
                owner.unlink()
            # Exact known metadata only; unknown files or a crash remain for inspection.
            lock.rmdir()

    def path(self, relative):
        p = self.base / relative
        if not p.resolve().is_relative_to(self.base.absolute()):
            raise ValueError('Bridge path escapes project or follows a junction')
        for part in [self.base, *p.parents]:
            if part == self.root:
                break
            if part.exists() and (part.is_symlink() or part.is_junction()):
                raise ValueError('Bridge symlink/junction is forbidden')
        if p.exists() and (p.is_symlink() or p.is_junction()):
            raise ValueError('Bridge symlink/junction is forbidden')
        return p

    def initialize(self):
        for directory in ['', 'signals', 'tasks', 'results', 'locks', 'operator',
                          'operator/requests', 'operator/acknowledgements']:
            self.path(directory).mkdir(parents=True, exist_ok=True)
        state = dict(schema_version=1, protocol_version=PROTOCOL_VERSION, status='IDLE', lifecycle='IDLE', run_id=None,
                     closed_run_ids={}, connectivity_runs={},
                     project_root=str(self.root), active_task=None, completed_tasks={},
                     last_completed_task=None, last_seen_control_revision=None,
                     initialized_at=now(), updated_at=now(), scheduler=None)
        result = dict(task_id=None, status='BLOCKED', result_file=None, revision=0,
                      reason_code='AWAITING_FIRST_TASK')
        for name, value in [('CODEX_STATE.json', state), ('signals/codex_result.json', result)]:
            path = self.path(name)
            if path.exists():
                read_json(path)  # Preserve existing state; invalid JSON blocks initialization.
            else:
                write_json(path, value, new=True)
        return dict(action='INITIALIZED', state=read_json(self.path('CODEX_STATE.json')))

    @staticmethod
    def valid_control(control):
        if control.get('lifecycle') not in LIFECYCLES:
            raise ValueError('Missing or unknown V2.2 CONTROL lifecycle')
        run_id = control.get('run_id')
        if run_id is not None and (not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id)):
            raise ValueError('Invalid CONTROL run_id')
        if control['lifecycle'] in {'STARTING', 'ACTIVE'} and run_id is None:
            raise ValueError('STARTING/ACTIVE requires an Operator-issued run_id')
        if control.get('mode') not in MODES:
            raise ValueError('Unknown CONTROL mode')
        if type(control.get('revision')) is not int or control['revision'] < 0:
            raise ValueError('Invalid CONTROL revision')
        cap = control.get('max_research_rounds', 8)
        if type(cap) is not int or cap < 0:
            raise ValueError('Invalid max_research_rounds')
        return cap

    @staticmethod
    def allowed(control, task_id=None, claimed=False):
        return control['lifecycle'] == 'ACTIVE' and (control['mode'] == 'RUN' or
                (claimed and control['mode'] == 'PAUSE_AFTER_ROUND'
                 and task_id is not None and control.get('effective_task') == task_id))

    @staticmethod
    def validate_run(control, state):
        seen = state.get('last_seen_control_revision')
        if seen is not None and (type(seen) is not int or control['revision'] < seen):
            raise ValueError('CONTROL revision regressed or recorded revision is invalid')
        closed = state.get('closed_run_ids', {})
        if not isinstance(closed, dict):
            raise ValueError('Invalid closed_run_ids ledger')
        if control['run_id'] in closed:
            raise ValueError('Ended run_id cannot restart; a new Operator START is required')
        old_run = state.get('run_id')
        if old_run and old_run != control['run_id']:
            if seen is not None and control['revision'] <= seen:
                raise ValueError('New run requires a strictly newer CONTROL revision')
            if state.get('lifecycle') in {'STARTING', 'ACTIVE', 'STOPPING'}:
                raise ValueError('Previous run has not safely ended; inspect preserved state first')

    def probe(self, ignore_lock=False):
        """Phase A: reads only the four small control/signal/state JSON files plus stat."""
        try:
            control = read_json(self.path('operator/CONTROL.json'))
            cap = self.valid_control(control)
            if control['lifecycle'] in {'IDLE', 'STOPPING', 'STOPPED'}:
                return dict(action='LIFECYCLE_STOP', lifecycle=control['lifecycle'],
                            run_id=control.get('run_id'), control_revision=control['revision'])
            if control['mode'] != 'RUN':
                return dict(action='CONTROL_STOP', mode=control['mode'], revision=control['revision'])
            if control['lifecycle'] == 'STARTING':
                self.validate_run(control, read_json(self.path('CODEX_STATE.json')))
                return dict(action='CONNECTIVITY_ONLY', run_id=control['run_id'], control_revision=control['revision'])
            signal = read_json(self.path('signals/claude_task.json'))
            state = read_json(self.path('CODEX_STATE.json'))
            result = read_json(self.path('signals/codex_result.json'))
            if signal.get('status') not in {'IDLE', 'READY'}:
                raise ValueError('Unknown Claude signal status')
            if result.get('status') not in TERMINAL or type(result.get('revision')) is not int or result['revision'] < 0:
                raise ValueError('Invalid Codex result signal')
            if not isinstance(state.get('completed_tasks'), dict) or not (
                state.get('active_task') is None or isinstance(state['active_task'], dict)):
                raise ValueError('Invalid Codex execution state')
            for completed_id, completed in state['completed_tasks'].items():
                if not TASK_RE.fullmatch(completed_id) or not isinstance(completed, dict) or completed.get('status') not in TERMINAL:
                    raise ValueError('Invalid completed task record')
            active = state.get('active_task')
            if active and (not isinstance(active.get('task_id'), str) or not TASK_RE.fullmatch(active['task_id'])
                           or not isinstance(active.get('execution_id'), str) or not active['execution_id']):
                raise ValueError('Invalid active task record')
            self.validate_run(control, state)
            if signal.get('status') != 'READY':
                return dict(action='NO_WORK', reason='No READY task')
            tid = signal.get('task_id')
            if not isinstance(tid, str) or not TASK_RE.fullmatch(tid):
                raise ValueError('Invalid task_id')
            if not self.allowed(control, tid):
                return dict(action='CONTROL_STOP', mode=control['mode'], task_id=tid)
            if type(signal.get('revision')) is not int or signal['revision'] < 1:
                raise ValueError('Invalid READY revision')
            if signal.get('run_id') != control['run_id']:
                raise ValueError('READY signal run_id does not match authorized CONTROL run_id')
            if signal.get('task_file') != f'.bridge/tasks/{tid}.md':
                raise ValueError('task_file must be the exact canonical task path')
            self.path(f'tasks/{tid}.md')  # Reject path indirection before reading the task.
            target = self.path(f'results/{tid}.md')
            if target.exists():
                committed = state.get('completed_tasks', {}).get(tid)
                if (committed and result.get('task_id') == tid and result.get('status') == committed.get('status')
                        and result.get('execution_id') == committed.get('execution_id')
                        and result.get('result_file') == f'.bridge/results/{tid}.md'):
                    return dict(action='DONE', task_id=tid, status=result['status'])
                return dict(action='RECOVERY_REQUIRED', task_id=tid,
                            reason='Immutable report exists; never repeat engineering work')
            if tid in state.get('completed_tasks', {}) or result.get('task_id') == tid:
                return dict(action='RECOVERY_REQUIRED', task_id=tid,
                            reason='Terminal state/signal without matching report')
            if self.lock.exists() and not ignore_lock:
                return dict(action='BUSY', task_id=tid, reason='Engineering lock exists; do not steal it')
            if state.get('active_task'):
                return dict(action='RECOVERY_REQUIRED', task_id=tid,
                            reason='Active execution recorded; never assume helper PID means worker exited')
            if int(tid[1:]) > cap:
                return dict(action='LIMIT_REACHED', task_id=tid, max_research_rounds=cap)
            if tid != 'R0000':
                first = state.get('completed_tasks', {}).get('R0000', {})
                if first.get('status') != 'SUCCESS' or not self.path('results/R0000.md').exists():
                    return dict(action='AWAITING_R0000', task_id=tid)
            return dict(action='CANDIDATE', task_id=tid, signal_revision=signal['revision'],
                        control_revision=control['revision'], run_id=control['run_id'],
                        task_file=f'.bridge/tasks/{tid}.md')
        except (OSError, ValueError, TypeError, KeyError) as e:
            return dict(action='BLOCKED', reason=str(e), reason_code='INVALID_OR_MISSING_BRIDGE_INPUT')

    def owner(self, execution_id):
        value = read_json(self.path('locks/engineering.lock/owner.json'))
        if value.get('execution_id') != execution_id:
            raise ValueError('Execution does not own the engineering lock')
        return value

    def lifecycle_check(self):
        """One-shot manual START/END inspection. Does not manage the native scheduler."""
        try:
            control = read_json(self.path('operator/CONTROL.json'))
            self.valid_control(control)
            state = read_json(self.path('CODEX_STATE.json'))
            info = dict(lifecycle=control['lifecycle'], mode=control['mode'],
                        run_id=control.get('run_id'), control_revision=control['revision'])
            if control['lifecycle'] not in {'STARTING', 'ACTIVE'} or control['mode'] != 'RUN':
                return dict(action='PAUSE_REQUIRED', **info)
            active = state.get('active_task')
            if (active and active.get('run_id') != control['run_id']) or (
                    self.lock.exists() and not active):
                return dict(action='RECOVERY_REQUIRED', reason='Preserved execution needs investigation', **info)
            if active and active.get('stop_required'):
                return dict(action='RECOVERY_REQUIRED', reason='Stopped execution cannot automatically resume', **info)
            self.validate_run(control, state)
            return dict(action='ENABLE_ALLOWED', **info)
        except (OSError, ValueError, TypeError, KeyError) as e:
            return dict(action='PAUSE_REQUIRED', reason=str(e))

    def connectivity_check(self):
        """At most one light check per authorized STARTING run; never claims a Task ID."""
        try:
            control = read_json(self.path('operator/CONTROL.json'))
            self.valid_control(control)
            if control['lifecycle'] != 'STARTING' or control['mode'] != 'RUN':
                return dict(action='NOT_STARTING')
            state = read_json(self.path('CODEX_STATE.json'))
            self.validate_run(control, state)
            run_id = control['run_id']
            if state.get('active_task') or self.lock.exists():
                return dict(action='BLOCKED', reason='Preserved engineering work must be reconciled first')
            previous = state.get('connectivity_runs', {}).get(run_id)
            if previous:
                return dict(action='CONNECTIVITY_DONE', run_id=run_id, result=previous)
            marker = self.path(f'connectivity/{run_id}.json')
            if marker.exists():
                return dict(action='CONNECTIVITY_DONE', run_id=run_id, result=read_json(marker))
            attempt = dict(run_id=run_id, status='CHECKING', control_revision=control['revision'], started_at=now())
            try:
                write_json(marker, attempt, new=True)  # no-clobber reservation; a crash never triggers a retry
            except FileExistsError:
                return dict(action='CONNECTIVITY_DONE', run_id=run_id, result=read_json(marker))
            try:
                signal = read_json(self.path('signals/claude_task.json'))
                result = read_json(self.path('signals/codex_result.json'))
                if signal.get('status') not in {'IDLE', 'READY'} or result.get('status') not in TERMINAL:
                    raise ValueError('Invalid small signal schema')
                current = read_json(self.path('operator/CONTROL.json'))
                if current != control:
                    raise ValueError('CONTROL changed during connectivity check')
                attempt.update(status='SUCCESS', detail='Four small JSON files readable; own-state write only; no Task ID executed')
            except (OSError, ValueError, TypeError, KeyError) as e:
                attempt.update(status='BLOCKED', detail=str(e))
            attempt['finished_at'] = now()
            # Lifecycle actions can run in another invocation. Never publish a stale STARTING
            # snapshot over a newer END, closed-run ledger, scheduler readback or active owner.
            with self.state_transaction() as state:
                latest = read_json(self.path('operator/CONTROL.json'))
                same_start = (latest == control and not state.get('active_task')
                              and run_id not in state.get('closed_run_ids', {}))
                if not same_start:
                    attempt.update(status='BLOCKED', detail='Lifecycle/state changed; connectivity cannot advance this run')
                    if latest.get('lifecycle') in {'STOPPING', 'STOPPED'}:
                        state.setdefault('closed_run_ids', {}).setdefault(run_id, dict(reason=latest['lifecycle'],
                                         observed_at=now(), revision=latest.get('revision')))
                        if state.get('lifecycle') != 'STOPPED':
                            state.update(lifecycle='STOPPING', status='STOP_REQUESTED', stop_reason='END during connectivity')
                write_json(marker, attempt)
                state.setdefault('connectivity_runs', {})[run_id] = attempt
                if same_start:
                    state.update(lifecycle='STARTING', run_id=run_id, status='STARTING',
                                 last_seen_control_revision=control['revision'])
                state['updated_at'] = now()
            return dict(action='CONNECTIVITY_CHECKED', run_id=run_id, result=attempt)
        except (OSError, ValueError, TypeError, KeyError) as e:
            return dict(action='BLOCKED', reason=str(e))

    def record_scheduler(self, status, native_config=None):
        """Read back App-owned config AFTER its API action; never enables/pauses it itself."""
        if status not in {'ACTIVE', 'PAUSED'}:
            raise ValueError('Unknown native scheduler status')
        if native_config is None:
            native_config = Path.home() / '.codex/automations/codex-engineering-dispatcher/automation.toml'
        config = tomllib.loads(Path(native_config).read_text(encoding='utf-8'))
        if (config.get('id') != 'codex-engineering-dispatcher' or config.get('kind') != 'cron'
                or config.get('execution_environment') != 'local'
                or [Path(p).resolve() for p in config.get('cwds', [])] != [self.root]):
            raise ValueError('Native schedule identity or local project mismatch')
        if config.get('status') != status:
            raise ValueError('Native scheduler readback does not match requested status')

        def recheck():
            if tomllib.loads(Path(native_config).read_text(encoding='utf-8')) != config:
                raise ValueError('Native scheduler changed during readback; inspect again')
        fields = dict(id=config['id'], kind=config['kind'], execution_environment='local', native_config=str(native_config),
                      prompt_sha256=digest(config.get('prompt', '').encode('utf-8')))
        return self._record_lifecycle(status, fields, recheck)

    def _record_lifecycle(self, status, fields, recheck=None):
        """Shared lifecycle bookkeeping after a verified scheduler/watcher readback."""
        with self.state_transaction() as state:
            if recheck:
                recheck()
            observed = self.lifecycle_check()
            if status == 'ACTIVE' and observed['action'] != 'ENABLE_ALLOWED':
                raise ValueError('Operator START is not valid; pause native scheduler immediately')
            state.setdefault('scheduler', {})
            state.setdefault('run_id', None)
            state.setdefault('closed_run_ids', {})
            state.setdefault('connectivity_runs', {})
            if state['scheduler'] is None:
                state['scheduler'] = {}
            if fields.get('kind') != state['scheduler'].get('kind') and state['scheduler']:
                # Switching native schedule <-> watcher: keep the old record for audit, not as live config.
                state['retired_scheduler'] = dict(state['scheduler'], retired_at=now())
                state['scheduler'] = {}
            state['scheduler'].update(status=status, verified_at=now(), **fields)
            if status == 'ACTIVE':
                state['scheduler'].pop('pause_reason', None)
                state.update(lifecycle=observed['lifecycle'], run_id=observed['run_id'],
                             last_seen_control_revision=observed['control_revision'])
                if not state.get('active_task'):
                    state['status'] = observed['lifecycle']
            else:
                life = observed.get('lifecycle')
                state['scheduler']['pause_reason'] = observed.get('reason', f'Observed lifecycle={life}, mode={observed.get("mode")}')
                if life in {'STOPPING', 'STOPPED'}:
                    run_id = observed.get('run_id') or state.get('run_id')
                    if run_id:
                        state.setdefault('closed_run_ids', {})[run_id] = dict(reason=life, observed_at=now(),
                                                                          revision=observed['control_revision'])
                    seen_revision = state.get('last_seen_control_revision')
                    stop_revision = max(observed['control_revision'], seen_revision if type(seen_revision) is int else 0)
                    state.update(lifecycle='STOPPING' if state.get('active_task') else 'STOPPED',
                                 status='STOP_REQUESTED' if state.get('active_task') else 'STOPPED',
                                 stop_reason=f'Operator END: {life}', last_seen_control_revision=stop_revision)
                elif life == 'IDLE' or life is None:
                    if not state.get('active_task'):
                        state.update(lifecycle='IDLE', status='IDLE')
                # Pausing the schedule alone never falsely declares a live worker finished.
            state.update(protocol_version=PROTOCOL_VERSION, updated_at=now())
        return dict(action='SCHEDULER_RECORDED', status=status, lifecycle=state.get('lifecycle'),
                    run_id=state.get('run_id'), observed_control=observed)

    # ---- V3.0 event-driven watcher (replaces both native schedules) ----

    def _watcher_readback(self, watcher_id):
        """The watcher writes .bridge/watcher/WATCHER_STATE.json; this verifies it is the live owner."""
        if not isinstance(watcher_id, str) or not RUN_RE.fullmatch(watcher_id):
            raise ValueError('Invalid watcher id')
        info = read_json(self.path('watcher/WATCHER_STATE.json'))
        if info.get('watcher_id') != watcher_id or info.get('status') != 'RUNNING':
            raise ValueError('WATCHER_STATE does not show this watcher RUNNING')
        if [Path(info.get('project_root', '')).resolve()] != [self.root]:
            raise ValueError('Watcher project root mismatch')
        return info

    def _watcher_record_current(self, status, watcher_id):
        """True when CODEX_STATE already reflects this watcher readback; avoids self-triggering writes."""
        state = read_json(self.path('CODEX_STATE.json'))
        sched = state.get('scheduler') or {}
        if sched.get('kind') != 'watcher' or sched.get('id') != watcher_id or sched.get('status') != status:
            return False
        control = read_json(self.path('operator/CONTROL.json'))
        life = control.get('lifecycle')
        if status == 'ACTIVE':
            return (state.get('run_id') == control.get('run_id') and state.get('lifecycle') == life
                    and state.get('last_seen_control_revision') == control.get('revision'))
        if life in {'STOPPING', 'STOPPED'}:
            want = 'STOPPING' if state.get('active_task') else 'STOPPED'
            run_id = control.get('run_id') or state.get('run_id')
            return state.get('lifecycle') == want and (not run_id or run_id in state.get('closed_run_ids', {}))
        if life == 'IDLE':
            return bool(state.get('active_task')) or state.get('lifecycle') == 'IDLE'
        return True

    def record_watcher(self, status, watcher_id):
        """Idempotent lifecycle record for the V3.0 FileSystemWatcher. Never launches anything."""
        try:
            if status not in {'ACTIVE', 'PAUSED'}:
                raise ValueError('Unknown watcher status')
            info = self._watcher_readback(watcher_id)
            if self._watcher_record_current(status, watcher_id):
                return dict(action='ALREADY_RECORDED', status=status)
            fields = dict(id=watcher_id, kind='watcher', execution_environment='local',
                          native_config='.bridge/watcher/WATCHER_STATE.json', watcher_pid=info.get('pid'),
                          watcher_started_at=info.get('started_at'))
            return self._record_lifecycle(status, fields, lambda: self._watcher_readback(watcher_id))
        except (OSError, ValueError, TypeError, KeyError) as e:
            return dict(action='BLOCKED', reason=str(e))

    def _release(self, execution_id):
        self.owner(execution_id)
        # No recursive deletion; remove only this execution's known bridge bookkeeping.
        allowed = {'owner.json', 'task_snapshot.md', 'result.draft.md'}
        unexpected = [p.name for p in self.lock.iterdir() if p.name not in allowed]
        if unexpected:
            raise ValueError(f'Unknown lock files retained for recovery: {unexpected}')
        for name in ['task_snapshot.md', 'result.draft.md', 'owner.json']:
            p = self.path('locks/engineering.lock/' + name)
            if p.exists():
                p.unlink()
        self.lock.rmdir()

    def claim(self, task_id, signal_revision, control_revision, run_id=None):
        before = self.probe()
        expected = dict(task_id=task_id, signal_revision=signal_revision, control_revision=control_revision,
                        run_id=before.get('run_id') if run_id is None else run_id)
        if before.get('action') != 'CANDIDATE' or any(before.get(k) != v for k,v in expected.items()):
            return dict(action='NOT_CLAIMED', observed=before)
        execution_id = uuid.uuid4().hex
        try:
            os.mkdir(self.path('locks/engineering.lock'))
        except FileExistsError:
            return dict(action='BUSY', task_id=task_id)
        owner = dict(**expected, execution_id=execution_id, started_at=now(), host=socket.gethostname(),
                     helper_pid=os.getpid(), phase='CLAIMING', running_experiment=None,
                     task_sha256=None, note='helper_pid is not the desktop worker lifetime')
        write_json(self.path('locks/engineering.lock/owner.json'), owner, new=True)
        after = self.probe(ignore_lock=True)
        if after.get('action') != 'CANDIDATE' or any(after.get(k) != v for k,v in expected.items()):
            self._release(execution_id)
            return dict(action='NOT_CLAIMED', observed=after)
        problem = None
        try:
            task = self.path(f'tasks/{task_id}.md')
            if task.stat().st_size > 2_000_000:
                raise ValueError('Task snapshot is too large')
            raw = task.read_bytes()
            body = raw.decode('utf-8-sig')
            if len(body.strip()) < 20:
                raise ValueError('Task snapshot is incomplete')
            if task_id != 'R0000':
                for heading in ['Research Objective', 'Hypothesis', 'Methodology', 'Acceptance Criteria']:
                    section = re.search(r'^#{1,6}[ \t]+' + re.escape(heading)
                            + r'[ \t]*\r?\n(.*?)(?=^#{1,6}[ \t]|\Z)', body, re.M|re.S|re.I)
                    if not section or not section[1].strip():
                        raise ValueError(f'Missing or empty task section: {heading}')
                first = self._read_report('R0000')
                if first['status'] != 'SUCCESS':
                    raise ValueError('R0000 has no verified SUCCESS report')
            atomic_write(self.path('locks/engineering.lock/task_snapshot.md'), body, new=True)
            owner['task_sha256'] = digest(raw)
        except (OSError, ValueError, UnicodeError) as e:
            problem = str(e)
        owner['phase'] = 'CLAIMED'
        write_json(self.path('locks/engineering.lock/owner.json'), owner)
        with self.state_transaction() as state:
            if state.get('active_task'):
                raise ValueError('Conflicting active task appeared during claim; preserve lock')
            ended = owner['run_id'] in state.get('closed_run_ids', {})
            if ended:
                owner.update(stop_required=True, stop_reason='END was recorded during claim')
                write_json(self.path('locks/engineering.lock/owner.json'), owner)
            state.update(status='STOP_REQUESTED' if ended else 'RUNNING', run_id=owner['run_id'], active_task=owner,
                         updated_at=now(), last_seen_control_revision=max(control_revision, state.get('last_seen_control_revision') or 0))
            if not ended:
                state['lifecycle'] = 'ACTIVE'
        if problem:
            return self.finish(execution_id, 'BLOCKED', self.blocked_report(task_id, problem))
        # Check for a pause arriving during the snapshot copy before any engineering side effect.
        check = self.checkpoint(execution_id, phase='CLAIMED')
        if not check['proceed']:
            return self.finish(execution_id, 'BLOCKED', self.blocked_report(task_id, check['reason']))
        return dict(action='CLAIMED', **owner, task_snapshot='.bridge/locks/engineering.lock/task_snapshot.md')

    def subagent_check(self, execution_id, purpose='spawn'):
        """Read-only Phase B gate. Does not create agents, reserve slots, or edit shared state."""
        try:
            if purpose not in {'spawn', 'continue'}:
                raise ValueError('Unknown subagent check purpose')
            control = read_json(self.path('operator/CONTROL.json'))
            self.valid_control(control)
            owner = self.owner(execution_id)
            state = read_json(self.path('CODEX_STATE.json'))
            self.validate_run(control, state)
            active = state.get('active_task') or {}
            if active.get('execution_id') != execution_id or active.get('task_id') != owner.get('task_id'):
                raise ValueError('Subagent has no matching claimed parent execution')
            if owner.get('task_id') == 'R0000':
                raise ValueError('Read-only R0000 is not an engineering delegation task')
            if (not isinstance(owner.get('task_id'), str) or not TASK_RE.fullmatch(owner['task_id'])
                    or int(owner['task_id'][1:]) < 1):
                raise ValueError('A formal parent Task ID is required')
            if owner.get('stop_required') or active.get('stop_required') or owner.get('phase') == 'PUBLISHING':
                raise ValueError('Parent execution is stopped or publishing; no subagent work permitted')
            if control.get('run_id') != owner.get('run_id') or state.get('run_id') != owner.get('run_id'):
                raise ValueError('Subagent run_id no longer matches the parent')
            if not self.allowed(control, owner['task_id'], claimed=(purpose == 'continue')):
                raise ValueError('Subagent creation/work is forbidden by lifecycle or mode')
            if not owner.get('task_sha256') or digest(self.path(f"tasks/{owner['task_id']}.md").read_bytes()) != owner['task_sha256']:
                raise ValueError('Parent task identity changed')
            if read_json(self.path('operator/CONTROL.json')) != control:
                raise ValueError('CONTROL changed during subagent check')
            return dict(action='SUBAGENT_ALLOWED', proceed=True, purpose=purpose, task_id=owner['task_id'],
                        run_id=owner['run_id'], execution_id=execution_id, control_revision=control['revision'],
                        scope='wrap_up_only' if control['mode'] == 'PAUSE_AFTER_ROUND' else 'assigned_subtask_only',
                        max_concurrent_subagents=3, recursive_delegation=False)
        except (OSError, ValueError, TypeError, KeyError) as e:
            return dict(action='SUBAGENT_STOP', proceed=False, reason=str(e))

    def checkpoint(self, execution_id, phase='work', running_experiment=...):
        owner = self.owner(execution_id)
        if running_experiment is ...:
            running_experiment = owner.get('running_experiment')
        try:
            control = read_json(self.path('operator/CONTROL.json'))
            self.valid_control(control)
            seen_control = owner.get('latest_control_revision')
            if seen_control is None:
                seen_control = owner['control_revision']
            if control['revision'] < seen_control:
                raise ValueError('CONTROL revision regressed; human reconciliation required')
            proceed = self.allowed(control, owner['task_id'], claimed=True) and control['run_id'] == owner.get('run_id')
            reason = 'RUN permitted' if proceed else f"Human control: {control['lifecycle']}/{control['mode']} or run_id changed"
            if owner.get('stop_required'):
                proceed, reason = False, owner.get('stop_reason', 'This execution was already stopped; never resume it automatically')
            if owner['task_sha256'] is not None:
                if digest(self.path(f"tasks/{owner['task_id']}.md").read_bytes()) != owner['task_sha256']:
                    proceed, reason = False, 'Immutable task snapshot changed after claim'
        except (OSError, ValueError, TypeError, KeyError) as e:
            control, proceed, reason = {}, False, str(e)
        latest_revision = control.get('revision')
        if latest_revision is None:
            latest_revision = owner.get('latest_control_revision', owner['control_revision'])
        owner.update(phase=phase, running_experiment=running_experiment, checkpoint_at=now(),
                     latest_control_revision=latest_revision, stop_required=not proceed)
        if not proceed:
            owner.setdefault('stop_reason', reason)
        write_json(self.path('locks/engineering.lock/owner.json'), owner)
        with self.state_transaction() as state:
            if (state.get('active_task') or {}).get('execution_id') != execution_id:
                raise ValueError('Active state conflicts with lock owner')
            if owner.get('run_id') in state.get('closed_run_ids', {}):
                proceed, reason = False, 'END is recorded for this run'
                owner.update(stop_required=True, stop_reason=reason)
                write_json(self.path('locks/engineering.lock/owner.json'), owner)
            state.update(active_task=owner, status='RUNNING' if proceed else 'STOP_REQUESTED', updated_at=now(),
                         last_seen_control_revision=max(latest_revision or 0, state.get('last_seen_control_revision') or 0))
            if not proceed:
                state['stop_reason'] = reason
                if control.get('lifecycle') in {'IDLE', 'STOPPING', 'STOPPED'}:
                    state['lifecycle'] = control['lifecycle']
                if control.get('lifecycle') in {'STOPPING', 'STOPPED'}:
                    state.setdefault('closed_run_ids', {})[owner['run_id']] = dict(reason=reason, observed_at=now(),
                                                                                revision=latest_revision)
        return dict(action='CHECKPOINT', proceed=proceed, reason=reason, execution_id=execution_id)

    def blocked_report(self, tid, reason):
        details = dict(Metadata=f'Task ID: {tid}\nExecution status: BLOCKED\nTime: {now()}\nPredecessor: not available during preflight',
                       Summary='Task could not safely start or continue.',
                       **{'Code Changes':'No research code was changed.',
                          'Experiment Configuration':'No training started; standard-library bridge checks only.',
                          'Results':'No experimental metrics were generated.',
                          'Validation':'Preflight or human-control checks did not pass.',
                          'Artifacts':'This immutable report and the Codex state/signal.',
                          'Issues and Limitations':reason,
                          'Feedback to Claude':'Resolve the blocker with human oversight. Do not reuse a terminal Task ID.',
                          'Git Provenance':f'- Repository Root: project {self.root}; Git not inspected during preflight\n'
                              '- Branch: not inspected\n- Base Commit SHA: unavailable; blocked before engineering\n'
                              '- Result Commit SHA: none; no commit attempted\n- Modified Files: bridge bookkeeping only\n'
                              '- Uncommitted Changes: not inspected; user files preserved\n'
                              '- Experiment Timing: no experiment run',
                          'Subagent Execution Summary':'0 subagents. Preflight blocked before engineering; no delegation or child artifacts.'})
        return '\n\n'.join(f'## {h}\n\n{details[h]}' for h in SECTIONS) + '\n'

    def _read_report(self, tid):
        line = self.path(f'results/{tid}.md').read_text(encoding='utf-8').splitlines()[0]
        if not line.startswith(HEADER) or not line.endswith(' -->'):
            raise ValueError('Existing report has no verifiable publication metadata; human recovery required')
        meta = json.loads(line[len(HEADER):-4])
        if meta.get('task_id') != tid or meta.get('status') not in TERMINAL:
            raise ValueError('Invalid immutable report metadata')
        return meta

    def _commit(self, owner, meta):
        tid = owner['task_id']
        with self.state_transaction() as state:
            signal = read_json(self.path('signals/codex_result.json'))
            active = state.get('active_task')
            if active and active.get('execution_id') != owner['execution_id']:
                raise ValueError('Conflicting active execution; publication stopped')
            prior = state.get('completed_tasks', {}).get(tid)
            if prior and (prior.get('execution_id') != owner['execution_id'] or prior.get('status') != meta['status']):
                raise ValueError('Conflicting completed execution; publication stopped')
            if signal['revision'] > meta['result_revision']:
                raise ValueError('Refusing to roll back a newer result signal')
            if signal['revision'] == meta['result_revision'] and signal.get('task_id') != tid:
                raise ValueError('Conflicting result revision')
            write_json(self.path('signals/codex_result.json'), dict(task_id=tid, status=meta['status'],
                       result_file=f'.bridge/results/{tid}.md', revision=meta['result_revision'],
                       execution_id=owner['execution_id'], result_sha256=digest(self.path(f'results/{tid}.md').read_bytes())))
            state.setdefault('completed_tasks', {})[tid] = dict(status=meta['status'], result_file=f'.bridge/results/{tid}.md',
                    execution_id=owner['execution_id'], task_sha256=meta['task_sha256'], finished_at=meta['published_at'],
                    run_id=owner.get('run_id'))
            state.update(status='BLOCKED' if meta['status'] == 'BLOCKED' else 'READY_TO_RECEIVE',
                         active_task=None, last_completed_task=tid, updated_at=now())
        self._release(owner['execution_id'])
        return dict(action='PUBLISHED', task_id=tid, status=meta['status'], result_file=f'.bridge/results/{tid}.md')

    def finish(self, execution_id, status, report_text):
        owner = self.owner(execution_id)
        if status not in TERMINAL:
            raise ValueError('Invalid terminal status')
        # Recheck human control and task identity at the publication boundary.
        # A BLOCKED/FAILED report remains publishable after a stop request.
        self.checkpoint(execution_id, phase='PUBLISHING')
        owner = self.owner(execution_id)
        if owner.get('stop_required') and status == 'SUCCESS':
            raise ValueError('Stop requested: report partial execution as BLOCKED or FAILED')
        if owner.get('running_experiment'):
            raise ValueError('Clear running_experiment only after the child computation has actually stopped')
        for heading in SECTIONS:
            section = re.search(r'^##[ \t]+' + re.escape(heading) + r'[ \t]*\n(.*?)(?=^##[ \t]|\Z)', report_text, re.M|re.S)
            if not section or not section[1].strip():
                raise ValueError('Incomplete report: ' + heading)
            if heading == 'Git Provenance':
                for field in PROVENANCE_FIELDS:
                    item = re.search(r'^-[ \t]+' + re.escape(field) + r':[ \t]*([^\r\n]+)', section[1], re.M)
                    if not item or not item[1].strip():
                        raise ValueError('Incomplete Git Provenance: ' + field)
        target = self.path(f"results/{owner['task_id']}.md")
        if target.exists():
            raise FileExistsError('Immutable report already exists; publication recovery only')
        signal = read_json(self.path('signals/codex_result.json'))
        meta = dict(schema_version=1, task_id=owner['task_id'], status=status, execution_id=execution_id,
                    run_id=owner.get('run_id'),
                    task_sha256=owner['task_sha256'], control_revision_at_claim=owner['control_revision'],
                    result_revision=signal['revision'] + 1, published_at=now())
        text = HEADER + json.dumps(meta, ensure_ascii=True, sort_keys=True) + ' -->\n\n' + report_text.rstrip() + '\n'
        atomic_write(target, text, new=True)
        return self._commit(owner, meta)

    def recover_publication(self, execution_id, confirmation):
        """Human-confirmed inactive worker only; never resumes or repeats engineering work."""
        if not confirmation.strip():
            raise ValueError('Explicit human confirmation that the previous worker stopped is required')
        owner = self.owner(execution_id)
        meta = self._read_report(owner['task_id'])
        if meta['execution_id'] != execution_id or meta['task_sha256'] != owner['task_sha256']:
            raise ValueError('Report and lock identity conflict')
        if owner.get('running_experiment'):
            raise ValueError('Recorded computation requires manual investigation; no automatic unlock')
        return self._commit(owner, meta)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    sub = p.add_subparsers(dest='command', required=True)
    for cmd in ['init', 'probe', 'lifecycle-check', 'connectivity-check']:
        sub.add_parser(cmd)
    claim = sub.add_parser('claim'); claim.add_argument('--task-id', required=True)
    claim.add_argument('--signal-revision', type=int, required=True); claim.add_argument('--control-revision', type=int, required=True)
    claim.add_argument('--run-id', required=True)
    record = sub.add_parser('record-scheduler'); record.add_argument('--status', choices=['ACTIVE', 'PAUSED'], required=True)
    watch = sub.add_parser('record-watcher'); watch.add_argument('--status', choices=['ACTIVE', 'PAUSED'], required=True)
    watch.add_argument('--watcher-id', required=True)
    subcheck = sub.add_parser('subagent-check'); subcheck.add_argument('--execution-id', required=True)
    subcheck.add_argument('--purpose', choices=['spawn', 'continue'], default='spawn')
    check = sub.add_parser('checkpoint'); check.add_argument('--execution-id', required=True)
    check.add_argument('--phase', default='work')
    flags=check.add_mutually_exclusive_group()
    flags.add_argument('--running-experiment', default=argparse.SUPPRESS)
    flags.add_argument('--clear-running-experiment', action='store_true')
    finish = sub.add_parser('finish'); finish.add_argument('--execution-id', required=True)
    finish.add_argument('--status', choices=sorted(TERMINAL), required=True); finish.add_argument('--report', type=Path, required=True)
    recover = sub.add_parser('recover-publication'); recover.add_argument('--execution-id', required=True)
    recover.add_argument('--human-confirmation', required=True)
    a = p.parse_args(); bridge = Bridge(a.root)
    if a.command == 'init': result = bridge.initialize()
    elif a.command == 'probe': result = bridge.probe()
    elif a.command == 'lifecycle-check': result = bridge.lifecycle_check()
    elif a.command == 'connectivity-check': result = bridge.connectivity_check()
    elif a.command == 'record-scheduler': result = bridge.record_scheduler(a.status)
    elif a.command == 'record-watcher': result = bridge.record_watcher(a.status, a.watcher_id)
    elif a.command == 'subagent-check': result = bridge.subagent_check(a.execution_id, a.purpose)
    elif a.command == 'claim': result = bridge.claim(a.task_id, a.signal_revision, a.control_revision, a.run_id)
    elif a.command == 'checkpoint': result = bridge.checkpoint(a.execution_id, a.phase,
                None if a.clear_running_experiment else getattr(a, 'running_experiment', ...))
    elif a.command == 'recover-publication': result = bridge.recover_publication(a.execution_id, a.human_confirmation)
    else:
        report = a.report.resolve()
        if not report.is_relative_to(bridge.lock.resolve()):
            raise ValueError('Write the draft inside the owned engineering lock directory')
        result = bridge.finish(a.execution_id, a.status, report.read_text(encoding='utf-8-sig'))
    print(json.dumps(result, ensure_ascii=True), flush=True)
    if a.command in {'checkpoint', 'subagent-check'} and not result['proceed']:
        raise SystemExit(3)


if __name__ == '__main__':
    main()
