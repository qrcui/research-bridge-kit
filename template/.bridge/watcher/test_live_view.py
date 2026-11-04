"""Unit tests for live_view.py (standard library only). Run from this folder: python -m unittest test_live_view"""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import live_view as lv

NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def stamp(offset=0):
    return datetime.fromtimestamp(time.time() + offset, timezone.utc).isoformat().replace('+00:00', 'Z')


def codex_line(typ, payload, ts=None):
    return json.dumps(dict(timestamp=ts or stamp(), type=typ, payload=payload))


class TailTest(unittest.TestCase):
    def test_partial_lines_wait_for_their_newline(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, 'x.jsonl')
            f.write_bytes(b'{"a":1}\n{"b":')
            t = lv.Tail(f)
            self.assertEqual(t.read_lines(), ['{"a":1}'])
            self.assertEqual(t.read_lines(), [])
            with open(f, 'ab') as h:
                h.write('2}\n{"c":"中"}\n'.encode('utf-8'))
            self.assertEqual(t.read_lines(), ['{"b":2}', '{"c":"中"}'])

    def test_truncated_file_is_read_again(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, 'x.jsonl')
            f.write_text('one\ntwo\n')
            t = lv.Tail(f)
            self.assertEqual(t.read_lines(), ['one', 'two'])
            f.write_text('new\n')
            self.assertEqual(t.read_lines(), ['new'])

    def test_replay_limit_skips_the_cut_line(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, 'x.jsonl')
            f.write_text('aaaaaaaaaa\nbbbb\ncccc\n')          # 21 bytes
            t = lv.Tail(f, from_end_limit=12)                  # starts inside 'aaaa…': that line is dropped
            self.assertEqual(t.read_lines(), ['bbbb', 'cccc'])
            self.assertTrue(t.skipped_bytes > 0)
            t = lv.Tail(f, from_end_limit=10)                  # starts exactly at 'bbbb': nothing is dropped
            self.assertEqual(t.read_lines(), ['bbbb', 'cccc'])
            t = lv.Tail(f, from_end_limit=7)
            self.assertEqual(t.read_lines(), ['cccc'])

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(lv.Tail('/nonexistent/never/here.log').read_lines(), [])


class StoreTest(unittest.TestCase):
    def test_since_first_next_and_eviction(self):
        s = lv.Store(max_events=3)
        for i in range(5):
            s.add('codex', 'say', f'm{i}')
        page = s.since(0)
        self.assertEqual(page['first'], 2)
        self.assertEqual([e['text'] for e in page['events']], ['m2', 'm3', 'm4'])
        self.assertEqual(page['next'], 5)
        self.assertFalse(page['more'])
        self.assertEqual(s.since(5)['events'], [])
        self.assertIsNone(s.text(0))
        self.assertEqual(s.text(4), 'm4')

    def test_char_budget_and_lazy_text(self):
        s = lv.Store(max_events=100, max_chars=25_000)
        big = 'x' * 10_000
        for _ in range(4):
            s.add('codex', 'output', big)
        page = s.since(0)
        self.assertLessEqual(sum(e['size'] for e in page['events']), 25_000)
        self.assertIsNone(page['events'][-1]['text'])            # long output: fetched on demand
        self.assertEqual(s.text(page['events'][-1]['id']), big)
        s.add('codex', 'say', big)
        self.assertEqual(s.since(s.next_id - 1)['events'][0]['text'], big)   # messages are always inline

    def test_paging(self):
        s = lv.Store()
        for i in range(lv.PAGE_EVENTS + 5):
            s.add('claude', 'say', str(i))
        p1 = s.since(0)
        self.assertTrue(p1['more'])
        p2 = s.since(p1['next'])
        self.assertEqual(len(p2['events']), 5)
        self.assertFalse(p2['more'])


class CodexParseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = lv.Store()
        f = Path(self.tmp.name, 'r.jsonl')
        f.write_text('')
        self.src = lv.CodexSource(self.store, f, 'Codex', 'root', replay=False)

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        return self.store.since(0)['events']

    def test_rows(self):
        h = self.src.handle
        h(codex_line('response_item', dict(type='message', role='assistant', content=[dict(type='output_text', text='你好')])))
        h(codex_line('response_item', dict(type='message', role='user', content=[dict(type='input_text', text='ignored')])))
        h(codex_line('response_item', dict(type='reasoning', summary=[dict(type='summary_text', text='thinking')])))
        h(codex_line('response_item', dict(type='function_call', name='shell', call_id='c1',
                                           arguments=json.dumps(dict(command=['bash', '-lc', 'ls -la'])))))
        h(codex_line('response_item', dict(type='function_call_output', call_id='c1',
                                           output=json.dumps(dict(output='file1\nfile2', metadata={})))))
        h(codex_line('response_item', dict(type='custom_tool_call', name='apply_patch', call_id='c2',
                                           input='*** Begin Patch\n*** Update File: src/a.py\n*** Add File: b.py\n')))
        h(codex_line('event_msg', dict(type='task_complete')))
        h('not json')
        rows = self.rows()
        self.assertEqual([r['kind'] for r in rows], ['say', 'think', 'run', 'output', 'run', 'done'])
        self.assertEqual(rows[0]['text'], '你好')
        self.assertEqual(rows[2]['summary'], 'ls -la')
        self.assertTrue(rows[3]['summary'].startswith('ls -la → file1'))
        self.assertEqual(rows[3]['text'], 'file1\nfile2')
        self.assertEqual(rows[4]['summary'], '修改 a.py，新建 b.py')
        self.assertTrue(all(r['src'] == 'Codex' for r in rows))
        self.assertIsNone(self.src.status())                     # done

    def test_status_counts_up_while_a_tool_runs(self):
        self.src.handle(codex_line('response_item', dict(type='function_call', name='exec_command', call_id='x',
                                                          arguments=json.dumps(dict(cmd='sleep 60'))), ts=stamp(-5)))
        st = self.src.status()
        self.assertEqual(st['what'], 'tool')
        self.assertGreaterEqual(st['secs'], 4)

    def test_naive_timestamps_do_not_crash(self):
        self.src.handle(json.dumps(dict(timestamp='2026-01-01T10:00:00', type='response_item',
                                        payload=dict(type='reasoning', summary=[]))))
        self.src.status()

    def test_labels(self):
        self.assertEqual(lv.codex_label(dict(source='exec')), ('Codex', 'root'))
        self.assertEqual(lv.codex_label(dict(source='cli'))[1], 'interactive')
        label, role = lv.codex_label(dict(id='abcdef123456', source={'subagent': {'thread_spawn': {'depth': 1}}}))
        self.assertEqual(role, 'sub')
        self.assertIn('abcdef12', label)


class ClaudeParseTest(unittest.TestCase):
    def test_rows_and_subagent_label(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, 'c.log')
            f.write_text('')
            store = lv.Store()
            src = lv.ClaudeSource(store, f, replay=True)
            src.handle(json.dumps(dict(type='system', subtype='init')))
            src.handle(json.dumps(dict(type='assistant', message=dict(content=[
                dict(type='thinking', thinking='hmm'), dict(type='text', text='plan'),
                dict(type='tool_use', id='t1', name='Read', input=dict(file_path='/x/y.py'))]))))
            src.handle(json.dumps(dict(type='user', message=dict(content=[
                dict(type='tool_result', tool_use_id='t1', content=[dict(type='text', text='line1')])]))))
            src.handle(json.dumps(dict(type='assistant', parent_tool_use_id='t9',
                                       message=dict(content=[dict(type='text', text='from child')]))))
            src.handle(json.dumps(dict(type='user', message=dict(content='plain string content'))))
            src.handle(json.dumps(dict(type='result', subtype='success', result='all done')))
            rows = store.since(0)['events']
            self.assertEqual([r['kind'] for r in rows], ['think', 'say', 'run', 'output', 'say', 'done'])
            self.assertEqual(rows[2]['summary'], 'Read: /x/y.py')
            self.assertEqual(rows[3]['summary'], 'Read: /x/y.py → line1')
            self.assertEqual(rows[4]['src'], 'Claude 子代理')
            self.assertEqual(rows[0]['time'], '')                # replayed rows carry no time


class FollowerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / 'Proj'
        (self.root / '.bridge' / 'watcher' / 'logs').mkdir(parents=True)
        self.sessions = base / 'codex' / 'sessions'
        now = datetime.now().astimezone()
        self.day = self.sessions / f'{now.year:04d}' / f'{now.month:02d}' / f'{now.day:02d}'
        self.day.mkdir(parents=True)
        self.store = lv.Store()
        self.f = lv.Follower(self.root, self.store, lv.Log(base / 'lv.log'), self.sessions)

    def tearDown(self):
        self.tmp.cleanup()

    def rollout(self, name, cwd, source='exec', extra=(), sid='0123456789ab'):
        path = self.day / name
        lines = [codex_line('session_meta', dict(id=sid, cwd=str(cwd), source=source))] + list(extra)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return path

    def say(self, text):
        return codex_line('response_item', dict(type='message', role='assistant', content=[dict(type='output_text', text=text)]))

    def texts(self):
        return [(e['src'], e['text']) for e in self.store.since(0)['events'] if e['kind'] == 'say']

    def test_follows_root_and_subagent_and_ignores_other_projects(self):
        self.rollout('rollout-2026-01-01T10-00-00-a.jsonl', self.root, extra=[self.say('root says')])
        self.rollout('rollout-2026-01-01T10-00-01-b.jsonl', self.root.parent / 'Other', extra=[self.say('other')])
        child = self.rollout('rollout-2026-01-01T10-00-02-c.jsonl', str(self.root).upper() if os.name == 'nt' else self.root,
                             source={'subagent': {}}, sid='cafebabe0000', extra=[self.say('child says')])
        self.f.discover(); self.f.read_all()
        got = self.texts()
        self.assertIn(('Codex', 'root says'), got)
        self.assertIn(('Codex 子代理 cafebabe', 'child says'), got)
        self.assertNotIn('other', [t for _, t in got])
        # The root keeps writing after the child finished: still followed (the old viewer lost it here).
        with open(self.day / 'rollout-2026-01-01T10-00-00-a.jsonl', 'a', encoding='utf-8') as h:
            h.write(self.say('root again') + '\n')
        with open(child, 'a', encoding='utf-8') as h:
            h.write(codex_line('event_msg', dict(type='task_complete')) + '\n')
        self.f.read_all()
        self.assertEqual(self.texts()[-1], ('Codex', 'root again'))

    def test_new_session_after_start_is_picked_up(self):
        self.f.discover(); self.f.read_all()
        self.assertEqual(self.texts(), [])
        p = self.day / 'rollout-2026-01-01T11-00-00-n.jsonl'
        p.write_text(codex_line('session_meta', dict(id='x', cwd=str(self.root), source='exec'))[:20])  # half a line
        self.f.discover()
        self.assertEqual(self.f.codex, {})
        self.rollout(p.name, self.root, extra=[self.say('late')])
        self.f.discover(); self.f.read_all()
        self.assertEqual(self.texts(), [('Codex', 'late')])

    def test_old_finished_session_is_shown_once_at_startup(self):
        p = self.rollout('rollout-2025-12-01T10-00-00-o.jsonl', self.root, extra=[self.say('old run')])
        old = time.time() - 3 * 3600
        os.utime(p, (old, old))
        self.f.started = time.time()
        self.f.discover(); self.f.read_all()
        self.assertEqual(self.texts(), [('Codex', 'old run')])

    def test_claude_current_run_from_watcher_state(self):
        logs = self.root / '.bridge' / 'watcher' / 'logs'
        cur = logs / '20260101-120000-claude'
        Path(str(cur) + '.out.log').write_text(json.dumps(dict(type='assistant', message=dict(content=[
            dict(type='text', text='lead')]))) + '\n')
        (self.root / '.bridge' / 'watcher' / 'WATCHER_STATE.json').write_text(json.dumps(dict(
            status='RUNNING', heartbeat_at=lv.now_iso(), agents=dict(claude=dict(running=True, log_base=str(cur)),
                                                                   codex=dict(running=False)))))
        self.f.discover(); self.f.read_all()
        self.assertEqual(self.texts(), [('Claude', 'lead')])
        st = self.f.status()
        self.assertTrue(st['watcher'] and st['claude'])
        self.assertFalse(st['codex'])
        nxt = logs / '20260101-130000-claude.out.log'                 # the next run: a new file
        nxt.write_text(json.dumps(dict(type='assistant', message=dict(content=[dict(type='text', text='next')]))) + '\n')
        self.f.discover(); self.f.read_all()
        self.assertEqual(self.texts()[-1], ('Claude', 'next'))
        self.assertNotEqual(self.store.since(0)['events'][-1]['time'], '')   # live rows carry a time

    def test_a_broken_source_never_stops_the_others(self):
        self.rollout('rollout-2026-01-01T10-00-00-a.jsonl', self.root, extra=['{"type": "response_item", "payload": 5}',
                                                                              self.say('still here')])
        self.f.discover(); self.f.read_all()
        self.assertEqual(self.texts(), [('Codex', 'still here')])


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, 'P')
        (self.root / '.bridge' / 'watcher' / 'logs').mkdir(parents=True)
        self.port = free_port()
        self.v = lv.Viewer(self.root, self.port, codex_sessions=Path(self.tmp.name, 'none'))
        self.assertIsNone(self.v.bind())
        self.v.store.add('codex', 'say', 'hello')
        self.v.store.add('codex', 'output', 'y' * (lv.INLINE_CHARS + 1))
        lv.write_state(self.root, dict(port=self.v.port, pid=os.getpid(), instance=self.v.instance, root=str(self.root)))
        self.thread = threading.Thread(target=self.v.httpd.serve_forever, kwargs=dict(poll_interval=0.1), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.v.httpd.shutdown()
        self.v.httpd.server_close()
        self.tmp.cleanup()

    def get(self, path, host=None):
        req = urllib.request.Request(f'http://127.0.0.1:{self.v.port}{path}')
        if host:
            req.add_header('Host', host)
        with NO_PROXY.open(req, timeout=5) as r:
            return r.status, r.read()

    def test_endpoints(self):
        code, body = self.get('/health')
        h = json.loads(body)
        self.assertEqual((code, h['instance']), (200, self.v.instance))
        page = json.loads(self.get('/events?since=0')[1])
        self.assertEqual(page['instance'], self.v.instance)
        self.assertEqual([e['text'] for e in page['events']][0], 'hello')
        self.assertIsNone(page['events'][1]['text'])
        self.assertIn('status', page)
        text = json.loads(self.get(f"/text?id={page['events'][1]['id']}")[1])['text']
        self.assertEqual(len(text), lv.INLINE_CHARS + 1)
        self.assertIn(b'<html', self.get('/')[1])
        self.assertEqual(self.get('/events?since=abc')[0], 200)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get('/text?id=999')
        self.assertEqual(e.exception.code, 404)

    def test_foreign_host_header_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get('/events', host='evil.example:%d' % self.v.port)
        self.assertEqual(e.exception.code, 403)
        self.assertEqual(self.get('/health', host=f'localhost:{self.v.port}')[0], 200)

    def test_second_viewer_for_the_same_project_does_not_start(self):
        other = lv.Viewer(self.root, self.v.port)
        found = other.bind()
        self.assertEqual(found['instance'], self.v.instance)
        self.assertIsNone(other.httpd)
        self.assertEqual(lv.running(self.root)['instance'], self.v.instance)

    def test_another_project_gets_the_next_port(self):
        root2 = Path(self.tmp.name, 'Q')
        (root2 / '.bridge' / 'watcher').mkdir(parents=True)
        other = lv.Viewer(root2, self.v.port)
        try:
            self.assertIsNone(other.bind())
            self.assertNotEqual(other.port, self.v.port)
        finally:
            if other.httpd:
                other.httpd.server_close()


if __name__ == '__main__':
    unittest.main()
