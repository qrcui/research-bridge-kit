"""Claude-side bridge helper (Research Lead + Operator). Standard library only.

One-shot commands, never a daemon. Writes only Claude-owned files:
  Research Lead: CLAUDE_STATE.json, signals/claude_task.json, locks/research.lock/
  Operator:      operator/CONTROL.json, operator/OPERATOR_STATE.json
Codex files (CODEX_STATE.json, signals/codex_result.json, results/) are read only.

  probe                      Phase A of claude-research-dispatcher (reads small JSON + request names)
  research-allowed           V2.3 subagent gate: FULL / WRAP_UP_ONLY / NONE from CONTROL
  lock / unlock              research.lock via atomic mkdir
  publish --task-id R0001    validate tasks/<id>.md, then publish READY (resumable transaction)
  recover                    finish an interrupted publish transaction
  resignal                   re-point an unfinished task at a new run_id after a restart
  processed ...              record a reviewed Codex result / handled Operator requests
  control ...                Operator: change CONTROL.json (revision + 1)
  oplog --event X            Operator: append a lifecycle_log entry to OPERATOR_STATE.json
  request --type T --text X  Operator: file the user's words as operator/requests/U####.md

V3.0: .bridge/watcher/bridge_watcher.ps1 runs `probe` after every bridge file change and launches
`claude -p` only on WORK. There are no scheduled tasks any more; `self_disable` is informational.
"""
import argparse
import json
import os
import re
import socket
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / '.bridge'
TASK_RE = re.compile(r'R[0-9]{4}\Z')
REQUEST_RE = re.compile(r'(U[0-9]{4})\.md\Z')
TERMINAL = {'SUCCESS', 'FAILED', 'BLOCKED'}
MODES = {'RUN', 'PAUSE_AFTER_ROUND', 'PAUSE_NOW', 'STOP'}
# Must match Codex's dispatcher.py. PARTIALLY_STARTED / PARTIALLY_STOPPED are Operator report
# states kept in OPERATOR_STATE.json; a pause is lifecycle ACTIVE with mode PAUSE_*.
LIFECYCLES = {'IDLE', 'STARTING', 'ACTIVE', 'STOPPING', 'STOPPED'}
RUN_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
# Same headings Codex's dispatcher.py claim() requires for formal rounds (exact heading text).
FORMAL_SECTIONS = ['Research Objective', 'Hypothesis', 'Methodology', 'Acceptance Criteria']
LOCK = BASE / 'locks' / 'research.lock'


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def read_json(path, limit=131072):
    if path.stat().st_size > limit:
        raise ValueError(f'Oversized state file: {path.name}')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError(f'Expected JSON object: {path.name}')
    return value


def write_json(path, value, new=False):
    """Temp file + fsync + parse check, then os.replace (or no-clobber rename when new)."""
    text = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('x', encoding='utf-8', newline='\n') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        json.loads(tmp.read_text(encoding='utf-8'))
        if new:
            os.rename(tmp, path)  # fails on Windows if the target exists
        else:
            os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def p(rel):
    path = BASE / rel
    for part in [path, *path.parents]:
        if part == BASE:
            break
        if part.exists() and (part.is_symlink() or part.is_junction()):
            raise ValueError('Bridge symlink/junction is forbidden: ' + rel)
    return path


def control():
    c = read_json(p('operator/CONTROL.json'))
    if c.get('mode') not in MODES:
        raise ValueError('Unknown CONTROL mode')
    if c.get('lifecycle') not in LIFECYCLES:
        raise ValueError('Missing or unknown CONTROL lifecycle')
    run_id = c.get('run_id')
    if run_id is not None and (not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id)):
        raise ValueError('Invalid CONTROL run_id')
    if c['lifecycle'] in {'STARTING', 'ACTIVE'} and run_id is None:
        raise ValueError('STARTING/ACTIVE requires a run_id')
    if type(c.get('revision')) is not int or c['revision'] < 0:
        raise ValueError('Invalid CONTROL revision')
    return c


def pending_requests(state):
    handled = set(state.get('handled_requests', []))
    names = sorted(m[1] for f in p('operator/requests').iterdir() if (m := REQUEST_RE.fullmatch(f.name)))
    return [r for r in names if r not in handled and not p(f'operator/acknowledgements/{r}.md').exists()]


def outstanding(signal, result):
    """The published task still waiting for a terminal Codex result, or None."""
    tid = signal.get('task_id')
    if signal.get('status') != 'READY' or tid is None:
        return None
    if result.get('task_id') == tid and result.get('status') in TERMINAL:
        return None
    return tid


def probe(ignore_lock=False):
    try:
        c = control()
        lifecycle = c['lifecycle']
        if lifecycle == 'STARTING':
            # Operator is waiting for Codex's connectivity receipt; stay scheduled, do nothing yet.
            return dict(action='NO_WORK', reason='lifecycle STARTING', self_disable=False)
        if lifecycle in {'IDLE', 'STOPPING', 'STOPPED'} or c['mode'] == 'STOP':
            # Outside an authorized research run the schedule itself should be off.
            return dict(action='INACTIVE', lifecycle=lifecycle, mode=c['mode'], self_disable=True)
        state = read_json(p('CLAUDE_STATE.json'))
        signal = read_json(p('signals/claude_task.json'))
        result = read_json(p('signals/codex_result.json'))
        if LOCK.exists() and not ignore_lock:
            return dict(action='BUSY', reason='research.lock exists; never delete it without investigation')
        if state.get('pending_publish'):
            return dict(action='WORK', reasons=['RECOVER_PUBLISH'], pending=state['pending_publish'])
        if signal.get('task_id') != state.get('last_published_task'):
            return dict(action='BLOCKED', reason='claude_task signal and CLAUDE_STATE disagree')
        reasons = []
        if outstanding(signal, result) and signal.get('run_id') != c['run_id'] and c['mode'] == 'RUN':
            reasons.append('RESIGNAL_RUN')  # unfinished task from an ended run; Codex needs the new run_id
        last = state.get('last_processed_result') or {}
        rid, rrev = result.get('task_id'), result.get('revision', 0)
        if rid is not None and result.get('status') in TERMINAL and (
                rid != last.get('task_id') or rrev != last.get('revision')):
            if rid != state.get('last_published_task'):
                return dict(action='BLOCKED', reason=f'Codex result {rid} does not match published task')
            reasons.append('NEW_RESULT')
        requests = pending_requests(state)
        if requests:
            reasons.append('NEW_REQUESTS')
        if c['mode'] == 'PAUSE_NOW':
            return dict(action='PAUSED', mode='PAUSE_NOW', unreviewed=reasons, self_disable=True)
        if c['mode'] == 'RUN' and state.get('last_published_task') is None:
            reasons.append('BOOTSTRAP_R0000')
        if not reasons:
            # Research Lead stopped the loop and is waiting for the user: polling would only burn tokens.
            idle = state.get('phase') in {'NEEDS_USER', 'RESEARCH_COMPLETE'}
            return dict(action='NO_WORK', outstanding=outstanding(signal, result), mode=c['mode'],
                        phase=state.get('phase'), self_disable=idle)
        return dict(action='WORK', reasons=reasons, result_task=rid, result_status=result.get('status'),
                    result_revision=rrev, requests=requests, mode=c['mode'],
                    control_revision=c['revision'], outstanding=outstanding(signal, result))
    except (OSError, ValueError, TypeError, KeyError) as e:
        return dict(action='BLOCKED', reason=str(e))


def research_allowed():
    """V2.3 gate, checked before every subagent delegation and after each subagent returns."""
    c = control()
    if c['lifecycle'] == 'ACTIVE' and c['mode'] == 'RUN':
        scope = 'FULL'
    elif c['lifecycle'] == 'ACTIVE' and c['mode'] == 'PAUSE_AFTER_ROUND':
        scope = 'WRAP_UP_ONLY'  # finish reviewing the current round; no new round, no new task
    else:
        scope = 'NONE'  # stop delegating, cancel running subagents, save partial analysis
    return dict(action='RESEARCH_SCOPE', scope=scope, lifecycle=c['lifecycle'], mode=c['mode'],
                run_id=c.get('run_id'), control_revision=c['revision'])


def lock():
    execution_id = uuid.uuid4().hex
    try:
        os.mkdir(LOCK)
    except FileExistsError:
        return dict(action='BUSY')
    write_json(LOCK / 'owner.json', dict(execution_id=execution_id, started_at=now(),
                                         host=socket.gethostname()), new=True)
    return dict(action='LOCKED', execution_id=execution_id)


def owns(execution_id):
    if read_json(LOCK / 'owner.json').get('execution_id') != execution_id:
        raise ValueError('This execution does not own research.lock')


def unlock(execution_id):
    owns(execution_id)
    (LOCK / 'owner.json').unlink()
    LOCK.rmdir()  # fails (and keeps the lock) if unknown files were left inside
    return dict(action='UNLOCKED')


def validate_task(tid):
    if not TASK_RE.fullmatch(tid):
        raise ValueError('Malformed task id')
    body = p(f'tasks/{tid}.md').read_text(encoding='utf-8-sig')
    if len(body.strip()) < 20 or f'Task ID: {tid}' not in body:
        raise ValueError('Task snapshot incomplete or missing "Task ID: <id>" line')
    if tid != 'R0000':
        for h in FORMAL_SECTIONS:
            m = re.search(r'^#{1,6}[ \t]+' + re.escape(h) + r'[ \t]*\r?\n(.*?)(?=^#{1,6}[ \t]|\Z)',
                          body, re.M | re.S | re.I)
            if not m or not m[1].strip():
                raise ValueError(f'Missing or empty section: {h}')


def publish(execution_id, tid):
    owns(execution_id)
    c = control()
    if c['mode'] != 'RUN' or c['lifecycle'] != 'ACTIVE':
        raise ValueError(f'CONTROL does not allow publishing ({c.get("lifecycle")}/{c["mode"]})')
    state = read_json(p('CLAUDE_STATE.json'))
    signal = read_json(p('signals/claude_task.json'))
    result = read_json(p('signals/codex_result.json'))
    if state.get('pending_publish'):
        raise ValueError('Interrupted publish pending; run recover first')
    if outstanding(signal, result):
        raise ValueError(f'{signal["task_id"]} has no terminal result yet; never overwrite the pointer')
    if tid in state.get('published_tasks', []) or p(f'results/{tid}.md').exists():
        raise ValueError(f'{tid} was already published; task ids are never reused')
    if tid != 'R0000':
        if state.get('r0000_verified') is not True:
            raise ValueError('R0000 not verified by Research Lead')
        if int(tid[1:]) > c.get('max_research_rounds', 8):
            raise ValueError('Research round budget exhausted')
    validate_task(tid)
    revision = signal.get('revision', 0) + 1
    state['pending_publish'] = dict(task_id=tid, revision=revision, run_id=c['run_id'],
                                    control_revision=c['revision'], at=now())
    write_json(p('CLAUDE_STATE.json'), state)
    return _finish_publish(state)


def _finish_publish(state):
    pend = state['pending_publish']
    tid, revision = pend['task_id'], pend['revision']
    signal = read_json(p('signals/claude_task.json'))
    if control()['run_id'] != pend['run_id']:
        raise ValueError('run_id changed during publish; human reconciliation required')
    if signal.get('task_id') != tid or signal.get('revision') != revision:
        if signal.get('revision', 0) >= revision:
            raise ValueError('Signal moved past the pending transaction; human reconciliation required')
        validate_task(tid)
        write_json(p('signals/claude_task.json'),
                   dict(task_id=tid, status='READY', task_file=f'.bridge/tasks/{tid}.md',
                        revision=revision, run_id=pend['run_id']))
    state['pending_publish'] = None
    state['last_published_task'] = tid
    if tid not in state.get('published_tasks', []):
        state['published_tasks'] = state.get('published_tasks', []) + [tid]
    state['run_id'] = pend['run_id']
    state['phase'] = f'WAITING_FOR_CODEX_{tid}'
    state['updated_at'] = now()
    write_json(p('CLAUDE_STATE.json'), state)
    return dict(action='PUBLISHED', task_id=tid, revision=revision)


def resignal(execution_id):
    """Re-point the unfinished task at the current run_id (same Task ID, revision + 1)."""
    owns(execution_id)
    c = control()
    if c['mode'] != 'RUN' or c['lifecycle'] != 'ACTIVE':
        raise ValueError('CONTROL does not allow publishing')
    state = read_json(p('CLAUDE_STATE.json'))
    signal = read_json(p('signals/claude_task.json'))
    result = read_json(p('signals/codex_result.json'))
    tid = outstanding(signal, result)
    if not tid or signal.get('run_id') == c['run_id'] or state.get('pending_publish'):
        return dict(action='NOTHING_TO_RESIGNAL')
    state['pending_publish'] = dict(task_id=tid, revision=signal['revision'] + 1, run_id=c['run_id'],
                                    control_revision=c['revision'], at=now(), resignal=True)
    write_json(p('CLAUDE_STATE.json'), state)
    return _finish_publish(state)


def recover(execution_id):
    owns(execution_id)
    state = read_json(p('CLAUDE_STATE.json'))
    if not state.get('pending_publish'):
        return dict(action='NOTHING_TO_RECOVER')
    return _finish_publish(state)


def processed(execution_id, result_task=None, result_revision=None, requests=(), r0000_verified=None,
              completed_round=False):
    owns(execution_id)
    state = read_json(p('CLAUDE_STATE.json'))
    if result_task:
        result = read_json(p('signals/codex_result.json'))
        if result.get('task_id') != result_task or result.get('revision') != result_revision:
            raise ValueError('Result signal changed; re-review before marking processed')
        state['last_processed_result'] = dict(task_id=result_task, revision=result_revision,
                                              status=result.get('status'), at=now())
    if requests:
        state['handled_requests'] = sorted(set(state.get('handled_requests', [])) | set(requests))
    if r0000_verified is not None:
        state['r0000_verified'] = r0000_verified
    if completed_round:
        state['completed_rounds'] = state.get('completed_rounds', 0) + 1
    state['updated_at'] = now()
    write_json(p('CLAUDE_STATE.json'), state)
    return dict(action='RECORDED', state=state)


def set_control(lifecycle=None, mode=None, effective_task=..., run_id=..., max_rounds=None, note=None):
    c = control()
    if lifecycle:
        if lifecycle not in LIFECYCLES:
            raise ValueError('Unknown lifecycle')
        c['lifecycle'] = lifecycle
    if mode:
        if mode not in MODES:
            raise ValueError('Unknown mode')
        c['mode'] = mode
    if effective_task is not ...:
        c['effective_task'] = effective_task
    if run_id is not ...:
        c['run_id'] = run_id
    if max_rounds is not None:
        c['max_research_rounds'] = max_rounds
    c['revision'] += 1
    c['updated_by'] = 'operator'
    c['updated_at'] = now()
    if note:
        c['note'] = note
    write_json(p('operator/CONTROL.json'), c)
    readback = read_json(p('operator/CONTROL.json'))
    if readback != c:
        raise IOError('CONTROL readback mismatch')
    return dict(action='CONTROL_UPDATED', control=readback)


def oplog(event, note=None):
    """Operator bookkeeping for bridgectl.ps1 (the human's own CLI). Writes only OPERATOR_STATE."""
    if not re.fullmatch(r'[A-Z][A-Z0-9_]{1,40}', event or ''):
        raise ValueError('Invalid event name')
    path = p('operator/OPERATOR_STATE.json')
    state = read_json(path) if path.exists() else dict(schema_version=1)
    c = control()
    entry = dict(at=now(), event=event, lifecycle=c['lifecycle'], mode=c['mode'], run_id=c.get('run_id'),
                 control_revision=c['revision'], via='bridgectl (human CLI)')
    if note:
        entry['user_words'] = note
    state.setdefault('lifecycle_log', []).append(entry)
    state['report_state'] = f"{c['lifecycle']}/{c['mode']} (V3.0 watcher)"
    state['updated_at'] = now()
    write_json(path, state)
    return dict(action='LOGGED', entry=entry)


REQUEST_TYPES = {'REDIRECT', 'IDEA', 'REVIEW', 'CONFIGURE', 'QUESTION'}
REQUEST_EFFECTIVE = {'NEXT_ROUND', 'IMMEDIATE', 'REFERENCE_ONLY'}


def file_request(rtype, effective, text):
    """Write the user's own words as the next U####.md (no-clobber), then update OPERATOR_STATE."""
    if rtype not in REQUEST_TYPES or effective not in REQUEST_EFFECTIVE:
        raise ValueError('Unknown request type or effective value')
    if not text or not text.strip():
        raise ValueError('Empty request text')
    folder = p('operator/requests')
    folder.mkdir(parents=True, exist_ok=True)
    numbers = [int(m[1][1:]) for f in folder.iterdir() if (m := REQUEST_RE.fullmatch(f.name))]
    rid = f'U{(max(numbers) if numbers else 0) + 1:04d}'
    body = (f'---\nrequest_id: {rid}\ntype: {rtype}\neffective: {effective}\ncreated_by: user\n'
            f'created_at: {now()}\n---\n\n{text.strip()}\n')
    path = p(f'operator/requests/{rid}.md')
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('x', encoding='utf-8', newline='\n') as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path) if os.name == 'nt' else (os.link(tmp, path), tmp.unlink())
    state_path = p('operator/OPERATOR_STATE.json')
    state = read_json(state_path) if state_path.exists() else dict(schema_version=1)
    state['last_request_id'] = rid
    state['updated_at'] = now()
    write_json(state_path, state)
    return dict(action='REQUEST_FILED', request_id=rid, path=f'.bridge/operator/requests/{rid}.md')


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('probe').add_argument('--ignore-lock', action='store_true',
        help='re-probe while holding research.lock (Phase B step 2)')
    sub.add_parser('research-allowed')
    sub.add_parser('lock')
    for name in ['unlock', 'recover', 'resignal']:
        sub.add_parser(name).add_argument('--execution-id', required=True)
    pub = sub.add_parser('publish')
    pub.add_argument('--execution-id', required=True)
    pub.add_argument('--task-id', required=True)
    pr = sub.add_parser('processed')
    pr.add_argument('--execution-id', required=True)
    pr.add_argument('--result-task')
    pr.add_argument('--result-revision', type=int)
    pr.add_argument('--requests', nargs='*', default=[])
    pr.add_argument('--r0000-verified', choices=['true', 'false'])
    pr.add_argument('--completed-round', action='store_true')
    ct = sub.add_parser('control')
    ct.add_argument('--lifecycle')
    ct.add_argument('--mode')
    ct.add_argument('--effective-task')
    ct.add_argument('--clear-effective-task', action='store_true')
    ct.add_argument('--new-run-id', action='store_true')
    ct.add_argument('--max-rounds', type=int)
    ct.add_argument('--note')
    ol = sub.add_parser('oplog')
    ol.add_argument('--event', required=True)
    ol.add_argument('--note')
    rq = sub.add_parser('request')
    rq.add_argument('--type', required=True, choices=sorted(REQUEST_TYPES))
    rq.add_argument('--effective', default='NEXT_ROUND', choices=sorted(REQUEST_EFFECTIVE))
    rq.add_argument('--text', required=True)
    a = ap.parse_args()
    try:
        if a.cmd == 'probe':
            out = probe(a.ignore_lock)
        elif a.cmd == 'research-allowed':
            out = research_allowed()
        elif a.cmd == 'lock':
            out = lock()
        elif a.cmd == 'unlock':
            out = unlock(a.execution_id)
        elif a.cmd == 'recover':
            out = recover(a.execution_id)
        elif a.cmd == 'resignal':
            out = resignal(a.execution_id)
        elif a.cmd == 'publish':
            out = publish(a.execution_id, a.task_id)
        elif a.cmd == 'request':
            out = file_request(a.type, a.effective, a.text)
        elif a.cmd == 'oplog':
            out = oplog(a.event, a.note)
        elif a.cmd == 'processed':
            verified = None if a.r0000_verified is None else a.r0000_verified == 'true'
            out = processed(a.execution_id, a.result_task, a.result_revision, a.requests, verified,
                            a.completed_round)
        else:
            eff = None if a.clear_effective_task else (a.effective_task if a.effective_task else ...)
            run = ('run-' + datetime.now().strftime('%Y%m%d-%H%M%S')) if a.new_run_id else ...
            out = set_control(a.lifecycle, a.mode, eff, run, a.max_rounds, a.note)
    except (OSError, ValueError, TypeError, KeyError) as e:
        out = dict(action='ERROR', reason=str(e))
    print(json.dumps(out, ensure_ascii=False))
    return 1 if out.get('action') in {'ERROR', 'BLOCKED'} else 0


if __name__ == '__main__':
    sys.exit(main())
