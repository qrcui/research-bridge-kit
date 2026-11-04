#!/usr/bin/env python3
"""Read-only live view of the two bridge agents (protocol V3.0), standard library only, Python 3.12+.

  live_view.py serve  [--port 8765]           run the viewer in the foreground (the watcher starts it hidden)
  live_view.py ensure [--port 8765]           start a hidden viewer unless a healthy one already serves this project
  live_view.py open   [--port 8765] [--no-browser]   ensure, then open the page in the default browser
  live_view.py stop                           stop this project's viewer
  live_view.py status                         print this project's viewer address, if it is running

Sources (opened for reading only, never written):
  Claude  .bridge/watcher/logs/<stamp>-claude.out.log; the current run is WATCHER_STATE.json agents.claude.log_base
  Codex   $CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl (default ~/.codex) whose session_meta cwd is this
          project; every such file that is still being written is followed, labelled root / subagent / interactive.

The viewer listens on 127.0.0.1 only, answers only requests addressed to localhost, and writes nothing but
.bridge/watcher/LIVE_VIEW.json and .bridge/watcher/logs/live_view.log. It never touches bridge state.
"""
import argparse
import collections
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DEFAULT_PORT = 8765
PORT_SPAN = 20
STATE_NAME = 'LIVE_VIEW.json'

MAX_EVENTS = 5000                 # events kept in memory
MAX_STORED_CHARS = 48_000_000     # total text kept in memory (oldest events are dropped first)
TEXT_CAP = 200_000                # one event's text; the agent's own log keeps everything
INLINE_CHARS = 4000               # longer command/output texts are fetched only when a row is opened
PAGE_EVENTS = 300                 # events per /events response
REPLAY_BYTES = 16 * 1024 * 1024   # a file found at startup is replayed from at most its last 16 MB
MAX_LINE_BYTES = 32 * 1024 * 1024 # a longer unfinished line is skipped instead of buffered forever
ACTIVE_SECONDS = 15 * 60          # a rollout modified this recently counts as live
IDLE_DROP_SECONDS = 30 * 60       # stop following a file after this long without a write
STATUS_HIDE_SECONDS = 600
TICK_SECONDS = 1.0
DISCOVER_EVERY = 3                # ticks between source discovery passes


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def clock(stamp=None):
    """Local HH:MM:SS for an ISO timestamp, or for now."""
    if stamp:
        try:
            return datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).astimezone().strftime('%H:%M:%S')
        except ValueError:
            pass
    return datetime.now().strftime('%H:%M:%S')


def parse_time(stamp):
    """Aware datetime for an ISO timestamp (a naive one is taken as local time), or None."""
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.astimezone()


def norm_path(p):
    """Comparable form of a path written on Windows, macOS or Linux."""
    s = str(p or '').strip()
    if s.startswith('\\\\?\\'):
        s = s[4:]
    s = s.replace('\\', '/').rstrip('/')
    if os.name == 'nt' or sys.platform == 'darwin' or (len(s) > 1 and s[1] == ':'):
        s = s.lower()
    return s


def cap(text, limit=TEXT_CAP):
    text = '' if text is None else str(text)
    if len(text) > limit:
        return text[:limit] + '\n… (truncated for display; the full text is in the agent log)'
    return text.rstrip()


def shorten(text, n=160):
    line = next((l.strip() for l in str(text or '').splitlines() if l.strip()), '')
    return line if len(line) <= n else line[:n] + '…'


class Log:
    """Rate-limited diagnostics in logs/live_view.log; the same message is written at most once a minute."""

    def __init__(self, path):
        self.path = path
        self.seen = {}
        self.lock = threading.Lock()

    def write(self, msg, key=None):
        key = key or msg
        t = time.monotonic()
        with self.lock:
            if t - self.seen.get(key, -1e9) < 60:
                return
            self.seen[key] = t
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, 'a', encoding='utf-8') as f:
                    f.write(f'{now_iso()} {msg}\n')
            except OSError:
                pass


# ---------------------------------------------------------------- event store

class Store:
    """Bounded, thread-safe event list with stable ids, so a page can ask for everything after an id."""

    def __init__(self, max_events=MAX_EVENTS, max_chars=MAX_STORED_CHARS):
        self.events = collections.deque()
        self.next_id = 0
        self.chars = 0
        self.max_events = max_events
        self.max_chars = max_chars
        self.lock = threading.Lock()

    def add(self, agent, kind, text='', summary='', time_='', src=''):
        text = cap(text)
        with self.lock:
            e = dict(id=self.next_id, time=time_, agent=agent, src=src or agent, kind=kind,
                     text=text, summary=str(summary or ''))
            self.next_id += 1
            self.events.append(e)
            self.chars += len(text)
            while self.events and (len(self.events) > self.max_events or self.chars > self.max_chars):
                self.chars -= len(self.events.popleft()['text'])
            return e['id']

    def first_id(self):
        with self.lock:
            return self.events[0]['id'] if self.events else self.next_id

    def since(self, since, limit=PAGE_EVENTS):
        with self.lock:
            first = self.events[0]['id'] if self.events else self.next_id
            start = max(since, first) - first
            out = []
            for i in range(start, min(len(self.events), start + limit)):
                e = self.events[i]
                row = {k: v for k, v in e.items() if k != 'text'}
                row['size'] = len(e['text'])
                lazy = e['kind'] in ('run', 'output') and len(e['text']) > INLINE_CHARS
                row['text'] = None if lazy else e['text']
                out.append(row)
            nxt = out[-1]['id'] + 1 if out else max(since, first)
            return dict(first=first, next=min(nxt, self.next_id), total=self.next_id,
                        more=nxt < self.next_id, events=out)

    def text(self, eid):
        with self.lock:
            if not self.events:
                return None
            i = eid - self.events[0]['id']
            if 0 <= i < len(self.events):
                return self.events[i]['text']
            return None


# ---------------------------------------------------------------- following files

class Tail:
    """Reads complete new lines of a file that another process keeps appending to."""

    def __init__(self, path, from_end_limit=None):
        self.path = Path(path)
        self.pos = 0
        self.buf = b''
        self.skipping = False
        self.last_growth = time.time()
        self.skipped_bytes = 0
        self.caught_up = False
        if from_end_limit is not None:
            try:
                size = self.path.stat().st_size
            except OSError:
                size = 0
            if size > from_end_limit:
                self.pos = size - from_end_limit
                self.skipped_bytes = self.pos
                try:
                    with open(self.path, 'rb') as f:
                        f.seek(self.pos - 1)
                        self.skipping = f.read(1) != b'\n'   # drop the first line unless it starts here
                except OSError:
                    self.skipping = True

    def read_lines(self, max_bytes=8 * 1024 * 1024):
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        if size < self.pos:               # truncated or replaced: start again
            self.pos, self.buf, self.skipping = 0, b'', False
        if size == self.pos:
            self.caught_up = True
            return []
        with open(self.path, 'rb') as f:
            f.seek(self.pos)
            chunk = f.read(min(size - self.pos, max_bytes))
        self.pos += len(chunk)
        self.caught_up = self.pos >= size
        self.last_growth = time.time()
        data = self.buf + chunk
        parts = data.split(b'\n')
        self.buf = parts.pop()
        if self.skipping:
            if parts:
                parts.pop(0)
                self.skipping = False
            else:
                self.buf = b''
        if len(self.buf) > MAX_LINE_BYTES:
            self.buf = b''
            self.skipping = True
        return [p.decode('utf-8', errors='replace').rstrip('\r') for p in parts if p.strip()]


def first_line(path, limit=4 * 1024 * 1024):
    """The complete first line of a file, or None while it is still being written."""
    try:
        with open(path, 'rb') as f:
            data = f.read(limit)
    except OSError:
        return None
    nl = data.find(b'\n')
    if nl < 0:
        return None
    return data[:nl].decode('utf-8', errors='replace')


# ---------------------------------------------------------------- Codex rollout rows

def command_text(cmd):
    if isinstance(cmd, list):
        parts = [str(x) for x in cmd]
        if len(parts) >= 3 and parts[-2] in ('-lc', '-c', '/c', '-Command', '-command'):
            return parts[-1]
        return ' '.join(parts)
    return str(cmd or '')


def patch_summary(text):
    names = {'Add': '新建', 'Update': '修改', 'Delete': '删除'}
    files = []
    for line in str(text or '').splitlines():
        if line.startswith('*** ') and ' File: ' in line:
            verb, _, name = line[4:].partition(' File: ')
            files.append(names.get(verb.strip(), verb.strip()) + ' ' + name.strip().replace('\\', '/').split('/')[-1])
    return '，'.join(files[:6]) + ('…' if len(files) > 6 else '')


def output_text(x):
    if isinstance(x, str):
        s = x.strip()
        if s.startswith('{'):
            try:
                j = json.loads(s)
            except ValueError:
                return x
            if isinstance(j, dict) and 'output' in j:
                return output_text(j['output'])
        return x
    if isinstance(x, list):
        return '\n'.join(output_text(i) for i in x)
    if isinstance(x, dict):
        if 'text' in x:
            return str(x['text'])
        if 'content' in x:
            return output_text(x['content'])
        if 'output' in x:
            return output_text(x['output'])
        return json.dumps(x, ensure_ascii=False)
    return '' if x is None else str(x)


def call_view(name, args):
    """(text, summary) for a Codex function call whose arguments are a JSON string."""
    parsed = args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except ValueError:
            parsed = args
    summary = ''
    if isinstance(parsed, dict):
        for key in ('cmd', 'command'):
            if key in parsed:
                summary = command_text(parsed[key])
                break
        body = json.dumps(parsed, ensure_ascii=False, indent=2)
    else:
        body = str(parsed or '')
    if summary:
        return f'{summary}\n\n{name} {body}', shorten(summary)
    return f'{name} {body}', name


def codex_label(meta):
    """Label for a rollout from its session_meta payload."""
    source = meta.get('source')
    blob = json.dumps(source, ensure_ascii=False).lower() if source is not None else ''
    sid = str(meta.get('id') or '')[:8]
    nick = meta.get('agent_nickname') or meta.get('agent_role') or ''
    if 'subagent' in blob or meta.get('parent_thread_id') or meta.get('forked_from_id'):
        return 'Codex 子代理' + (f' {nick}' if nick else (f' {sid}' if sid else '')), 'sub'
    if isinstance(source, str) and source.lower() not in ('exec', ''):
        return f'Codex 交互会话（{source}）', 'interactive'
    return 'Codex', 'root'


class CodexSource:
    def __init__(self, store, path, label, role, replay):
        self.store, self.label, self.role = store, label, role
        self.tail = Tail(path, REPLAY_BYTES if replay else None)
        self.last = None          # datetime of the last row
        self.kind = None          # payload type of the last response item, or 'done'
        self.calls = {}           # call_id -> command summary, to label its output

    def emit(self, kind, text, summary='', t=''):
        self.store.add('codex', kind, text, summary, t, self.label)

    def handle(self, line):
        try:
            o = json.loads(line)
        except ValueError:
            return
        if not isinstance(o, dict):
            return
        p = o.get('payload') if isinstance(o.get('payload'), dict) else {}
        t = clock(o.get('timestamp'))
        when = parse_time(o.get('timestamp'))
        prev = self.last
        if when and not (o.get('type') == 'event_msg' and p.get('type') == 'item_completed'):
            self.last = when
        if o.get('type') == 'event_msg' and p.get('type') in ('task_complete', 'turn_aborted'):
            self.kind = 'done'
            if p.get('type') == 'task_complete':
                self.emit('done', '本轮完成', '', t)
            else:
                self.emit('done', '本轮已中止' + (f"：{p['reason']}" if p.get('reason') else ''), '', t)
            return
        if o.get('type') != 'response_item':
            return
        kind = p.get('type')
        self.kind = kind
        if kind == 'message':
            if p.get('role') == 'assistant':
                text = '\n'.join(str(c.get('text', '')) for c in p.get('content') or [] if isinstance(c, dict))
                if text.strip():
                    self.emit('say', text, '', t)
        elif kind == 'reasoning':
            took = f'{round((when - prev).total_seconds())}s' if (when and prev) else '?s'
            text = '\n'.join(str(c.get('text', '')) for c in p.get('summary') or [] if isinstance(c, dict))
            self.emit('think', text, took, t)
        elif kind == 'function_call':
            text, summary = call_view(p.get('name', ''), p.get('arguments'))
            self.calls[p.get('call_id')] = summary
            self.emit('run', text, summary, t)
        elif kind == 'custom_tool_call':
            raw = p.get('input', '')
            summary = patch_summary(raw) or f"{p.get('name', '')}: {shorten(raw, 120)}"
            self.calls[p.get('call_id')] = summary
            self.emit('run', f"{p.get('name', '')}\n{raw}", summary, t)
        elif kind == 'local_shell_call':
            action = p.get('action') or {}
            summary = shorten(command_text(action.get('command')))
            self.calls[p.get('call_id')] = summary
            self.emit('run', command_text(action.get('command')), summary, t)
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            text = output_text(p.get('output'))
            about = self.calls.pop(p.get('call_id'), '')
            summary = (f'{about} → ' if about else '') + (shorten(text, 100) or '（无内容）')
            self.emit('output', text, summary, t)
        elif kind == 'web_search_call':
            q = (p.get('action') or {}).get('query', '')
            self.emit('run', f'web_search {q}', f'网页搜索：{q}' if q else '网页搜索', t)

    def status(self):
        if not self.last or self.kind == 'done':
            return None
        secs = max(0, int((datetime.now(timezone.utc) - self.last).total_seconds()))
        if secs > STATUS_HIDE_SECONDS:
            return None
        what = 'tool' if self.kind in ('custom_tool_call', 'function_call', 'local_shell_call') else 'model'
        return dict(what=what, secs=secs, src=self.label)


# ---------------------------------------------------------------- Claude stream-json rows

def tool_use_summary(c):
    inp = c.get('input') if isinstance(c.get('input'), dict) else {}
    what = next((inp[k] for k in ('description', 'file_path', 'pattern', 'command', 'url', 'query', 'prompt')
                 if isinstance(inp.get(k), str) and inp[k].strip()), '')
    name = str(c.get('name', ''))
    return f'{name}: {shorten(what, 120)}' if what else name


class ClaudeSource:
    def __init__(self, store, path, replay):
        self.store = store
        self.replay = replay      # rows replayed at startup get no time: stream-json carries none
        self.tail = Tail(path, REPLAY_BYTES if replay else None)
        self.calls = {}

    def handle(self, line):
        try:
            o = json.loads(line)
        except ValueError:
            return
        if not isinstance(o, dict):
            return
        t = clock(o['timestamp']) if o.get('timestamp') else ('' if self.replay else clock())
        label = 'Claude 子代理' if o.get('parent_tool_use_id') else 'Claude'
        msg = o.get('message') if isinstance(o.get('message'), dict) else {}
        content = msg.get('content')
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            typ = c.get('type')
            if typ == 'text' and str(c.get('text', '')).strip():
                self.store.add('claude', 'say', c['text'], '', t, label)
            elif typ == 'thinking':
                self.store.add('claude', 'think', c.get('thinking', ''), '', t, label)
            elif typ == 'tool_use':
                summary = tool_use_summary(c)
                self.calls[c.get('id')] = summary
                body = json.dumps(c.get('input'), ensure_ascii=False, indent=2)
                self.store.add('claude', 'run', f"{c.get('name', '')} {body}", summary, t, label)
            elif typ == 'tool_result':
                text = output_text(c.get('content'))
                about = self.calls.pop(c.get('tool_use_id'), '')
                flag = '（出错）' if c.get('is_error') else ''
                summary = (f'{about} → ' if about else '') + flag + (shorten(text, 100) or '（无内容）')
                self.store.add('claude', 'output', text, summary, t, label)
        if o.get('type') == 'result':
            self.store.add('claude', 'done', o.get('result') or o.get('subtype') or '运行结束', '', t, label)


# ---------------------------------------------------------------- source discovery

def codex_home():
    env = os.environ.get('CODEX_HOME')
    if env:
        return Path(env)
    base = os.environ.get('USERPROFILE') if os.name == 'nt' else None
    return Path(base) / '.codex' if base else Path.home() / '.codex'


def session_dirs(sessions, now=None):
    """Today's and yesterday's date folders (local and UTC), plus the folder itself for flat layouts."""
    now = now or datetime.now(timezone.utc)
    days = []
    for d in (now.astimezone(), now.astimezone() - timedelta(days=1), now, now - timedelta(days=1)):
        key = (d.year, d.month, d.day)
        if key not in days:
            days.append(key)
    out = [sessions / f'{y:04d}' / f'{m:02d}' / f'{d:02d}' for y, m, d in days]
    out.append(sessions)
    return out


class Follower:
    """Finds the files to follow and turns their new lines into events. Runs in its own thread."""

    def __init__(self, root, store, log, codex_sessions=None):
        self.root = Path(root)
        self.root_norm = norm_path(self.root)
        self.bridge = self.root / '.bridge'
        self.logs = self.bridge / 'watcher' / 'logs'
        self.store, self.log = store, log
        self.sessions = Path(codex_sessions) if codex_sessions else codex_home() / 'sessions'
        self.codex = {}           # path -> CodexSource
        self.claude = {}          # path -> ClaudeSource
        self.checked = {}         # rollout path -> (label, role) when it belongs here, False otherwise
        self.started = time.time()
        self.first_pass = True
        self.watcher = {}
        self.stop_event = threading.Event()

    # -- watcher state
    def read_watcher(self):
        try:
            self.watcher = json.loads((self.bridge / 'watcher' / 'WATCHER_STATE.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            self.watcher = {}
        if not isinstance(self.watcher, dict):
            self.watcher = {}

    def watcher_alive(self):
        w = self.watcher
        if w.get('status') != 'RUNNING':
            return False
        beat = parse_time(w.get('heartbeat_at'))
        # The watcher saves its state at least every heartbeat_seconds (default 300).
        return beat is None or (datetime.now(timezone.utc) - beat).total_seconds() < 900

    # -- discovery
    def discover(self):
        self.read_watcher()
        self.discover_claude()
        self.discover_codex()
        self.first_pass = False

    def discover_claude(self):
        wanted = []
        base = ((self.watcher.get('agents') or {}).get('claude') or {}).get('log_base')
        if base:
            wanted.append(Path(str(base) + '.out.log'))
        try:
            logs = sorted(self.logs.glob('*-claude.out.log'), key=lambda p: p.name, reverse=True)
        except OSError:
            logs = []
        if logs:
            wanted.append(logs[0])
        for p in logs[1:4]:       # an earlier run that is still being written (rare)
            try:
                if time.time() - p.stat().st_mtime < 120:
                    wanted.append(p)
            except OSError:
                pass
        for p in wanted:
            key = norm_path(p)
            if key in self.claude or not p.exists():
                continue
            self.claude[key] = ClaudeSource(self.store, p, replay=self.first_pass)
            self.store.add('claude', 'session', p.name, '', '', 'Claude')
            self.note_skip(self.claude[key].tail, 'claude', 'Claude')
            self.drain(key, self.claude[key])

    def rollout_identity(self, path):
        if path in self.checked:
            return self.checked[path]
        line = first_line(path)
        if line is None:
            return None               # still being written: decide on a later pass
        try:
            o = json.loads(line)
        except ValueError:
            self.checked[path] = False
            return False
        meta = o.get('payload') if isinstance(o, dict) and isinstance(o.get('payload'), dict) else {}
        if o.get('type') != 'session_meta' or not meta.get('cwd'):
            self.checked[path] = False
            return False
        hit = codex_label(meta) if norm_path(meta['cwd']) == self.root_norm else False
        self.checked[path] = hit
        return hit

    def discover_codex(self):
        candidates = []
        for d in session_dirs(self.sessions):
            try:
                names = [e.name for e in os.scandir(d) if e.name.startswith('rollout-') and e.name.endswith('.jsonl')]
            except OSError:
                continue
            for name in names:
                path = d / name
                if norm_path(path) in self.codex:
                    continue
                try:
                    # os.stat, not the directory entry: on NTFS the entry of a file that is still
                    # open for writing can show an old modification time.
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                candidates.append((name, mtime, path))
        candidates.sort(key=lambda c: c[0], reverse=True)      # rollout names start with their creation time
        t = time.time()
        added = False
        for name, mtime, path in candidates:
            if t - mtime < ACTIVE_SECONDS or mtime >= self.started - 5:
                ident = self.rollout_identity(str(path))
                if ident:
                    self.add_codex(norm_path(path), path, ident)
                    added = True
        if self.first_pass and not added and not self.codex:
            # Nothing live at startup: show this project's most recent finished session, as before.
            for name, mtime, path in candidates:
                ident = self.rollout_identity(str(path))
                if ident:
                    self.add_codex(norm_path(path), path, ident)
                    break

    def add_codex(self, key, path, ident):
        label, role = ident
        self.codex[key] = CodexSource(self.store, path, label, role, replay=self.first_pass)
        self.store.add('codex', 'session', path.name, '', '', label)
        self.note_skip(self.codex[key].tail, 'codex', label)
        self.drain(key, self.codex[key])

    def note_skip(self, tail, agent, label):
        if tail.skipped_bytes:
            mb = tail.skipped_bytes / 1024 / 1024
            self.store.add(agent, 'note', f'启动时只回放了最后 {REPLAY_BYTES // 1024 // 1024} MB，跳过了更早的 {mb:.1f} MB；'
                           '完整内容在 agent 日志里。', '', '', label)

    # -- reading
    def read_source(self, key, src):
        try:
            for line in src.tail.read_lines():
                try:
                    src.handle(line)
                except Exception as e:    # one bad row never stops the viewer
                    self.log.write(f'row skipped in {src.tail.path.name}: {e!r}', 'row:' + src.tail.path.name)
        except OSError as e:
            self.log.write(f'cannot read {src.tail.path}: {e}', 'read:' + key)
            return False
        return True

    def drain(self, key, src):
        """Read a newly found file up to its end, so its rows follow its own session header."""
        for _ in range(8):
            if not self.read_source(key, src) or src.tail.caught_up:
                break

    def read_all(self):
        for group in (self.claude, self.codex):
            for key, src in list(group.items()):
                self.read_source(key, src)
                if time.time() - src.tail.last_growth > IDLE_DROP_SECONDS and not self.is_current(src):
                    del group[key]
        for src in self.claude.values():
            if src.tail.caught_up:
                src.replay = False

    def is_current(self, src):
        base = ((self.watcher.get('agents') or {}).get('claude') or {}).get('log_base')
        return isinstance(src, ClaudeSource) and base and norm_path(str(base) + '.out.log') == norm_path(src.tail.path)

    def status(self):
        agents = self.watcher.get('agents') or {}
        acts = [s.status() for s in self.codex.values()]
        acts = [a for a in acts if a]
        acts.sort(key=lambda a: a['secs'])
        return dict(watcher=self.watcher_alive(),
                    claude=bool((agents.get('claude') or {}).get('running')) and self.watcher_alive(),
                    codex=bool((agents.get('codex') or {}).get('running')) and self.watcher_alive(),
                    codex_activity=acts[0] if acts else None)

    def run(self):
        tick = 0
        while not self.stop_event.is_set():
            try:
                if tick % DISCOVER_EVERY == 0:
                    self.discover()
                self.read_all()
            except Exception:
                self.log.write('follower pass failed: ' + traceback.format_exc(limit=3).replace('\n', ' | '), 'pass')
            tick += 1
            self.stop_event.wait(TICK_SECONDS)


# ---------------------------------------------------------------- HTTP

class Viewer:
    def __init__(self, root, port, codex_sessions=None):
        self.root = Path(root).resolve()
        self.port = port
        self.instance = uuid.uuid4().hex[:12]
        self.project = self.root.name
        self.log = Log(self.root / '.bridge' / 'watcher' / 'logs' / 'live_view.log')
        self.store = Store()
        self.follower = Follower(self.root, self.store, self.log, codex_sessions)
        self.page = HERE / 'live_view.html'
        self.httpd = None

    def health(self):
        return dict(ok=True, instance=self.instance, root=str(self.root), project=self.project, pid=os.getpid(),
                    port=self.port)

    def make_handler(self):
        viewer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'bridge-live-view'

            def log_message(self, *args):
                pass

            def send(self, code, body, ctype='application/json; charset=utf-8'):
                data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                try:
                    host = (self.headers.get('Host') or '').lower()
                    allowed = {f'{h}:{viewer.port}' for h in ('127.0.0.1', 'localhost', '[::1]')}
                    if host not in allowed:      # blocks DNS-rebinding pages from reading agent output
                        return self.send(403, dict(error='forbidden host'))
                    url = urllib.parse.urlsplit(self.path)
                    q = urllib.parse.parse_qs(url.query)
                    if url.path == '/health':
                        return self.send(200, viewer.health())
                    if url.path == '/events':
                        try:
                            since = max(0, int(q.get('since', ['0'])[0]))
                        except ValueError:
                            since = 0
                        body = viewer.store.since(since)
                        body.update(instance=viewer.instance, project=viewer.project,
                                    status=viewer.follower.status())
                        return self.send(200, body)
                    if url.path == '/text':
                        try:
                            text = viewer.store.text(int(q.get('id', ['-1'])[0]))
                        except ValueError:
                            text = None
                        if text is None:
                            return self.send(404, dict(error='gone'))
                        return self.send(200, dict(text=text))
                    if url.path in ('/', '/index.html'):
                        return self.send(200, viewer.page.read_bytes(), 'text/html; charset=utf-8')
                    return self.send(404, dict(error='not found'))
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                except Exception as e:
                    viewer.log.write(f'request failed: {e!r}', 'http')
                    try:
                        self.send(500, dict(error='internal'))
                    except OSError:
                        pass

        return Handler

    def bind(self):
        """Bind the first free port from self.port. Returns None if this project's viewer already holds one."""
        handler = self.make_handler()
        first = self.port
        for port in range(first, first + PORT_SPAN):
            other = probe(port)
            if other and norm_path(other.get('root')) == norm_path(self.root):
                return other
            try:
                self.port = port
                self.httpd = Server(('127.0.0.1', port), handler)
                return None
            except OSError:
                self.httpd = None
                continue
        self.port = first
        raise OSError(f'no free port in {first}-{first + PORT_SPAN - 1}')

    def serve(self):
        other = self.bind()
        if other:
            print(json.dumps(dict(action='ALREADY_RUNNING', url=url_for(other['port']), pid=other.get('pid'))))
            return 0
        state = dict(port=self.port, pid=os.getpid(), instance=self.instance, project=self.project,
                     root=str(self.root), url=url_for(self.port), started_at=now_iso())
        write_state(self.root, state)
        threading.Thread(target=self.follower.run, name='follower', daemon=True).start()
        self.log.write(f'viewer {self.instance} serving {url_for(self.port)} (pid {os.getpid()})', 'start')
        print(json.dumps(dict(action='SERVING', url=url_for(self.port), pid=os.getpid())), flush=True)
        try:
            self.httpd.serve_forever(poll_interval=0.5)
        finally:
            self.follower.stop_event.set()
            self.httpd.server_close()
            cur = read_state(self.root)
            if cur.get('instance') == self.instance:
                try:
                    (self.root / '.bridge' / 'watcher' / STATE_NAME).unlink()
                except OSError:
                    pass
        return 0


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind a port that is already listening.
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# ---------------------------------------------------------------- control commands

def url_for(port):
    return f'http://127.0.0.1:{port}/'


def state_path(root):
    return Path(root) / '.bridge' / 'watcher' / STATE_NAME


def read_state(root):
    try:
        v = json.loads(state_path(root).read_text(encoding='utf-8-sig'))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(root, value):
    path = state_path(root)
    tmp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe(port, timeout=3):
    """/health of a viewer on this port, or None."""
    try:
        with _NO_PROXY.open(f'http://127.0.0.1:{int(port)}/health', timeout=timeout) as r:
            v = json.loads(r.read().decode('utf-8'))
            return v if isinstance(v, dict) and v.get('ok') else None
    except Exception:
        return None


def running(root):
    """Health of this project's viewer, if one is serving."""
    st = read_state(root)
    if not st.get('port'):
        return None
    h = probe(st['port'])
    if h and norm_path(h.get('root')) == norm_path(Path(root).resolve()):
        return h
    return None


def spawn(root, port):
    logs = Path(root) / '.bridge' / 'watcher' / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / 'live_view.out.log', 'ab')
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=str(root), close_fds=True)
    if os.name == 'nt':
        # No console window, own process group: closing the watcher window or Ctrl+C never reaches it.
        kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    try:
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve', '--port', str(port),
                                 '--root', str(root)], env=env, **kwargs)
    finally:
        out.close()


def ensure(root, port, wait=10.0):
    h = running(root)
    if h:
        return dict(action='RUNNING', url=url_for(h['port']), pid=h['pid'])
    proc = spawn(root, port)
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.25)
        h = running(root)
        if h:
            return dict(action='STARTED', url=url_for(h['port']), pid=h['pid'])
        if proc.poll() is not None and proc.returncode != 0:
            break
    tail = ''
    try:
        tail = (Path(root) / '.bridge/watcher/logs/live_view.out.log').read_text(encoding='utf-8', errors='replace')[-600:]
    except OSError:
        pass
    return dict(action='ERROR', reason='live view did not start; see .bridge/watcher/logs/live_view.out.log',
                detail=tail.strip())


def stop(root):
    h = running(root)
    if not h:
        return dict(action='NOT_RUNNING')
    try:
        os.kill(int(h['pid']), signal.SIGTERM)
    except OSError as e:
        return dict(action='ERROR', reason=f'cannot stop pid {h["pid"]}: {e}')
    for _ in range(20):
        time.sleep(0.25)
        if not running(root):
            break
    try:
        if read_state(root).get('instance') == h['instance']:
            state_path(root).unlink()
    except OSError:
        pass
    return dict(action='STOPPED', pid=h['pid'])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['serve', 'ensure', 'open', 'stop', 'status'])
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--root', type=Path, default=ROOT)
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args(argv)
    root = a.root.resolve()
    if a.command == 'serve':
        if os.name != 'nt':
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            return Viewer(root, a.port).serve()
        except OSError as e:
            print(json.dumps(dict(action='ERROR', reason=str(e))), flush=True)
            return 1
    if a.command == 'status':
        h = running(root)
        result = dict(action='RUNNING', url=url_for(h['port']), pid=h['pid']) if h else dict(action='NOT_RUNNING')
    elif a.command == 'stop':
        result = stop(root)
    else:
        result = ensure(root, a.port)
        if a.command == 'open' and result['action'] != 'ERROR' and not a.no_browser:
            try:
                webbrowser.open(result['url'])
            except Exception:
                pass
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result['action'] == 'ERROR' else 0


if __name__ == '__main__':
    sys.exit(main())
