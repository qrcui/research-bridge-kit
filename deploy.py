#!/usr/bin/env python3
"""research-bridge-kit deployer (Claude Code x Codex research bridge, protocol V3.0).

Standard library only, Python 3.8+. Never overwrites an existing project file.

  python deploy.py --example-config --out myproject.json  write a config template to fill in
  python deploy.py --config myproject.json --dry-run       show the plan, write nothing
  python deploy.py --config myproject.json                 deploy (refuses if .bridge/ already exists)
  python deploy.py --upgrade-code --project <root>         refresh only the code files of a deployment
  python deploy.py --verify --project <root>               unit tests + probes with the project Python

--target <dir> writes to a different local path than config.project_root (for example a mounted
copy of a Windows folder); project_root is still what the documents and prompts show.
Deployment is NOT a start: the system is left IDLE until the user explicitly starts it.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

KIT_VERSION = '3.2.0'
KIT = Path(__file__).resolve().parent
TEMPLATE = KIT / 'template'
RENDER_SUFFIXES = {'.md', '.txt'}
# Files that `--upgrade-code` may replace. Documents, prompts, configs and state are never touched.
CODE_FILES = [
    '.bridge/dispatcher.py', '.bridge/claude_bridge.py', '.bridge/test_dispatcher.py',
    '.bridge/watcher/bridge_watcher.ps1', '.bridge/watcher/bridgectl.ps1',
    '.bridge/watcher/start_watcher.cmd', '.bridge/watcher/bridgectl.cmd',
    '.bridge/watcher/install_clis.ps1', '.bridge/watcher/install_clis.cmd',
    '.bridge/watcher/operator.ps1', '.bridge/watcher/operator.cmd',
    '.bridge/watcher/live_view.ps1', '.bridge/watcher/live_view.html', '.bridge/watcher/live_view.cmd',
    '.bridge/watcher/live_view.py', '.bridge/watcher/test_live_view.py',
]
EXAMPLE = {
    "project_root": "D:/path/to/your/project",
    "project_name": "My research project",
    "python": "C:/Users/you/venv/Scripts/python.exe",
    "research_goal": "One paragraph: what the research should achieve and why.",
    "success_criteria": "Primary metric, on which data, and what counts as success.",
    "evaluation_protocol": "How results and generalization are judged (held-out split, cross-validation, ...).",
    "deadline": "",
    "max_research_rounds": 8,
    "protected_paths": ["model.pt", "data/train.csv"],
    "data_rules": ["Only data under data/ may be used for training."],
    "compute": "1 GPU; at most one large training job at a time.",
    "other_constraints": [],
    "claude_model": "opus",
    "codex_model": "",
    "codex_reasoning_effort": "high",
    "git_init_if_missing": True,
}
GITIGNORE_BLOCK = ("\n# research-bridge (protocol V3.0): runtime communication stays out of Git\n"
                   "/.bridge/\n")
GITIGNORE_DEFAULT = """# Python / notebooks
__pycache__/
*.py[cod]
.ipynb_checkpoints/
.venv/
venv/
env/
*.egg-info/
.pytest_cache/

# Secrets
.env
.env.*
*.pem
*.key
credentials*.json
secrets.*

# Weights, caches and logs (not backed up by Git; keep separate backups)
*.pt
*.pth
*.ckpt
*.safetensors
*.onnx
*.log
*.tmp
"""


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def fail(msg):
    print(json.dumps({'action': 'ERROR', 'reason': msg}, ensure_ascii=False))
    sys.exit(1)


def write_new(path, data, dry, plan):
    """Create a file that must not exist yet (UTF-8, no BOM)."""
    plan.append(str(path))
    if dry:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode('utf-8')
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    with open(tmp, 'xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if path.exists():
        tmp.unlink()
        raise FileExistsError(str(path))
    os.rename(tmp, path)


def jdump(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


# ---------------------------------------------------------------- config

def load_config(path):
    try:
        raw = Path(path).read_bytes()
        # PowerShell 5.1 '>' writes UTF-16; accept it as well as UTF-8 (with or without BOM).
        enc = 'utf-16' if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8-sig'
        cfg = json.loads(raw.decode(enc))
    except (OSError, ValueError) as e:
        fail(f'Cannot read config {path}: {e}')
    if not isinstance(cfg, dict):
        fail('Config must be a JSON object')
    merged = dict(EXAMPLE)
    merged.update(cfg)
    for key in ['project_root', 'project_name', 'python']:
        if not isinstance(merged.get(key), str) or not merged[key].strip() or merged[key] == EXAMPLE[key]:
            fail(f'Config "{key}" must be set to a real value')
    rounds = merged.get('max_research_rounds')
    if type(rounds) is not int or not 1 <= rounds <= 999:
        fail('max_research_rounds must be an integer 1-999')
    for key in ['protected_paths', 'data_rules', 'other_constraints']:
        if not isinstance(merged.get(key), list) or not all(isinstance(x, str) for x in merged[key]):
            fail(f'"{key}" must be a list of strings')
    for rel in merged['protected_paths']:
        if os.path.isabs(rel) or re.match(r'^[A-Za-z]:', rel) or '..' in Path(rel.replace('\\', '/')).parts:
            fail(f'Protected path must be relative to the project root: {rel}')
    for key in ['research_goal', 'success_criteria', 'evaluation_protocol', 'deadline', 'compute',
                'claude_model', 'codex_model', 'codex_reasoning_effort']:
        if not isinstance(merged.get(key, ''), str):
            fail(f'"{key}" must be a string')
    if merged['codex_reasoning_effort'] and not re.fullmatch(r'[a-z]+', merged['codex_reasoning_effort']):
        fail('codex_reasoning_effort must be a single lowercase word such as high')
    for key in ['claude_model', 'codex_model']:
        if merged[key] and not re.fullmatch(r'[A-Za-z0-9._:\-\[\]]+', merged[key]):
            fail(f'{key} contains unexpected characters')
    return merged


# ---------------------------------------------------------------- generated documents

def bullets(items, empty='- (none)'):
    return '\n'.join(f'- {x}' for x in items) if items else empty


def constraints_md(c, deployed):
    return f"""# Project constraints — {c['project_name']}

Binding for the Research Lead, Codex, every subagent and every task (`.bridge/PROTOCOL.md` section 1).
Changed only on the user's explicit instruction (an Operator CONFIGURE request, or the user editing this
file). Agents never relax these rules themselves. Generated {deployed} by research-bridge-kit {KIT_VERSION}.

## Research goal

{c['research_goal'].strip() or '(not specified — the Research Lead asks the user via NEEDS_USER before R0001)'}

## Success criteria

{c['success_criteria'].strip() or '(not specified)'}

## Evaluation protocol

{c['evaluation_protocol'].strip() or '(not specified — do not invent one; ask the user)'}

## Deadline

{c['deadline'].strip() or '(none)'}

## Round budget

At most {c['max_research_rounds']} formal research rounds per authorization (R0000 does not count).
`operator/CONTROL.json` `max_research_rounds` is authoritative and changes only through the Operator.

## Protected paths

Never modify, move, delete or overwrite without the user's explicit authorization. Hash them, do not
rewrite them. Better candidates go under `experiments/R####/`; replacement is a user decision.

{bullets(['`' + p + '`' for p in c['protected_paths']])}

## Data rules

{bullets(c['data_rules'])}

## Compute

{c['compute'].strip() or '(not specified; default: one large GPU job at a time)'}

## Other constraints

{bullets(c['other_constraints'])}

## Environment

- Project root: `{c['project_root']}`
- Python: `{c['python']}`
- No archives, uploads, pushes or public repositories unless the user explicitly asks.
"""


def status_md(c, deployed):
    goal = c['research_goal'].strip().splitlines()[0] if c['research_goal'].strip() else '（未填写）'
    return f"""# 项目状态 / Project status（Claude Research Lead 维护，供用户与 Operator 阅读 / maintained by the Research Lead for the user and the Operator）

## 当前状态 / Current state（{deployed}，已部署未开始 / deployed, not started）

- **生命周期 / Lifecycle**：IDLE / STOP，等待用户明确说“开始研究” / waiting for the user's explicit start.
- **研究目标 / Goal**：{goal}（全文见 / full text: `.bridge/PROJECT_CONSTRAINTS.md`）
- **研究轮次 / Rounds**：0 / {c['max_research_rounds']}
- **Claude 状态 / Claude**：IDLE。**Codex 状态 / Codex**：IDLE。
- **下一步 / Next**：开始后发布 R0000 通信测试，核验通过后设计 R0001 / after start: R0000 communication test, then R0001.
- **需要用户介入 / Needs user**：否 / no
- **当前阻塞 / Blockers**：无 / none

## Decision Records（只追加 / append-only）

- {deployed} 部署（research-bridge-kit {KIT_VERSION}，protocol V3.0）：生命周期 IDLE，未发布任何任务，未开始研究。
"""


def watcher_config(c):
    claude = ['-p']
    if c['claude_model']:
        claude += ['--model', c['claude_model']]
    claude += ['--permission-mode', 'bypassPermissions', '--output-format', 'stream-json', '--verbose']
    codex = ['exec', '--cd', '{ROOT}', '--dangerously-bypass-approvals-and-sandbox', '--skip-git-repo-check']
    if c['codex_model']:
        codex += ['-m', c['codex_model']]
    if c['codex_reasoning_effort']:
        codex += ['-c', 'model_reasoning_effort=' + c['codex_reasoning_effort']]
    codex += ['-o', '{LASTMSG}', '-']
    return {
        "_comment": "research-bridge V3.0 watcher config. Edit models/flags here; restart the watcher to apply.",
        "python": c['python'].replace('\\', '/'),
        "debounce_ms": 1500, "rescan_seconds": 60, "max_attempts_per_state": 2,
        "retry_delay_seconds": 120, "heartbeat_seconds": 300,
        "live_view": {"enabled": True, "port": 8765},
        "agents": {
            "claude": {"enabled": True, "command": "claude", "args": claude,
                       "prompt_file": ".bridge/watcher/CLAUDE_WORKER_PROMPT.txt"},
            "codex": {"enabled": True, "command": "codex", "args": codex,
                      "prompt_file": ".bridge/watcher/CODEX_WORKER_PROMPT.txt"},
        },
    }


def initial_states(c, deployed):
    return {
        '.bridge/operator/CONTROL.json': {
            "revision": 0, "lifecycle": "IDLE", "mode": "STOP", "run_id": None, "effective_task": None,
            "max_research_rounds": c['max_research_rounds'], "updated_by": "operator", "updated_at": deployed},
        '.bridge/operator/OPERATOR_STATE.json': {
            "schema_version": 1, "protocol": "V3.0", "last_request_id": None,
            "lifecycle_log": [{"at": deployed, "event": "INITIALIZED", "lifecycle": "IDLE", "mode": "STOP",
                               "control_revision": 0, "via": "research-bridge-kit " + KIT_VERSION}],
            "report_state": "IDLE", "updated_at": deployed},
        '.bridge/CLAUDE_STATE.json': {
            "schema_version": 1, "protocol": "V3.0", "phase": "IDLE", "run_id": None,
            "last_published_task": None, "published_tasks": [], "pending_publish": None,
            "last_processed_result": None, "handled_requests": [], "r0000_verified": False,
            "completed_rounds": 0, "interrupted_subtasks": [],
            "subagent_budget": {"max_concurrent": 3, "nesting": False, "roles_dir": ".claude/agents/research-*"},
            "updated_at": deployed},
        '.bridge/signals/claude_task.json': {"task_id": None, "status": "IDLE", "task_file": None, "revision": 0},
        # Same schema as dispatcher.Bridge.initialize(); Codex-owned from here on.
        '.bridge/CODEX_STATE.json': {
            "schema_version": 1, "protocol_version": "3.0", "status": "IDLE", "lifecycle": "IDLE",
            "run_id": None, "closed_run_ids": {}, "connectivity_runs": {}, "project_root": c['project_root'],
            "active_task": None, "completed_tasks": {}, "last_completed_task": None,
            "last_seen_control_revision": None, "initialized_at": deployed, "updated_at": deployed,
            "scheduler": None},
        '.bridge/signals/codex_result.json': {
            "task_id": None, "status": "BLOCKED", "result_file": None, "revision": 0,
            "reason_code": "AWAITING_FIRST_TASK"},
    }


def render(text, values):
    for key, val in values.items():
        text = text.replace('{{' + key + '}}', val)
    left = re.findall(r'\{\{[A-Z_]+\}\}', text)
    if left:
        raise ValueError('Unrendered placeholders: ' + ', '.join(sorted(set(left))))
    return text


# ---------------------------------------------------------------- deploy

def deploy(c, target, dry):
    if not target.is_dir():
        fail(f'Project folder does not exist: {target}')
    if (target / '.bridge').exists():
        fail(f'{target / ".bridge"} already exists. Use --upgrade-code to refresh code, never redeploy over state.')
    deployed = now()
    protected = '\n'.join(f'   - `{p}`' for p in c['protected_paths']) or '   - (no protected paths configured)'
    values = {'PROJECT_NAME': c['project_name'], 'PROJECT_ROOT': c['project_root'], 'PYTHON': c['python'],
              'DEPLOYED_AT': deployed, 'KIT_VERSION': KIT_VERSION,
              'DEADLINE': c['deadline'].strip() or '未设定',
              'RESEARCH_GOAL': c['research_goal'].strip() or '（待填写）',
              'PROTECTED_CHECKLIST': protected}
    created, skipped = [], []
    for src in sorted(p for p in TEMPLATE.rglob('*') if p.is_file()):
        rel = src.relative_to(TEMPLATE).as_posix()
        dest = target / rel
        if dest.exists():          # only possible outside .bridge/: existing project files are preserved
            skipped.append(rel)
            continue
        data = src.read_bytes()
        if src.suffix in RENDER_SUFFIXES:
            data = render(data.decode('utf-8'), values).encode('utf-8')
        write_new(dest, data, dry, created)
    gen = {'.bridge/PROJECT_CONSTRAINTS.md': constraints_md(c, deployed),
           '.bridge/PROJECT_STATUS.md': status_md(c, deployed),
           '.bridge/watcher/watcher_config.json': jdump(watcher_config(c))}
    gen.update({k: jdump(v) for k, v in initial_states(c, deployed).items()})
    for rel, text in gen.items():
        write_new(target / rel, text, dry, created)
    for d in ['.bridge/tasks', '.bridge/results', '.bridge/locks', '.bridge/operator/requests',
              '.bridge/operator/acknowledgements', '.bridge/watcher/logs']:
        created.append(d + '/')
        if not dry:
            (target / d).mkdir(parents=True, exist_ok=True)
    git = git_setup(c, target, dry)
    record = {'kit_version': KIT_VERSION, 'deployed_at': deployed, 'config': c,
              'created': [x for x in created if not x.endswith('/')], 'preserved_existing': skipped, 'git': git}
    if not dry:
        record['created_sha256'] = {Path(p).relative_to(target).as_posix(): sha256(p)
                                    for p in record['created'] if Path(p).is_file()}
        write_new(target / '.bridge/DEPLOYMENT.json', jdump(record), dry, [])
    print(json.dumps({'action': 'DRY_RUN' if dry else 'DEPLOYED', 'target': str(target),
                      'created_files': len(record['created']), 'preserved_existing': skipped, 'git': git,
                      'lifecycle': 'IDLE (not started)',
                      'next': ['Install/login CLIs: .bridge/watcher/install_clis.cmd (if needed)',
                               'Check: .bridge/watcher/bridgectl check (Windows)',
                               'Review git status and make the baseline commit with the user',
                               'Start the watcher: .bridge/watcher/start_watcher.cmd',
                               'Start research only on the user\'s explicit words: bridgectl start']},
                     ensure_ascii=False, indent=2))


def git_setup(c, target, dry):
    out = {'gitignore': None, 'repository': None}
    gi = target / '.gitignore'
    if gi.exists():
        text = gi.read_text(encoding='utf-8', errors='replace')
        if not any(line.strip() in ('/.bridge/', '.bridge/', '/.bridge', '.bridge') for line in text.splitlines()):
            out['gitignore'] = 'appended /.bridge/'
            if not dry:
                with open(gi, 'a', encoding='utf-8', newline='\n') as f:
                    f.write(GITIGNORE_BLOCK)
        else:
            out['gitignore'] = 'already ignores .bridge/'
    else:
        out['gitignore'] = 'created'
        if not dry:
            write_new(gi, GITIGNORE_DEFAULT + GITIGNORE_BLOCK, False, [])
    if (target / '.git').exists():
        out['repository'] = 'existing (left unchanged)'
    elif not c.get('git_init_if_missing', True):
        out['repository'] = 'none (git_init_if_missing=false)'
    elif os.name != 'nt':
        out['repository'] = 'none: run git init on Windows (a Linux-side init would set the wrong core.filemode)'
    elif not shutil.which('git'):
        out['repository'] = 'none: git not found'
    else:
        out['repository'] = 'initialized (no commit yet; review git status, then make the baseline commit)'
        if not dry:
            r = subprocess.run(['git', '-C', str(target), 'init', '-b', 'main'], capture_output=True, text=True)
            if r.returncode != 0:
                r = subprocess.run(['git', '-C', str(target), 'init'], capture_output=True, text=True)
            if r.returncode != 0:
                out['repository'] = 'git init failed: ' + (r.stderr or r.stdout).strip()[:300]
    return out


# ---------------------------------------------------------------- upgrade / verify

def upgrade(target, force):
    b = target / '.bridge'
    if not (b / 'dispatcher.py').exists():
        fail(f'No deployment found at {b}')
    busy = [p for p in ['locks/engineering.lock', 'locks/research.lock'] if (b / p).exists()]
    try:
        ws = json.loads((b / 'watcher/WATCHER_STATE.json').read_text(encoding='utf-8-sig'))
        if ws.get('status') == 'RUNNING' and any(a.get('running') for a in ws.get('agents', {}).values()):
            busy.append('watcher reports a running agent')
    except (OSError, ValueError, AttributeError):
        pass
    if busy and not force:
        fail('Work is live (' + ', '.join(busy) + '). Upgrade when idle, or pass --force after checking.')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = b / 'deployment_backups' / f'upgrade-{KIT_VERSION}-{stamp}'
    changed, same = [], []
    for rel in CODE_FILES:
        src, dest = TEMPLATE / rel, target / rel
        if dest.exists() and sha256(dest) == sha256(src):
            same.append(rel)
            continue
        if dest.exists():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, backup / rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name('.' + dest.name + '.' + uuid.uuid4().hex + '.tmp')
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
        changed.append(rel)
    print(json.dumps({'action': 'UPGRADED', 'kit_version': KIT_VERSION, 'changed': changed, 'unchanged': same,
                      'backup': str(backup) if changed else None,
                      'note': 'Documents, prompts, configs and state were not touched. Restart the watcher to load a new watcher script.'},
                     indent=2))


def verify(target):
    b = target / '.bridge'
    try:
        py = json.loads((b / 'watcher/watcher_config.json').read_text(encoding='utf-8-sig'))['python']
    except (OSError, ValueError, KeyError) as e:
        fail(f'Cannot read watcher_config.json: {e}')
    if not Path(py).exists():
        fail(f'Project Python {py} is not reachable from here. Run ".bridge\\watcher\\bridgectl check" on Windows instead.')
    res = {}
    r = subprocess.run([py, '-c', 'import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 12) else 1)'],
                       capture_output=True, text=True)
    res['python'] = (r.stdout.strip(), 'OK' if r.returncode == 0 else 'TOO OLD (need 3.12+)')
    r = subprocess.run([py, '-m', 'unittest', 'test_dispatcher'], cwd=str(b), capture_output=True, text=True)
    res['unit_tests'] = 'PASS' if r.returncode == 0 else 'FAIL: ' + r.stderr.strip()[-600:]
    r = subprocess.run([py, '-m', 'unittest', 'test_live_view'], cwd=str(b / 'watcher'), capture_output=True, text=True)
    res['live_view_tests'] = 'PASS' if r.returncode == 0 else 'FAIL: ' + r.stderr.strip()[-600:]
    for name in ['dispatcher.py', 'claude_bridge.py']:
        r = subprocess.run([py, str(b / name), 'probe'], cwd=str(target), capture_output=True, text=True)
        res[name + ' probe'] = (r.stdout.strip().splitlines() or ['(no output) ' + r.stderr.strip()[-300:]])[-1]
    for cli in ['claude', 'codex']:
        res[cli + ' CLI'] = shutil.which(cli) or 'NOT FOUND (run .bridge\\watcher\\install_clis.cmd)'
    ok = res['python'][1] == 'OK' and res['unit_tests'] == 'PASS' and res['live_view_tests'] == 'PASS'
    print(json.dumps({'action': 'VERIFIED' if ok else 'VERIFY_FAILED', **res}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config')
    ap.add_argument('--target', help='local path to write to (default: config.project_root)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--example-config', action='store_true')
    ap.add_argument('--out', help='with --example-config: write the template to this file (UTF-8)')
    ap.add_argument('--upgrade-code', action='store_true')
    ap.add_argument('--verify', action='store_true')
    ap.add_argument('--project', help='existing deployment root for --upgrade-code / --verify')
    ap.add_argument('--force', action='store_true', help='upgrade even if work looks live (after checking)')
    a = ap.parse_args()
    if sys.version_info < (3, 8):
        fail('Python 3.8+ is required to run the deployer')
    if a.example_config:
        text = json.dumps(EXAMPLE, ensure_ascii=False, indent=2) + '\n'
        if a.out:
            out = Path(a.out)
            if out.exists():
                fail(f'{out} already exists; choose another name')
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding='utf-8')
            print(json.dumps({'action': 'EXAMPLE_WRITTEN', 'path': str(out)}))
        else:
            print(text, end='')
    elif a.upgrade_code or a.verify:
        if not a.project:
            fail('--project is required')
        target = Path(a.project).expanduser().resolve()
        upgrade(target, a.force) if a.upgrade_code else verify(target)
    elif a.config:
        c = load_config(a.config)
        target = Path(a.target or c['project_root']).expanduser().resolve()
        try:
            deploy(c, target, a.dry_run)
        except (OSError, ValueError) as e:
            fail(f'Deployment stopped: {e}. Nothing existing was overwritten. Delete the partially created '
                 f'{target / ".bridge"} folder (and any new .claude/agents, GIT_PROVENANCE.md or HANDOFF.md it '
                 f'created) before retrying.')
    else:
        ap.print_help()


if __name__ == '__main__':
    main()
