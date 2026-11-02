"""Isolated standard-library tests. No real bridge state or model is exercised.

Run with the project Python 3.14 environment. Every fake project is created
under this .bridge directory and verified before recursive fixture cleanup.
"""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatcher
from dispatcher import Bridge

PROVENANCE_FIELDS = ['Repository Root', 'Branch', 'Base Commit SHA',
                     'Result Commit SHA', 'Modified Files',
                     'Uncommitted Changes', 'Experiment Timing']


def claim_process(project, ready, start, replies):
    """A short-lived helper PID must not make its surviving worker lock stale."""
    try:
        ready.put(os.getpid())
        if not start.wait(20):
            raise TimeoutError('claim test start signal did not arrive')
        replies.put(Bridge(project).claim('R0000', 1, 1))
    except BaseException as exc:
        replies.put({'test_error': repr(exc)})


def connectivity_process(project, ready, start, replies):
    try:
        ready.put(os.getpid())
        if not start.wait(20):
            raise TimeoutError('connectivity test start signal did not arrive')
        replies.put(Bridge(project).connectivity_check())
    except BaseException as exc:
        replies.put({'test_error': repr(exc)})


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='bridge-test-', dir=HERE)).resolve()
        self.assertEqual(self.root.parent, HERE)
        self.bridge = Bridge(self.root)
        self.bridge.initialize()
        self.legacy = {}
        for name in ['HANDOFF.md', 'model.pt', 'train.csv', 'validation.csv',
                     'student_model.py', 'train_model.ipynb', 'evaluate_model.ipynb']:
            path = self.root / name
            path.write_bytes(('untouched fixture ' + name).encode())
            self.legacy[name] = path.read_bytes()

    def tearDown(self):
        for name, original in self.legacy.items():
            self.assertEqual((self.root / name).read_bytes(), original, name)
        resolved = self.root.resolve()
        # Never clean an inferred parent, real project, junction, or unknown path.
        if (resolved.parent != HERE or not resolved.name.startswith('bridge-test-')
                or resolved == HERE or not resolved.is_relative_to(HERE)
                or self.root.is_symlink() or self.root.is_junction()):
            raise RuntimeError(f'Unsafe test cleanup target: {resolved}')
        shutil.rmtree(resolved)

    def jread(self, relative):
        return json.loads(self.bridge.path(relative).read_text(encoding='utf-8'))

    def jwrite(self, relative, value):
        dispatcher.write_json(self.bridge.path(relative), value)

    def control(self, mode='RUN', revision=1, effective_task=None, cap=8,
                lifecycle='ACTIVE', run_id='fixture-run-1'):
        self.jwrite('operator/CONTROL.json', dict(revision=revision, mode=mode,
                    effective_task=effective_task, max_research_rounds=cap,
                    lifecycle=lifecycle, run_id=run_id,
                    updated_by='fixture_operator'))

    def signal(self, tid='R0000', revision=1, status='READY', body=None, run_id='fixture-run-1'):
        if tid is not None and body is not False:
            if body is None:
                body = ('# Read-only fixture communication test\n\n'
                        'Confirm files exist. Do not load weights or train a model.\n')
                if tid != 'R0000':
                    body = '\n\n'.join('## ' + h + '\n\nFixture content only.' for h in
                              ['Research Objective', 'Hypothesis', 'Methodology', 'Acceptance Criteria'])
            self.bridge.path(f'tasks/{tid}.md').write_text(body, encoding='utf-8')
        self.jwrite('signals/claude_task.json', dict(task_id=tid, status=status,
                    task_file=None if tid is None else f'.bridge/tasks/{tid}.md',
                    revision=revision, run_id=run_id))

    def ready(self, tid='R0000'):
        self.control()
        self.signal(tid)

    def native_config(self, status='PAUSED', **overrides):
        config = dict(id='codex-engineering-dispatcher', kind='cron',
                      execution_environment='local', cwds=[str(self.root)],
                      status=status, prompt='Isolated fixture; never executes a native action.')
        config.update(overrides)
        path = self.root / 'fixture_automation.toml'
        path.write_text('\n'.join(f'{key} = {json.dumps(value)}' for key, value in config.items()) + '\n',
                        encoding='utf-8')
        return path

    def claim(self):
        result = self.bridge.claim('R0000', 1, 1)
        self.assertEqual(result['action'], 'CLAIMED', result)
        return result['execution_id']

    def report(self, status='SUCCESS'):
        provenance = dict(zip(PROVENANCE_FIELDS, [str(self.root), 'fixture-branch',
                          'a' * 40, 'a' * 40, 'None; read-only fixture.',
                          'None; isolated fake project.', 'No experiment executed.']))
        return '\n\n'.join('## ' + h + '\n\n' +
               (f'Task ID: R0000\nExecution status: {status}' if h == 'Metadata'
                else '\n'.join(f'- {label}: {value}' for label, value in provenance.items())
                if h == 'Git Provenance'
                else 'Isolated fixture. No actual research or training executed.')
               for h in dispatcher.SECTIONS)

    def assert_provenance_fields(self, report):
        section = re.search(r'^## Git Provenance\n(.*?)(?=^## |\Z)', report, re.M | re.S)
        self.assertIsNotNone(section, 'Missing Git Provenance section')
        for label in PROVENANCE_FIELDS:
            match = re.search(r'^- ' + re.escape(label) + r':[ \t]*(\S[^\r\n]*)$',
                              section[1], re.M)
            self.assertIsNotNone(match, 'Missing or empty provenance field: ' + label)

    def complete_bootstrap(self):
        self.ready()
        execution = self.claim()
        self.bridge.finish(execution, 'SUCCESS', self.report())

    def watcher(self, watcher_id='watch-1', status='RUNNING', root=None):
        self.jwrite('watcher/WATCHER_STATE.json', dict(watcher_id=watcher_id, status=status, pid=4242,
                    started_at='2026-11-01T15:00:00+08:00', project_root=str(root or self.root)))

    def test_v30_watcher_record_requires_live_matching_watcher(self):
        self.ready()
        original = self.bridge.path('CODEX_STATE.json').read_bytes()
        self.assertEqual(self.bridge.record_watcher('ACTIVE', 'watch-1')['action'], 'BLOCKED')  # no state file
        for kwargs in [dict(watcher_id='other'), dict(status='STOPPED'), dict(root=self.root.parent)]:
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                self.watcher(**kwargs)
                self.assertEqual(self.bridge.record_watcher('ACTIVE', 'watch-1')['action'], 'BLOCKED')
        self.assertEqual(self.bridge.path('CODEX_STATE.json').read_bytes(), original)

    def test_v30_watcher_active_record_is_idempotent_and_needs_operator_start(self):
        self.watcher()
        original = self.bridge.path('CODEX_STATE.json').read_bytes()
        self.assertEqual(self.bridge.record_watcher('ACTIVE', 'watch-1')['action'], 'BLOCKED')  # no CONTROL
        self.assertEqual(self.bridge.path('CODEX_STATE.json').read_bytes(), original)
        self.ready()
        self.assertEqual(self.bridge.record_watcher('ACTIVE', 'watch-1')['action'], 'SCHEDULER_RECORDED')
        state = self.jread('CODEX_STATE.json')
        self.assertEqual((state['scheduler']['kind'], state['scheduler']['status']), ('watcher', 'ACTIVE'))
        self.assertEqual((state['lifecycle'], state['run_id']), ('ACTIVE', 'fixture-run-1'))
        written = self.bridge.path('CODEX_STATE.json').read_bytes()
        # A second identical event must not rewrite state (otherwise the watcher would re-trigger itself).
        self.assertEqual(self.bridge.record_watcher('ACTIVE', 'watch-1')['action'], 'ALREADY_RECORDED')
        self.assertEqual(self.bridge.path('CODEX_STATE.json').read_bytes(), written)
        self.assertEqual(self.bridge.probe()['action'], 'CANDIDATE')

    def test_v30_watcher_closes_stale_native_run_then_allows_new_run(self):
        # Reproduces the real 2026-11-01 state: native schedules deleted while CODEX_STATE still ACTIVE.
        self.complete_bootstrap()
        self.bridge.record_scheduler('ACTIVE', self.native_config('ACTIVE'))
        self.signal('R0001', revision=2)
        self.control('STOP', revision=4, lifecycle='STOPPED')
        self.watcher()
        self.assertEqual(self.bridge.probe()['action'], 'LIFECYCLE_STOP')
        self.assertEqual(self.bridge.record_watcher('PAUSED', 'watch-1')['action'], 'SCHEDULER_RECORDED')
        state = self.jread('CODEX_STATE.json')
        self.assertEqual(state['lifecycle'], 'STOPPED')
        self.assertIn('fixture-run-1', state['closed_run_ids'])
        self.assertEqual(state['retired_scheduler']['kind'], 'cron')
        self.assertEqual(self.bridge.record_watcher('PAUSED', 'watch-1')['action'], 'ALREADY_RECORDED')
        # Old run cannot restart; new Operator START (new run id, newer revision) can.
        self.control(revision=5, lifecycle='ACTIVE')
        self.assertEqual(self.bridge.record_watcher('ACTIVE', 'watch-1')['action'], 'BLOCKED')
        self.control(revision=5, lifecycle='ACTIVE', run_id='fixture-run-2')
        self.assertEqual(self.bridge.probe()['action'], 'BLOCKED')  # READY still carries the old run id
        self.assertEqual(self.bridge.record_watcher('ACTIVE', 'watch-1')['action'], 'SCHEDULER_RECORDED')
        self.signal('R0001', revision=3, run_id='fixture-run-2')
        probe = self.bridge.probe()
        self.assertEqual((probe['action'], probe['task_id'], probe['run_id']), ('CANDIDATE', 'R0001', 'fixture-run-2'))

    def test_v30_watcher_pause_keeps_live_worker(self):
        self.ready(); self.watcher(); execution = self.claim()
        self.control('STOP', revision=2, lifecycle='STOPPING')
        self.assertEqual(self.bridge.record_watcher('PAUSED', 'watch-1')['action'], 'SCHEDULER_RECORDED')
        state = self.jread('CODEX_STATE.json')
        self.assertEqual(state['lifecycle'], 'STOPPING')
        self.assertEqual(state['active_task']['execution_id'], execution)
        self.assertEqual(self.bridge.record_watcher('PAUSED', 'watch-1')['action'], 'ALREADY_RECORDED')
        self.assertFalse(self.bridge.checkpoint(execution)['proceed'])

    def test_v30_watcher_starting_then_connectivity(self):
        self.control(lifecycle='STARTING'); self.signal(); self.watcher()
        self.assertEqual(self.bridge.probe()['action'], 'CONNECTIVITY_ONLY')
        self.assertEqual(self.bridge.record_watcher('ACTIVE', 'watch-1')['action'], 'SCHEDULER_RECORDED')
        self.assertEqual(self.bridge.connectivity_check()['result']['status'], 'SUCCESS')
        self.assertEqual(self.bridge.connectivity_check()['action'], 'CONNECTIVITY_DONE')
        self.control(revision=2, lifecycle='ACTIVE')
        self.assertEqual(self.bridge.record_watcher('ACTIVE', 'watch-1')['action'], 'SCHEDULER_RECORDED')
        self.assertEqual(self.bridge.probe()['action'], 'CANDIDATE')

    def test_initialize_writes_only_codex_files_and_preserves_valid_state(self):
        state = self.jread('CODEX_STATE.json')
        self.assertEqual(state['status'], 'IDLE')
        self.assertEqual(state['lifecycle'], 'IDLE')
        self.assertIsNone(state['run_id'])
        result = self.jread('signals/codex_result.json')
        self.assertEqual((result['task_id'], result['status'], result['revision']), (None, 'BLOCKED', 0))
        for path in ['operator/CONTROL.json', 'operator/OPERATOR_STATE.json',
                     'CLAUDE_STATE.json', 'PROJECT_STATUS.md', 'signals/claude_task.json']:
            self.assertFalse(self.bridge.path(path).exists(), path)
        self.control('STOP', revision=7)
        state['test_marker'] = 'preserve existing owned state'
        self.jwrite('CODEX_STATE.json', state)
        originals = {p: self.bridge.path(p).read_bytes() for p in
                     ['CODEX_STATE.json', 'signals/codex_result.json', 'operator/CONTROL.json']}
        self.bridge.initialize()
        for path, raw in originals.items():
            self.assertEqual(self.bridge.path(path).read_bytes(), raw)

    def test_missing_control_is_blocked_and_not_created(self):
        self.assertEqual(self.bridge.probe()['action'], 'BLOCKED')
        self.assertFalse(self.bridge.path('operator/CONTROL.json').exists())

    def test_lifecycle_and_active_run_id_are_required_valid_control_fields(self):
        self.ready()
        valid = self.jread('operator/CONTROL.json')
        variants = []
        for key in ['lifecycle', 'run_id']:
            value = dict(valid); value.pop(key); variants.append(value)
        for lifecycle in [None, 'UNKNOWN', 'active', True]:
            variants.append(dict(valid, lifecycle=lifecycle))
        for lifecycle in ['STARTING', 'ACTIVE']:
            for run_id in [None, '', ' ', 123, True, [], 'fixture\nrun']:
                variants.append(dict(valid, lifecycle=lifecycle, run_id=run_id))
        for value in variants:
            with self.subTest(value=value):
                self.jwrite('operator/CONTROL.json', value)
                self.assertEqual(self.bridge.probe()['action'], 'BLOCKED')
                self.assertNotEqual(self.bridge.claim('R0000', 1, 1)['action'], 'CLAIMED')
                self.assertFalse(self.bridge.lock.exists())

    def test_inactive_lifecycles_ignore_ready_signal_and_never_claim(self):
        self.signal()
        original_read = Path.read_text
        signal_path = self.bridge.path('signals/claude_task.json')
        def no_ready_read(path, *args, **kwargs):
            self.assertNotEqual(Path(path), signal_path, 'Inactive lifecycle read the READY signal')
            return original_read(path, *args, **kwargs)
        for lifecycle in ['IDLE', 'STOPPING', 'STOPPED']:
            with self.subTest(lifecycle=lifecycle):
                self.control(lifecycle=lifecycle)
                # Even corrupt stale input cannot turn an inactive lifecycle into work.
                signal_path.write_text('{corrupt stale READY', encoding='utf-8')
                with mock.patch.object(Path, 'read_text', no_ready_read):
                    self.assertEqual(self.bridge.probe()['action'], 'LIFECYCLE_STOP')
                    self.assertNotEqual(self.bridge.claim('R0000', 1, 1)['action'], 'CLAIMED')
                self.assertFalse(self.bridge.lock.exists())
                self.assertIsNone(self.jread('CODEX_STATE.json')['active_task'])
                self.assertEqual(self.jread('signals/codex_result.json')['revision'], 0)
                self.assertEqual(list(self.bridge.path('results').iterdir()), [])

    def test_starting_returns_connectivity_only_without_formal_execution(self):
        self.control(lifecycle='STARTING')
        self.signal()
        self.assertEqual(self.bridge.probe()['action'], 'CONNECTIVITY_ONLY')
        self.assertNotEqual(self.bridge.claim('R0000', 1, 1)['action'], 'CLAIMED')
        self.assertFalse(self.bridge.lock.exists())
        self.assertIsNone(self.jread('CODEX_STATE.json')['active_task'])
        self.assertEqual(self.jread('signals/codex_result.json')['revision'], 0)
        self.assertEqual(list(self.bridge.path('results').iterdir()), [])

    def test_ready_signal_run_id_must_match_current_control(self):
        self.ready()
        valid = self.jread('signals/claude_task.json')
        variants = [dict(valid, run_id=run_id) for run_id in
                    [None, '', 123, 'fixture-previous-run']]
        missing = dict(valid); missing.pop('run_id'); variants.append(missing)
        for value in variants:
            with self.subTest(value=value):
                self.jwrite('signals/claude_task.json', value)
                self.assertNotEqual(self.bridge.probe()['action'], 'CANDIDATE')
                self.assertNotEqual(self.bridge.claim('R0000', 1, 1)['action'], 'CLAIMED')
                self.assertFalse(self.bridge.lock.exists())
                self.assertEqual(list(self.bridge.path('results').iterdir()), [])

    def test_lifecycle_check_fails_closed_and_only_active_or_starting_run_can_enable(self):
        self.assertEqual(self.bridge.lifecycle_check()['action'], 'PAUSE_REQUIRED')
        for lifecycle in ['IDLE', 'STOPPING', 'STOPPED', 'STARTING', 'ACTIVE']:
            for mode in ['RUN', 'STOP', 'PAUSE_NOW', 'PAUSE_AFTER_ROUND']:
                with self.subTest(lifecycle=lifecycle, mode=mode):
                    self.control(mode, lifecycle=lifecycle)
                    expected = 'ENABLE_ALLOWED' if lifecycle in {'STARTING', 'ACTIVE'} and mode == 'RUN' else 'PAUSE_REQUIRED'
                    self.assertEqual(self.bridge.lifecycle_check()['action'], expected)
        self.bridge.path('operator/CONTROL.json').write_text('{invalid', encoding='utf-8')
        self.assertEqual(self.bridge.lifecycle_check()['action'], 'PAUSE_REQUIRED')

    def test_lifecycle_check_never_reclaims_an_execution_from_another_run(self):
        self.ready(); execution = self.claim()
        original = self.bridge.path('locks/engineering.lock/owner.json').read_bytes()
        self.control(revision=2, run_id='fixture-run-2')
        self.assertEqual(self.bridge.lifecycle_check()['action'], 'RECOVERY_REQUIRED')
        self.assertEqual(self.bridge.path('locks/engineering.lock/owner.json').read_bytes(), original)
        self.assertEqual(self.bridge.owner(execution)['run_id'], 'fixture-run-1')

    def test_closed_run_id_cannot_be_reenabled_or_claimed(self):
        self.ready()
        state = self.jread('CODEX_STATE.json')
        state['closed_run_ids'] = {'fixture-run-1': {'reason': 'STOPPED'}}
        self.jwrite('CODEX_STATE.json', state)
        for lifecycle in ['STARTING', 'ACTIVE']:
            with self.subTest(lifecycle=lifecycle):
                self.control(revision=2, lifecycle=lifecycle)
                self.assertEqual(self.bridge.probe()['action'], 'BLOCKED')
                self.assertEqual(self.bridge.lifecycle_check()['action'], 'PAUSE_REQUIRED')
                self.assertNotEqual(self.bridge.claim('R0000', 1, 2)['action'], 'CLAIMED')
                self.assertFalse(self.bridge.lock.exists())
        self.control(revision=3, lifecycle='ACTIVE', run_id='fixture-run-2')
        self.signal(revision=2, run_id='fixture-run-2')
        self.assertEqual(self.bridge.lifecycle_check()['action'], 'ENABLE_ALLOWED')
        self.assertEqual(self.bridge.probe()['action'], 'CANDIDATE')
        self.assertIn('fixture-run-1', self.jread('CODEX_STATE.json')['closed_run_ids'])

    def test_connectivity_check_reads_only_small_bridge_json_once_per_run(self):
        self.control(lifecycle='STARTING')
        self.signal()
        allowed = {self.bridge.path(p) for p in ['operator/CONTROL.json', 'signals/claude_task.json',
                                               'CODEX_STATE.json', 'signals/codex_result.json']}
        preserved = {p: p.read_bytes() for p in allowed if p != self.bridge.path('CODEX_STATE.json')}
        observed = []
        original_read = Path.read_text
        def guarded(path, *args, **kwargs):
            path = Path(path)
            own_write_validation = (
                path.parent == self.bridge.path('connectivity') and
                re.fullmatch(r'\.fixture-run-1\.json\.[a-f0-9]{32}\.tmp', path.name)
            ) or (
                path.parent == self.bridge.path('') and
                re.fullmatch(r'\.CODEX_STATE\.json\.[a-f0-9]{32}\.tmp', path.name)
            ) or (
                path.parent == self.bridge.path('locks/codex_state.lock') and
                (path.name == 'owner.json' or
                 re.fullmatch(r'\.owner\.json\.[a-f0-9]{32}\.tmp', path.name))
            )
            self.assertTrue(path in allowed or own_write_validation,
                            f'Connectivity read unexpected file {path}')
            if path in allowed: observed.append(path)
            return original_read(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', guarded), \
             mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('Connectivity started a process')), \
             mock.patch.object(os, 'system', side_effect=AssertionError('Connectivity invoked a shell')):
            first = self.bridge.connectivity_check()
        self.assertEqual(first['action'], 'CONNECTIVITY_CHECKED')
        self.assertEqual(set(observed), allowed)
        state = self.jread('CODEX_STATE.json')
        self.assertEqual(state['connectivity_runs']['fixture-run-1']['status'], 'SUCCESS')
        self.assertIsNone(state['active_task'])
        original_state = self.bridge.path('CODEX_STATE.json').read_bytes()
        self.assertEqual(self.bridge.connectivity_check()['action'], 'CONNECTIVITY_DONE')
        self.assertEqual(self.bridge.path('CODEX_STATE.json').read_bytes(), original_state)
        for path, raw in preserved.items(): self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(self.bridge.lock.exists())
        self.assertEqual(list(self.bridge.path('results').iterdir()), [])
        # A new authorized run gets a new check while retaining the prior run's history.
        state = self.jread('CODEX_STATE.json')
        state['lifecycle'] = 'STOPPED'
        state['closed_run_ids'] = {'fixture-run-1': {'reason': 'STOPPED'}}
        self.jwrite('CODEX_STATE.json', state)
        self.control(revision=2, lifecycle='STARTING', run_id='fixture-run-2')
        self.signal(run_id='fixture-run-2')
        self.assertEqual(self.bridge.connectivity_check()['action'], 'CONNECTIVITY_CHECKED')
        self.assertEqual(set(self.jread('CODEX_STATE.json')['connectivity_runs']), {'fixture-run-1', 'fixture-run-2'})

    def test_connectivity_check_never_starts_from_other_lifecycles(self):
        self.signal()
        for lifecycle in ['IDLE', 'ACTIVE', 'STOPPING', 'STOPPED']:
            with self.subTest(lifecycle=lifecycle):
                self.control(lifecycle=lifecycle)
                before = self.bridge.path('CODEX_STATE.json').read_bytes()
                self.assertEqual(self.bridge.connectivity_check()['action'], 'NOT_STARTING')
                self.assertEqual(self.bridge.path('CODEX_STATE.json').read_bytes(), before)
                self.assertFalse(self.bridge.lock.exists())
                self.assertEqual(list(self.bridge.path('results').iterdir()), [])

    def test_concurrent_connectivity_checks_only_one_performs_the_check(self):
        self.control(lifecycle='STARTING'); self.signal()
        ctx = multiprocessing.get_context('spawn')
        ready, replies, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=connectivity_process,
                     args=(str(self.root), ready, start, replies)) for _ in range(4)]
        try:
            for process in processes: process.start()
            for _ in processes: ready.get(timeout=30)
            start.set()
            results = [replies.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(timeout=30)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all('test_error' not in result for result in results), results)
            self.assertEqual(sum(result.get('action') == 'CONNECTIVITY_CHECKED' for result in results), 1, results)
            # Windows path metadata can transiently fail closed during concurrent
            # publication; refusal is safe, but a second actual check is forbidden.
            self.assertTrue(all(result.get('action') in
                            {'CONNECTIVITY_CHECKED', 'CONNECTIVITY_DONE', 'BLOCKED'}
                            for result in results), results)
            for result in results:
                if result.get('action') == 'BLOCKED': self.assertTrue(result.get('reason'))
            self.assertEqual(self.jread('CODEX_STATE.json')['connectivity_runs']['fixture-run-1']['status'], 'SUCCESS')
            self.assertFalse(self.bridge.lock.exists())
            self.assertEqual(list(self.bridge.path('results').iterdir()), [])
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate(); process.join(timeout=10)
            for queue in [ready, replies]: queue.close(); queue.join_thread()

    def test_interrupted_connectivity_reservation_is_not_retried(self):
        self.control(lifecycle='STARTING'); self.signal()
        original_read = dispatcher.read_json
        def interrupted(path, *args, **kwargs):
            if Path(path) == self.bridge.path('signals/claude_task.json'):
                raise RuntimeError('simulated abrupt worker exit after reservation')
            return original_read(path, *args, **kwargs)
        with mock.patch.object(dispatcher, 'read_json', interrupted):
            with self.assertRaises(RuntimeError): self.bridge.connectivity_check()
        marker = self.bridge.path('connectivity/fixture-run-1.json')
        original = marker.read_bytes()
        self.assertEqual(json.loads(original)['status'], 'CHECKING')
        result = self.bridge.connectivity_check()
        self.assertEqual(result['action'], 'CONNECTIVITY_DONE')
        self.assertEqual(result['result']['status'], 'CHECKING')
        self.assertEqual(marker.read_bytes(), original)
        self.assertNotIn('fixture-run-1', self.jread('CODEX_STATE.json')['connectivity_runs'])
        self.assertFalse(self.bridge.lock.exists())

    def test_connectivity_check_cannot_overwrite_concurrent_end_state(self):
        self.control(lifecycle='STARTING'); self.signal()
        original_read = dispatcher.read_json
        end_observed = False
        def end_during_signal_read(path, *args, **kwargs):
            nonlocal end_observed
            if Path(path) == self.bridge.path('signals/claude_task.json') and not end_observed:
                end_observed = True
                # Another executor has completed the native PAUSED readback for END.
                self.control('STOP', revision=2, lifecycle='STOPPED')
                self.bridge.record_scheduler('PAUSED', self.native_config('PAUSED'))
                stopped = self.jread('CODEX_STATE.json')
                self.assertEqual(stopped['lifecycle'], 'STOPPED')
                self.assertIn('fixture-run-1', stopped['closed_run_ids'])
            return original_read(path, *args, **kwargs)
        with mock.patch.object(dispatcher, 'read_json', end_during_signal_read):
            result = self.bridge.connectivity_check()
        self.assertTrue(end_observed)
        self.assertNotEqual(result.get('result', {}).get('status'), 'SUCCESS')
        state = self.jread('CODEX_STATE.json')
        self.assertEqual(state['lifecycle'], 'STOPPED')
        self.assertEqual(state['status'], 'STOPPED')
        self.assertEqual(state['scheduler']['status'], 'PAUSED')
        self.assertIn('fixture-run-1', state['closed_run_ids'])
        self.assertGreaterEqual(state['last_seen_control_revision'], 2)
        self.assertIsNone(state['active_task'])
        self.assertFalse(self.bridge.lock.exists())
        self.assertEqual(self.bridge.probe()['action'], 'LIFECYCLE_STOP')
        # Restoring RUN cannot make this already-ended run eligible again.
        self.control(revision=3, lifecycle='ACTIVE')
        self.assertEqual(self.bridge.probe()['action'], 'BLOCKED')
        self.assertEqual(self.bridge.lifecycle_check()['action'], 'PAUSE_REQUIRED')
        self.assertNotEqual(self.bridge.claim('R0000', 1, 3, run_id='fixture-run-1')['action'], 'CLAIMED')

    def test_end_waits_for_final_connectivity_state_transaction_and_preserves_stop(self):
        self.control(lifecycle='STARTING'); self.signal()
        config = self.native_config('PAUSED')
        worker_entered, worker_finished = threading.Event(), threading.Event()
        worker_errors = []
        def end_worker():
            try:
                self.control('STOP', revision=2, lifecycle='STOPPED')
                worker_entered.set()
                self.bridge.record_scheduler('PAUSED', config)
            except BaseException as exc:
                worker_errors.append(repr(exc))
            finally:
                worker_finished.set()
        worker = threading.Thread(target=end_worker, name='fixture-end-during-state-write')
        original_write = dispatcher.write_json
        raced = False
        def stop_before_final_state_write(path, value, new=False):
            nonlocal raced
            if (Path(path) == self.bridge.path('CODEX_STATE.json') and
                    value.get('lifecycle') == 'STARTING' and
                    'fixture-run-1' in value.get('connectivity_runs', {}) and not raced):
                raced = True
                worker.start()
                self.assertTrue(worker_entered.wait(2), 'END worker did not reach the transaction')
                self.assertFalse(worker_finished.wait(0.05),
                                 'END bypassed the in-flight state transaction and can be overwritten')
            return original_write(path, value, new)
        try:
            with mock.patch.object(dispatcher, 'write_json', stop_before_final_state_write):
                self.bridge.connectivity_check()
            self.assertTrue(raced, 'The final connectivity state publication was not exercised')
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), 'END worker did not finish after transaction release')
            self.assertEqual(worker_errors, [])
        finally:
            if worker.ident is not None: worker.join(timeout=5)
        state = self.jread('CODEX_STATE.json')
        self.assertEqual(state['lifecycle'], 'STOPPED')
        self.assertEqual(state['status'], 'STOPPED')
        self.assertEqual(state['scheduler']['status'], 'PAUSED')
        self.assertIn('fixture-run-1', state['closed_run_ids'])
        self.assertGreaterEqual(state['last_seen_control_revision'], 2)
        self.assertFalse(self.bridge.lock.exists())
        self.assertEqual(self.bridge.probe()['action'], 'LIFECYCLE_STOP')

    def test_scheduler_readback_rejects_wrong_identity_status_or_environment(self):
        self.ready()
        original = self.bridge.path('CODEX_STATE.json').read_bytes()
        variants = [dict(id='different-schedule'), dict(kind='other'),
                    dict(execution_environment='cloud'), dict(cwds=[str(self.root.parent)]),
                    dict(status='PAUSED')]
        for overrides in variants:
            with self.subTest(overrides=overrides):
                config = self.native_config(**dict(dict(status='ACTIVE'), **overrides))
                config_bytes = config.read_bytes()
                with self.assertRaises(ValueError): self.bridge.record_scheduler('ACTIVE', config)
                self.assertEqual(self.bridge.path('CODEX_STATE.json').read_bytes(), original)
                self.assertEqual(config.read_bytes(), config_bytes)

    def test_active_scheduler_cannot_be_recorded_without_operator_start(self):
        original = self.bridge.path('CODEX_STATE.json').read_bytes()
        with self.assertRaises(ValueError):
            self.bridge.record_scheduler('ACTIVE', self.native_config('ACTIVE'))
        self.assertEqual(self.bridge.path('CODEX_STATE.json').read_bytes(), original)
        result = self.bridge.record_scheduler('PAUSED', self.native_config('PAUSED'))
        self.assertEqual(result['action'], 'SCHEDULER_RECORDED')
        self.assertEqual(self.jread('CODEX_STATE.json')['scheduler']['status'], 'PAUSED')
        self.assertEqual(self.jread('CODEX_STATE.json')['lifecycle'], 'IDLE')
        self.assertFalse(self.bridge.path('operator/CONTROL.json').exists())

    def test_recorded_pause_closes_run_but_does_not_erase_live_execution(self):
        self.ready(); execution = self.claim()
        self.control('STOP', revision=2, lifecycle='STOPPING')
        config = self.native_config('PAUSED'); original_config = config.read_bytes()
        result = self.bridge.record_scheduler('PAUSED', config)
        self.assertEqual(result['status'], 'PAUSED')
        state = self.jread('CODEX_STATE.json')
        self.assertEqual(state['lifecycle'], 'STOPPING')
        self.assertEqual(state['active_task']['execution_id'], execution)
        self.assertIn('fixture-run-1', state['closed_run_ids'])
        self.assertEqual(self.bridge.owner(execution)['execution_id'], execution)
        self.assertEqual(config.read_bytes(), original_config)
        self.assertEqual(self.jread('signals/codex_result.json')['revision'], 0)
        self.assertEqual(list(self.bridge.path('results').iterdir()), [])
        self.assertFalse(self.bridge.checkpoint(execution)['proceed'])

    def test_new_start_needs_new_run_and_revision_and_preserves_old_results(self):
        self.complete_bootstrap()
        old_report = self.bridge.path('results/R0000.md').read_bytes()
        self.control('STOP', revision=2, lifecycle='STOPPED')
        self.bridge.record_scheduler('PAUSED', self.native_config('PAUSED'))
        self.control(revision=3, run_id='fixture-run-1')
        with self.assertRaises(ValueError):
            self.bridge.record_scheduler('ACTIVE', self.native_config('ACTIVE'))
        self.control(revision=2, run_id='fixture-run-2')
        with self.assertRaises(ValueError):
            self.bridge.record_scheduler('ACTIVE', self.native_config('ACTIVE'))
        self.control(revision=3, run_id='fixture-run-2')
        self.assertEqual(self.bridge.record_scheduler('ACTIVE', self.native_config('ACTIVE'))['action'],
                         'SCHEDULER_RECORDED')
        state = self.jread('CODEX_STATE.json')
        self.assertEqual(state['run_id'], 'fixture-run-2')
        self.assertIn('R0000', state['completed_tasks'])
        self.assertEqual(self.bridge.path('results/R0000.md').read_bytes(), old_report)
        self.assertNotEqual(self.bridge.probe()['action'], 'CANDIDATE')
        self.signal(run_id='fixture-run-2')
        self.assertEqual(self.bridge.probe()['action'], 'DONE')
        self.assertNotEqual(self.bridge.claim('R0000', 1, 3, run_id='fixture-run-2')['action'], 'CLAIMED')

    def test_invalid_json_and_unknown_control_are_fail_closed(self):
        self.ready()
        for content in ['{bad json', '[]', '{"mode":"UNKNOWN","revision":1}',
                        '{"mode":"RUN","revision":true}']:
            with self.subTest(content=content):
                self.bridge.path('operator/CONTROL.json').write_text(content, encoding='utf-8')
                self.assertEqual(self.bridge.probe()['action'], 'BLOCKED')
                self.assertNotEqual(self.bridge.claim('R0000', 1, 1)['action'], 'CLAIMED')
                self.assertFalse(self.bridge.lock.exists())

    def test_unknown_signal_status_is_blocked(self):
        self.ready()
        value = self.jread('signals/claude_task.json'); value['status'] = 'SURPRISE'
        self.jwrite('signals/claude_task.json', value)
        self.assertEqual(self.bridge.probe()['action'], 'BLOCKED')

    def test_corrupt_state_and_result_types_are_blocked(self):
        self.ready()
        for filename, bad in [('CODEX_STATE.json', {'completed_tasks': []}),
                              ('signals/codex_result.json', {'status': 'BOGUS', 'revision': 0})]:
            original = self.jread(filename)
            try:
                self.jwrite(filename, bad)
                self.assertEqual(self.bridge.probe()['action'], 'BLOCKED', filename)
            finally:
                self.jwrite(filename, original)

    def test_malformed_completed_record_is_blocked_not_an_uncaught_error(self):
        self.ready('R0001')
        state = self.jread('CODEX_STATE.json')
        state['completed_tasks']['R0000'] = []
        self.jwrite('CODEX_STATE.json', state)
        self.assertEqual(self.bridge.probe()['action'], 'BLOCKED')

    def test_atomic_write_preserves_windows_line_endings(self):
        target = self.bridge.path('results/crlf-fixture.md')
        text = '# Windows input\r\n\r\nChinese text: \u6d4b\u8bd5\r\n'
        dispatcher.atomic_write(target, text, new=True)
        self.assertEqual(target.read_bytes(), text.encode('utf-8'))

    def test_invalid_json_temp_cannot_replace_previous_signal(self):
        target = self.bridge.path('signals/codex_result.json')
        original = target.read_bytes()
        with self.assertRaises(ValueError): dispatcher.atomic_write(target, '{invalid json')
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(target.parent.glob('.codex_result.json.*.tmp')), [])

    def test_idle_phase_reads_only_four_small_files(self):
        self.control()
        self.signal(None, revision=0, status='IDLE')
        allowed = {self.bridge.path(p) for p in ['operator/CONTROL.json', 'signals/claude_task.json',
                                               'CODEX_STATE.json', 'signals/codex_result.json']}
        observed = []
        original = Path.read_text
        def guarded(path, *args, **kwargs):
            path = Path(path)
            self.assertIn(path, allowed, f'Phase A unexpectedly read {path}')
            observed.append(path)
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', guarded), \
             mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('Phase A started a process')), \
             mock.patch.object(os, 'system', side_effect=AssertionError('Phase A invoked a shell')), \
             mock.patch.object(os, 'popen', side_effect=AssertionError('Phase A invoked a shell')):
            self.assertEqual(self.bridge.probe()['action'], 'NO_WORK')
        self.assertEqual(set(observed), allowed)
        self.assertFalse(self.bridge.lock.exists())
        self.assertEqual(list(self.bridge.path('results').iterdir()), [])

    def test_candidate_phase_does_not_invoke_git_or_other_processes(self):
        self.ready()
        with mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('Phase A started a process')), \
             mock.patch.object(os, 'system', side_effect=AssertionError('Phase A invoked a shell')), \
             mock.patch.object(os, 'popen', side_effect=AssertionError('Phase A invoked a shell')):
            self.assertEqual(self.bridge.probe()['action'], 'CANDIDATE')
        self.assertFalse(self.bridge.lock.exists())

    def test_control_modes_and_exact_pause_after_round(self):
        self.signal()
        for mode, effective, expected in [('STOP', None, 'CONTROL_STOP'),
                ('PAUSE_NOW', None, 'CONTROL_STOP'), ('PAUSE_AFTER_ROUND', None, 'CONTROL_STOP'),
                ('PAUSE_AFTER_ROUND', 'R0001', 'CONTROL_STOP'),
                ('PAUSE_AFTER_ROUND', 'R0000', 'CONTROL_STOP'), ('RUN', None, 'CANDIDATE')]:
            with self.subTest(mode=mode, effective=effective):
                self.control(mode, effective_task=effective)
                self.assertEqual(self.bridge.probe()['action'], expected)

    def test_signal_path_cannot_escape_task_directory(self):
        self.ready()
        value = self.jread('signals/claude_task.json')
        value['task_file'] = '../model.pt'
        self.jwrite('signals/claude_task.json', value)
        self.assertEqual(self.bridge.probe()['action'], 'BLOCKED')
        self.assertFalse(self.bridge.lock.exists())

    def test_oversized_small_state_is_rejected(self):
        self.ready()
        self.bridge.path('signals/claude_task.json').write_text(' ' * 131073, encoding='utf-8')
        self.assertEqual(self.bridge.probe()['action'], 'BLOCKED')

    def test_bootstrap_gate_and_round_cap(self):
        self.ready('R0001')
        self.assertEqual(self.bridge.probe()['action'], 'AWAITING_R0000')
        self.complete_bootstrap()
        self.signal('R0001', revision=2)
        self.assertEqual(self.bridge.probe()['action'], 'CANDIDATE')
        self.signal('R0009', revision=3)
        self.assertEqual(self.bridge.probe()['action'], 'LIMIT_REACHED')

    def test_missing_task_publishes_blocked_report_once(self):
        self.control(); self.signal(body=False)
        result = self.bridge.claim('R0000', 1, 1)
        self.assertEqual((result['action'], result['status']), ('PUBLISHED', 'BLOCKED'))
        raw = self.bridge.path('results/R0000.md').read_bytes()
        self.assert_provenance_fields(raw.decode('utf-8'))
        self.assertEqual(self.bridge.probe()['action'], 'DONE')
        self.assertNotEqual(self.bridge.claim('R0000', 1, 1)['action'], 'CLAIMED')
        self.assertEqual(self.bridge.path('results/R0000.md').read_bytes(), raw)

    def test_incomplete_task_cannot_start_engineering(self):
        self.control(); self.signal(body='Too short')
        result = self.bridge.claim('R0000', 1, 1)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertFalse(self.bridge.lock.exists())

    def test_empty_formal_task_sections_publish_blocked(self):
        self.complete_bootstrap()
        empty = '\n\n'.join('## ' + h for h in
                 ['Research Objective', 'Hypothesis', 'Methodology', 'Acceptance Criteria'])
        self.signal('R0001', revision=2, body=empty)
        result = self.bridge.claim('R0001', 2, 1)
        self.assertEqual((result['action'], result['status']), ('PUBLISHED', 'BLOCKED'))
        self.assertFalse(self.bridge.lock.exists())
        self.assertIn('Missing or empty task section', self.bridge.path('results/R0001.md').read_text())

    def test_existing_empty_lock_is_not_stolen(self):
        self.ready(); self.bridge.lock.mkdir()
        self.assertEqual(self.bridge.probe()['action'], 'BUSY')
        self.assertNotEqual(self.bridge.claim('R0000', 1, 1)['action'], 'CLAIMED')
        self.assertTrue(self.bridge.lock.is_dir())
        self.assertEqual(list(self.bridge.lock.iterdir()), [])

    def test_concurrent_processes_only_one_claims(self):
        self.ready()
        ctx = multiprocessing.get_context('spawn')
        ready, replies, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=claim_process,
                      args=(str(self.root), ready, start, replies)) for _ in range(4)]
        try:
            for process in processes: process.start()
            for _ in processes: ready.get(timeout=30)
            start.set()
            results = [replies.get(timeout=30) for _ in processes]
            for process in processes:
                process.join(timeout=30)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all('test_error' not in r for r in results), results)
            claimed = [r for r in results if r.get('action') == 'CLAIMED']
            self.assertEqual(len(claimed), 1, results)
            self.assertEqual(self.bridge.owner(claimed[0]['execution_id'])['task_id'], 'R0000')
            # The short helper processes are now dead. Their lock is still authoritative.
            self.assertEqual(self.bridge.probe()['action'], 'BUSY')
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate(); process.join(timeout=10)
            for queue in [ready, replies]: queue.close(); queue.join_thread()

    def test_control_changed_after_lock_acquisition_is_rechecked(self):
        self.ready()
        original_mkdir = os.mkdir
        def raced_mkdir(path, *args, **kwargs):
            result = original_mkdir(path, *args, **kwargs)
            if Path(path).resolve() == self.bridge.lock.resolve():
                self.control('STOP', revision=2)
            return result
        with mock.patch.object(dispatcher.os, 'mkdir', raced_mkdir):
            result = self.bridge.claim('R0000', 1, 1)
        self.assertEqual(result['action'], 'NOT_CLAIMED')
        self.assertFalse(self.bridge.lock.exists())
        self.assertIsNone(self.jread('CODEX_STATE.json')['active_task'])

    def test_signal_changed_after_lock_acquisition_is_rechecked(self):
        self.ready()
        original_mkdir = os.mkdir
        def raced_mkdir(path, *args, **kwargs):
            result = original_mkdir(path, *args, **kwargs)
            if Path(path).resolve() == self.bridge.lock.resolve():
                self.signal(None, revision=2, status='IDLE')
            return result
        with mock.patch.object(dispatcher.os, 'mkdir', raced_mkdir):
            result = self.bridge.claim('R0000', 1, 1)
        self.assertEqual(result['action'], 'NOT_CLAIMED')
        self.assertFalse(self.bridge.lock.exists())

    def test_claim_rejects_an_explicit_stale_run_id(self):
        self.ready()
        result = self.bridge.claim('R0000', 1, 1, run_id='fixture-previous-run')
        self.assertNotEqual(result['action'], 'CLAIMED')
        self.assertFalse(self.bridge.lock.exists())
        result = self.bridge.claim('R0000', 1, 1, run_id='fixture-run-1')
        self.assertEqual(result['action'], 'CLAIMED')
        self.assertEqual(self.bridge.owner(result['execution_id'])['run_id'], 'fixture-run-1')

    def test_run_id_changed_after_lock_acquisition_is_rechecked(self):
        self.ready()
        original_mkdir = os.mkdir
        def raced_mkdir(path, *args, **kwargs):
            result = original_mkdir(path, *args, **kwargs)
            if Path(path).resolve() == self.bridge.lock.resolve():
                # The run identity must be checked independently of revision counters.
                self.control(run_id='fixture-run-2')
                self.signal(run_id='fixture-run-2')
            return result
        with mock.patch.object(dispatcher.os, 'mkdir', raced_mkdir):
            result = self.bridge.claim('R0000', 1, 1, run_id='fixture-run-1')
        self.assertEqual(result['action'], 'NOT_CLAIMED')
        self.assertFalse(self.bridge.lock.exists())
        self.assertIsNone(self.jread('CODEX_STATE.json')['active_task'])

    def test_foreign_execution_cannot_modify_or_release_owner(self):
        self.ready(); execution = self.claim()
        original = self.bridge.path('locks/engineering.lock/owner.json').read_bytes()
        for operation in [lambda: self.bridge.checkpoint('foreign'),
                          lambda: self.bridge.finish('foreign', 'SUCCESS', self.report()),
                          lambda: self.bridge._release('foreign')]:
            with self.assertRaises(ValueError): operation()
        self.assertEqual(self.bridge.path('locks/engineering.lock/owner.json').read_bytes(), original)
        self.assertEqual(self.bridge.owner(execution)['execution_id'], execution)

    def test_checkpoint_preserves_running_process_until_explicit_clear(self):
        self.ready(); execution = self.claim()
        self.bridge.checkpoint(execution, running_experiment={'pid': 123456, 'description': 'fixture only'})
        self.bridge.checkpoint(execution, phase='between_steps')
        self.assertIsNotNone(self.bridge.owner(execution)['running_experiment'])
        with self.assertRaises(ValueError): self.bridge.finish(execution, 'SUCCESS', self.report())
        self.bridge.checkpoint(execution, running_experiment=None)
        self.assertIsNone(self.bridge.owner(execution)['running_experiment'])

    def test_pause_checkpoint_prevents_success_but_allows_blocked_result(self):
        self.ready(); execution = self.claim()
        self.control('PAUSE_NOW', revision=2)
        self.assertFalse(self.bridge.checkpoint(execution)['proceed'])
        with self.assertRaises(ValueError): self.bridge.finish(execution, 'SUCCESS', self.report())
        result = self.bridge.finish(execution, 'BLOCKED', self.report('BLOCKED'))
        self.assertEqual(result['status'], 'BLOCKED')

    def test_pause_after_round_only_allows_the_already_claimed_task(self):
        self.ready(); execution = self.claim()
        self.control('PAUSE_AFTER_ROUND', revision=2, effective_task='R0000')
        self.assertTrue(self.bridge.checkpoint(execution)['proceed'])
        self.assertEqual(self.bridge.probe()['action'], 'CONTROL_STOP')
        self.control('PAUSE_AFTER_ROUND', revision=3, effective_task='R0001')
        self.assertFalse(self.bridge.checkpoint(execution)['proceed'])

    def test_stop_is_latched_even_after_run_returns_with_newer_revision(self):
        self.ready(); execution = self.claim()
        self.control('STOP', revision=2)
        self.assertFalse(self.bridge.checkpoint(execution)['proceed'])
        self.control('RUN', revision=3)
        self.assertFalse(self.bridge.checkpoint(execution)['proceed'])
        with self.assertRaises(ValueError): self.bridge.finish(execution, 'SUCCESS', self.report())
        self.assertFalse(self.bridge.path('results/R0000.md').exists())
        self.assertEqual(self.bridge.finish(execution, 'BLOCKED', self.report('BLOCKED'))['status'], 'BLOCKED')

    def test_run_id_change_stops_old_execution_and_is_latched(self):
        self.ready(); execution = self.claim()
        self.assertEqual(self.bridge.owner(execution)['run_id'], 'fixture-run-1')
        self.control(revision=2, run_id='fixture-run-2')
        self.assertFalse(self.bridge.checkpoint(execution)['proceed'])
        self.control(revision=3, run_id='fixture-run-1')
        self.assertFalse(self.bridge.checkpoint(execution)['proceed'])
        with self.assertRaises(ValueError): self.bridge.finish(execution, 'SUCCESS', self.report())

    def test_leaving_active_lifecycle_stops_existing_execution(self):
        self.ready(); execution = self.claim()
        self.control(revision=2, lifecycle='STOPPING')
        self.assertFalse(self.bridge.checkpoint(execution)['proceed'])
        self.control(revision=3, lifecycle='ACTIVE')
        self.assertFalse(self.bridge.checkpoint(execution)['proceed'])
        with self.assertRaises(ValueError): self.bridge.finish(execution, 'SUCCESS', self.report())

    def test_finish_rechecks_stop_even_without_an_intermediate_checkpoint(self):
        self.ready(); execution = self.claim()
        self.control('STOP', revision=2)
        with self.assertRaises(ValueError): self.bridge.finish(execution, 'SUCCESS', self.report())
        self.assertFalse(self.bridge.path('results/R0000.md').exists())

    def test_checkpoint_rejects_control_revision_rollback_after_stop(self):
        self.ready(); execution = self.claim()
        self.control('STOP', revision=2)
        self.assertFalse(self.bridge.checkpoint(execution)['proceed'])
        self.control('RUN', revision=1)
        self.assertFalse(self.bridge.checkpoint(execution)['proceed'])
        # A rejected read must not lower the high-water mark and enable a later retry.
        self.assertFalse(self.bridge.checkpoint(execution)['proceed'])
        self.assertGreaterEqual(self.jread('CODEX_STATE.json')['last_seen_control_revision'], 2)

    def test_probe_rejects_control_revision_below_recorded_revision(self):
        self.ready()
        state = self.jread('CODEX_STATE.json')
        state['last_seen_control_revision'] = 2
        self.jwrite('CODEX_STATE.json', state)
        self.assertEqual(self.bridge.probe()['action'], 'BLOCKED')
        self.control('RUN', revision=2)
        self.assertEqual(self.bridge.probe()['action'], 'CANDIDATE')

    def test_mutated_original_task_requires_stop(self):
        self.ready(); execution = self.claim()
        self.bridge.path('tasks/R0000.md').write_text('A changed immutable task document.', encoding='utf-8')
        checked = self.bridge.checkpoint(execution)
        self.assertFalse(checked['proceed'])
        self.assertIn('changed', checked['reason'])

    def test_incomplete_report_does_not_publish_any_terminal_result(self):
        self.ready(); execution = self.claim()
        for report in ['## Metadata\n\nOnly one section.',
                       '\n\n'.join('## ' + h for h in dispatcher.SECTIONS)]:
            with self.assertRaises(ValueError): self.bridge.finish(execution, 'SUCCESS', report)
            self.assertFalse(self.bridge.path('results/R0000.md').exists())
            self.assertEqual(self.jread('signals/codex_result.json')['revision'], 0)

    def test_git_provenance_schema_has_all_required_fields(self):
        self.assertIn('Git Provenance', dispatcher.SECTIONS)
        self.assertEqual(dispatcher.PROVENANCE_FIELDS, PROVENANCE_FIELDS)
        self.assert_provenance_fields(self.report())

    def test_report_without_git_provenance_section_is_not_published(self):
        self.ready(); execution = self.claim()
        report = re.sub(r'^## Git Provenance\n.*?(?=^## |\Z)', '', self.report(), flags=re.M | re.S)
        with self.assertRaises(ValueError): self.bridge.finish(execution, 'SUCCESS', report)
        self.assertFalse(self.bridge.path('results/R0000.md').exists())
        self.assertEqual(self.jread('signals/codex_result.json')['revision'], 0)
        self.assertEqual(self.bridge.owner(execution)['execution_id'], execution)

    def test_subagent_summary_is_required_and_nonempty(self):
        self.ready(); execution = self.claim()
        self.assertEqual(dispatcher.SECTIONS[-1], 'Subagent Execution Summary')
        for replacement in ['', '## Subagent Execution Summary\n\n  \t\n']:
            report = re.sub(r'^## Subagent Execution Summary\n.*?(?=^## |\Z)',
                            lambda match: replacement, self.report(), flags=re.M | re.S)
            with self.assertRaises(ValueError):
                self.bridge.finish(execution, 'SUCCESS', report)
            self.assertFalse(self.bridge.path('results/R0000.md').exists())
            self.assertEqual(self.jread('signals/codex_result.json')['revision'], 0)

    def test_preflight_blocked_report_truthfully_has_zero_subagents(self):
        report = self.bridge.blocked_report('R0001', 'Fixture missing inputs')
        self.assertIn('## Subagent Execution Summary\n\n0 subagents.', report)

    def formal_execution(self):
        self.complete_bootstrap()
        self.signal('R0001', revision=2)
        result = self.bridge.claim('R0001', 2, 1, 'fixture-run-1')
        self.assertEqual(result['action'], 'CLAIMED', result)
        return result['execution_id']

    def test_subagent_check_is_read_only_and_never_launches_anything(self):
        execution = self.formal_execution()
        paths = ['CODEX_STATE.json', 'signals/codex_result.json', 'operator/CONTROL.json',
                 'locks/engineering.lock/owner.json']
        before = {p: self.bridge.path(p).read_bytes() for p in paths}
        with mock.patch('subprocess.Popen', side_effect=AssertionError('No processes in gate')), \
             mock.patch.object(dispatcher, 'write_json', side_effect=AssertionError('Gate is read only')):
            result = self.bridge.subagent_check(execution)
        self.assertEqual(result['action'], 'SUBAGENT_ALLOWED')
        self.assertEqual(result['max_concurrent_subagents'], 3)
        self.assertFalse(result['recursive_delegation'])
        self.assertEqual(result['task_id'], 'R0001')
        for p in paths:
            self.assertEqual(self.bridge.path(p).read_bytes(), before[p])

    def test_subagent_gate_rejects_no_parent_r0000_and_foreign_owner(self):
        self.control(lifecycle='IDLE', run_id=None)
        self.assertFalse(self.bridge.subagent_check('not-claimed')['proceed'])
        self.ready(); execution = self.claim()
        self.assertFalse(self.bridge.subagent_check(execution)['proceed'])
        self.assertFalse(self.bridge.subagent_check('foreign')['proceed'])

    def test_subagent_gate_blocks_inactive_lifecycle_and_pauses(self):
        execution = self.formal_execution()
        for life in ['IDLE', 'STARTING', 'STOPPING', 'STOPPED']:
            with self.subTest(lifecycle=life):
                self.control(lifecycle=life, revision=2)
                self.assertFalse(self.bridge.subagent_check(execution)['proceed'])
                self.assertFalse(self.bridge.subagent_check(execution, 'continue')['proceed'])
        for mode in ['STOP', 'PAUSE_NOW']:
            self.control(mode, revision=3)
            self.assertFalse(self.bridge.subagent_check(execution)['proceed'])
            self.assertFalse(self.bridge.subagent_check(execution, 'continue')['proceed'])

    def test_pause_after_round_allows_existing_subagent_wrapup_only(self):
        execution = self.formal_execution()
        self.control('PAUSE_AFTER_ROUND', revision=2, effective_task='R0001')
        self.assertFalse(self.bridge.subagent_check(execution, 'spawn')['proceed'])
        result = self.bridge.subagent_check(execution, 'continue')
        self.assertTrue(result['proceed'])
        self.assertEqual(result['scope'], 'wrap_up_only')
        self.control('PAUSE_AFTER_ROUND', revision=3, effective_task='R0002')
        self.assertFalse(self.bridge.subagent_check(execution, 'continue')['proceed'])

    def test_subagent_gate_checks_run_stop_latch_and_task_identity(self):
        execution = self.formal_execution()
        self.control(revision=2, run_id='different-run')
        self.assertFalse(self.bridge.subagent_check(execution)['proceed'])
        self.control(revision=3)
        task = self.bridge.path('tasks/R0001.md')
        original = task.read_bytes()
        task.write_bytes(original + b'\nchanged fixture')
        self.assertFalse(self.bridge.subagent_check(execution)['proceed'])
        task.write_bytes(original)
        owner = self.jread('locks/engineering.lock/owner.json')
        owner['stop_required'] = True
        self.jwrite('locks/engineering.lock/owner.json', owner)
        self.assertFalse(self.bridge.subagent_check(execution, 'continue')['proceed'])

    def test_each_missing_or_empty_git_provenance_field_prevents_publication(self):
        self.ready(); execution = self.claim()
        for label in PROVENANCE_FIELDS:
            for replacement in ['', '- ' + label + ':   \t']:
                with self.subTest(label=label, replacement=replacement):
                    report = re.sub(r'^- ' + re.escape(label) + r':[^\n]*$',
                                    lambda match: replacement, self.report(), flags=re.M)
                    with self.assertRaises(ValueError):
                        self.bridge.finish(execution, 'SUCCESS', report)
                    self.assertFalse(self.bridge.path('results/R0000.md').exists())
                    self.assertEqual(self.jread('signals/codex_result.json')['revision'], 0)
                    self.assertEqual(self.bridge.owner(execution)['execution_id'], execution)

    def test_provenance_fields_in_another_section_do_not_satisfy_requirement(self):
        self.ready(); execution = self.claim()
        report = self.report()
        section = re.search(r'^## Git Provenance\n(.*?)(?=^## |\Z)', report, re.M | re.S)
        fields = section[1]
        report = report[:section.start()] + '## Git Provenance\n\nFields omitted from this section.\n'
        report = report.replace('## Summary\n', '## Summary\n' + fields + '\n', 1)
        with self.assertRaises(ValueError): self.bridge.finish(execution, 'SUCCESS', report)
        self.assertFalse(self.bridge.path('results/R0000.md').exists())
        self.assertEqual(self.jread('signals/codex_result.json')['revision'], 0)

    def test_success_report_is_immutable_and_revision_does_not_enable_replay(self):
        self.ready(); execution = self.claim()
        result = self.bridge.finish(execution, 'SUCCESS', self.report())
        self.assertEqual(result['action'], 'PUBLISHED')
        path = self.bridge.path('results/R0000.md'); original = path.read_bytes()
        self.assertFalse(self.bridge.lock.exists())
        self.signal(revision=2)
        self.assertEqual(self.bridge.probe()['action'], 'DONE')
        self.assertNotEqual(self.bridge.claim('R0000', 2, 1)['action'], 'CLAIMED')
        with self.assertRaises(FileExistsError): dispatcher.atomic_write(path, 'replacement', new=True)
        self.assertEqual(path.read_bytes(), original)

    def test_report_without_signal_requires_recovery_not_retraining(self):
        self.ready(); execution = self.claim()
        original_write = dispatcher.write_json
        def fail_signal(path, value, new=False):
            if Path(path) == self.bridge.path('signals/codex_result.json'):
                raise OSError('simulated publication crash')
            return original_write(path, value, new)
        with mock.patch.object(dispatcher, 'write_json', fail_signal):
            with self.assertRaises(OSError): self.bridge.finish(execution, 'SUCCESS', self.report())
        raw = self.bridge.path('results/R0000.md').read_bytes()
        self.assertEqual(self.bridge.probe()['action'], 'RECOVERY_REQUIRED')
        self.assertNotEqual(self.bridge.claim('R0000', 1, 1)['action'], 'CLAIMED')
        with self.assertRaises(ValueError): self.bridge.recover_publication(execution, ' ')
        result = self.bridge.recover_publication(execution, 'Human fixture confirmation: no worker is active.')
        self.assertEqual(result['action'], 'PUBLISHED')
        self.assertEqual(self.bridge.path('results/R0000.md').read_bytes(), raw)
        self.assertFalse(self.bridge.lock.exists())

    def test_committed_result_can_recover_failed_lock_release(self):
        self.ready(); execution = self.claim()
        with mock.patch.object(self.bridge, '_release', side_effect=OSError('simulated cleanup crash')):
            with self.assertRaises(OSError): self.bridge.finish(execution, 'SUCCESS', self.report())
        self.assertIsNone(self.jread('CODEX_STATE.json')['active_task'])
        self.assertEqual(self.jread('signals/codex_result.json')['revision'], 1)
        raw = self.bridge.path('results/R0000.md').read_bytes()
        result = self.bridge.recover_publication(execution, 'Human fixture confirmation: old worker has stopped.')
        self.assertEqual(result['action'], 'PUBLISHED')
        self.assertEqual(self.jread('signals/codex_result.json')['revision'], 1)
        self.assertEqual(self.bridge.path('results/R0000.md').read_bytes(), raw)
        self.assertFalse(self.bridge.lock.exists())

    def test_active_state_without_lock_is_not_reclaimed(self):
        self.ready()
        state = self.jread('CODEX_STATE.json')
        state['active_task'] = {'task_id': 'R0000', 'execution_id': 'lost-owner'}
        state['status'] = 'RUNNING'
        self.jwrite('CODEX_STATE.json', state)
        self.assertEqual(self.bridge.probe()['action'], 'RECOVERY_REQUIRED')
        self.assertNotEqual(self.bridge.claim('R0000', 1, 1)['action'], 'CLAIMED')
        self.assertFalse(self.bridge.lock.exists())

    def test_report_and_state_conflict_does_not_overwrite_completed_record(self):
        self.ready(); execution = self.claim()
        state = self.jread('CODEX_STATE.json')
        state['completed_tasks']['R0000'] = {'status': 'FAILED', 'execution_id': 'different-owner'}
        self.jwrite('CODEX_STATE.json', state)
        with self.assertRaises(ValueError): self.bridge.finish(execution, 'SUCCESS', self.report())
        self.assertEqual(self.jread('CODEX_STATE.json')['completed_tasks']['R0000']['execution_id'], 'different-owner')


if __name__ == '__main__':
    unittest.main(verbosity=2)
