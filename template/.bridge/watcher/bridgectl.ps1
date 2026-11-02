<#
.SYNOPSIS
  Human control CLI for the V3.0 bridge (protocol section 8). Run from any directory.

.DESCRIPTION
  bridgectl status                       show CONTROL, both agents, watcher, running processes
  bridgectl check                        self-test: Python 3.12+, unit tests, probes, CLIs, watcher
  bridgectl view                         open the read-only live view of both agents in the browser
  bridgectl start  [-Note "..."]         new run_id: STARTING -> watcher connectivity check -> ACTIVE
  bridgectl pause  [-Note "..."]         PAUSE_NOW (workers stop at the next safe checkpoint)
  bridgectl pause-after [-Note "..."]    PAUSE_AFTER_ROUND for the current task
  bridgectl resume [-Note "..."]         back to RUN within the same run_id
  bridgectl end    [-Note "..."]         STOPPING; becomes STOPPED once no agent/worker is live
  bridgectl request "text" [-Type IDEA] [-Effective NEXT_ROUND]
                                         file your words as operator/requests/U####.md

  Writes CONTROL only through claude_bridge.py control (revision+1, atomic, read back) and logs
  each lifecycle event in OPERATOR_STATE. It never edits task/result/state files of either agent.
#>
param(
    [Parameter(Position = 0)][ValidateSet('status', 'check', 'view', 'start', 'pause', 'pause-after', 'resume', 'end', 'request')]
    [string]$Command = 'status',
    [Parameter(Position = 1)][string]$Text,
    [string]$Note,
    [ValidateSet('REDIRECT', 'IDEA', 'REVIEW', 'CONFIGURE', 'QUESTION')][string]$Type = 'IDEA',
    [ValidateSet('NEXT_ROUND', 'IMMEDIATE', 'REFERENCE_ONLY')][string]$Effective = 'NEXT_ROUND'
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Bridge = Join-Path $Root '.bridge'
$Cfg = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'watcher_config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$Py = [string]$Cfg.python
$CB = Join-Path $Bridge 'claude_bridge.py'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }

function J([string]$rel) { try { return (Get-Content -LiteralPath (Join-Path $Bridge $rel) -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null } }
function Py([string[]]$argv) {
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $out = @(& $Py @argv 2>&1 | ForEach-Object { "$_" }) } finally { $ErrorActionPreference = $prev }
    $line = $out | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
    if (-not $line) { throw "python failed: $($out -join "`n")" }
    $obj = $line | ConvertFrom-Json
    if ($obj.action -in @('ERROR', 'BLOCKED')) { throw "$($argv[1]) refused: $($obj.reason)" }
    return $obj
}
function Control([string[]]$extra) {
    $argv = @($CB, 'control') + $extra
    if ($Note) { $argv += @('--note', $Note) }
    $r = Py $argv
    Write-Host ("CONTROL -> rev {0}: {1}/{2} run_id={3}" -f $r.control.revision, $r.control.lifecycle, $r.control.mode, $r.control.run_id) -ForegroundColor Cyan
    return $r.control
}
function OpLog([string]$event) {
    $argv = @($CB, 'oplog', '--event', $event)
    if ($Note) { $argv += @('--note', $Note) }
    Py $argv | Out-Null
}
function Watcher-Alive {
    $w = J 'watcher\WATCHER_STATE.json'
    if ($null -eq $w -or $w.status -ne 'RUNNING') { return $null }
    try { Get-Process -Id ([int]$w.pid) -ErrorAction Stop | Out-Null; return $w } catch { return $null }
}
function Live-Agents {
    $w = J 'watcher\WATCHER_STATE.json'
    $live = @()
    if ($w -and $w.agents) {
        foreach ($n in @('claude', 'codex')) {
            $a = $w.agents.$n
            if ($a -and $a.pid) { try { Get-Process -Id ([int]$a.pid) -ErrorAction Stop | Out-Null; $live += "$n(pid $($a.pid))" } catch { } }
        }
    }
    return $live
}
function Show-Status {
    $c = J 'operator\CONTROL.json'; $cs = J 'CLAUDE_STATE.json'; $xs = J 'CODEX_STATE.json'
    $sig = J 'signals\claude_task.json'; $res = J 'signals\codex_result.json'; $w = Watcher-Alive
    Write-Host ''
    Write-Host ("CONTROL   : {0}/{1}  run_id={2}  rev={3}  rounds<={4}" -f $c.lifecycle, $c.mode, $c.run_id, $c.revision, $c.max_research_rounds)
    Write-Host ("Claude    : phase={0}  last_published={1}  completed_rounds={2}" -f $cs.phase, $cs.last_published_task, $cs.completed_rounds)
    Write-Host ("Task sig  : {0} {1} rev {2} run {3}" -f $sig.task_id, $sig.status, $sig.revision, $sig.run_id)
    $active = if ($xs.active_task) { "$($xs.active_task.task_id) ($($xs.active_task.phase), exp=$($xs.active_task.running_experiment))" } else { 'none' }
    Write-Host ("Codex     : {0}/{1}  active={2}  last_completed={3}" -f $xs.lifecycle, $xs.status, $active, $xs.last_completed_task)
    Write-Host ("Result sig: {0} {1} rev {2}" -f $res.task_id, $res.status, $res.revision)
    if ($w) {
        Write-Host ("Watcher   : RUNNING {0} (pid {1}), heartbeat {2}" -f $w.watcher_id, $w.pid, $w.heartbeat_at) -ForegroundColor Green
        foreach ($n in @('claude', 'codex')) {
            $a = $w.agents.$n
            if ($a) { Write-Host ("  {0,-6}: running={1} attempts={2} last_exit={3} stalled={4}" -f $n, $a.running, $a.attempts, $a.last_exit, $a.stalled) }
        }
    } else { Write-Host 'Watcher   : NOT running  (start: .bridge\watcher\start_watcher.cmd)' -ForegroundColor Yellow }
    $live = Live-Agents
    if ($live.Count) { Write-Host ("Live agent processes: " + ($live -join ', ')) -ForegroundColor Yellow }
    $lv = J 'watcher\LIVE_VIEW.json'
    if ($lv -and $lv.pid) {
        try { Get-Process -Id ([int]$lv.pid) -ErrorAction Stop | Out-Null; Write-Host ("Live view : http://localhost:{0}/" -f $lv.port) -ForegroundColor Cyan } catch { }
    }
    Write-Host ''
}

function Run-Native([string[]]$argv, [string]$cwd) {
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    Push-Location -LiteralPath $cwd
    try { $out = @(& $Py @argv 2>&1 | ForEach-Object { "$_" }); $code = $LASTEXITCODE } finally { Pop-Location; $ErrorActionPreference = $prev }
    return @{ code = $code; out = $out }
}
function Report([string]$name, [bool]$ok, [string]$detail) {
    $tag = if ($ok) { 'PASS' } else { 'FAIL' }
    $color = if ($ok) { 'Green' } else { 'Red' }
    Write-Host ("[{0}] {1,-22} {2}" -f $tag, $name, $detail) -ForegroundColor $color
    if (-not $ok) { $script:CheckFailed = $true }
}

switch ($Command) {
    'status' { Show-Status }
    'view' {
        $lvs = Join-Path $PSScriptRoot 'live_view.ps1'
        if (-not (Test-Path -LiteralPath $lvs)) { throw 'live_view.ps1 is missing; upgrade this deployment with research-bridge-kit (deploy.py --upgrade-code).' }
        $hostExe = (Get-Process -Id $PID).Path
        $lvArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + $lvs + '"'
        if (($PSVersionTable.PSEdition -ne 'Core') -or $IsWindows) {
            Start-Process -FilePath $hostExe -ArgumentList $lvArgs      # own console window, inherits no handles
        } else {
            $lvLog = Join-Path $PSScriptRoot 'logs\live_view'
            Start-Process -FilePath $hostExe -ArgumentList $lvArgs -RedirectStandardOutput "$lvLog.out.log" -RedirectStandardError "$lvLog.err.log"
        }
        Write-Host 'Live view is starting in its own window and opens in your browser (default http://localhost:8765/, next free port if taken). Close that window to stop viewing; the agents are not affected.' -ForegroundColor Cyan
    }
    'check' {
        $script:CheckFailed = $false
        if (-not (Test-Path -LiteralPath $Py)) { Report 'python' $false "$Py not found (fix watcher_config.json)"; break }
        $v = Run-Native @('-c', 'import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 12) else 1)') $Root
        Report 'python >= 3.12' ($v.code -eq 0) (($v.out -join ' ') + "  ($Py)")
        Write-Host 'Running unit tests (about 10-30 s)...'
        $t = Run-Native @('-m', 'unittest', 'test_dispatcher') $Bridge
        $last = ($t.out | Where-Object { $_ -match '^(Ran |OK|FAILED)' }) -join ' '
        Report 'unit tests' ($t.code -eq 0) $last
        foreach ($h in @('dispatcher.py', 'claude_bridge.py')) {
            $r = Run-Native @((Join-Path $Bridge $h), 'probe') $Root
            $line = $r.out | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
            $ok = $false; if ($line) { try { $ok = (($line | ConvertFrom-Json).action -notin @('BLOCKED', 'ERROR')) } catch { } }
            Report "$h probe" $ok ([string]$line)
        }
        foreach ($n in @('claude', 'codex')) {
            $c = Get-Command $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $c) {
                $cands = @()
                if ($env:USERPROFILE) { $cands += (Join-Path $env:USERPROFILE ".local\bin\$n.exe") }
                if ($env:APPDATA) { $cands += (Join-Path $env:APPDATA "npm\$n.cmd") }
                foreach ($f in $cands) { if (Test-Path -LiteralPath $f) { $c = @{ Source = $f }; break } }
            }
            if ($c) { Report "$n CLI" $true $c.Source } else { Report "$n CLI" $false 'not found: double-click .bridge\watcher\install_clis.cmd' }
        }
        $w = Watcher-Alive
        if ($w) { Write-Host ("[INFO] watcher                RUNNING {0}" -f $w.watcher_id) -ForegroundColor Cyan }
        else { Write-Host '[INFO] watcher                not running (start it with .bridge\watcher\start_watcher.cmd before bridgectl start)' -ForegroundColor Yellow }
        if ($script:CheckFailed) { Write-Host 'Some checks failed.' -ForegroundColor Red; exit 1 } else { Write-Host 'All checks passed.' -ForegroundColor Green }
    }
    'start' {
        $c = J 'operator\CONTROL.json'
        if ($c.lifecycle -in @('STARTING', 'ACTIVE')) { throw "Already $($c.lifecycle)/$($c.mode). Use 'resume' after a pause, or 'end' first." }
        if (-not (Watcher-Alive)) { throw 'The watcher is not running. Start it first (.bridge\watcher\start_watcher.cmd), then run start again.' }
        # The watcher must have closed the previous run in CODEX_STATE before a new run can begin.
        for ($i = 0; $i -lt 20; $i++) { $xs = J 'CODEX_STATE.json'; if ($xs.lifecycle -notin @('STARTING', 'ACTIVE', 'STOPPING')) { break }; Start-Sleep -Milliseconds 500 }
        if ($xs.lifecycle -in @('STARTING', 'ACTIVE', 'STOPPING')) { throw "CODEX_STATE still shows $($xs.lifecycle) for the previous run (active task or unclosed run). Run 'bridgectl status' and resolve it first." }
        $new = Control @('--lifecycle', 'STARTING', '--mode', 'RUN', '--new-run-id')
        OpLog 'USER_START'
        $ok = $false
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Milliseconds 500
            $xs = J 'CODEX_STATE.json'
            $run = $xs.connectivity_runs.($new.run_id)
            if ($run -and $run.status -eq 'SUCCESS') { $ok = $true; break }
            if ($run -and $run.status -ne 'CHECKING') { break }
        }
        if (-not $ok) { Write-Host 'Connectivity check did not succeed within 20 s; system left in STARTING (PARTIALLY_STARTED). Check the watcher window.' -ForegroundColor Yellow; OpLog 'PARTIALLY_STARTED'; break }
        Control @('--lifecycle', 'ACTIVE', '--mode', 'RUN') | Out-Null
        OpLog 'ACTIVE'
        Write-Host 'ACTIVE. The watcher will now dispatch work immediately.' -ForegroundColor Green
    }
    'pause' {
        $c = J 'operator\CONTROL.json'; if ($c.lifecycle -ne 'ACTIVE') { throw "Not ACTIVE ($($c.lifecycle))." }
        Control @('--mode', 'PAUSE_NOW') | Out-Null; OpLog 'USER_PAUSE_NOW'
        Write-Host 'PAUSE_NOW recorded. Running workers stop at their next safe checkpoint; nothing new is launched.'
    }
    'pause-after' {
        $c = J 'operator\CONTROL.json'; if ($c.lifecycle -ne 'ACTIVE') { throw "Not ACTIVE ($($c.lifecycle))." }
        $sig = J 'signals\claude_task.json'
        Control @('--mode', 'PAUSE_AFTER_ROUND', '--effective-task', [string]$sig.task_id) | Out-Null; OpLog 'USER_PAUSE_AFTER_ROUND'
        Write-Host "PAUSE_AFTER_ROUND for $($sig.task_id) recorded."
    }
    'resume' {
        $c = J 'operator\CONTROL.json'; if ($c.lifecycle -ne 'ACTIVE' -or $c.mode -eq 'RUN') { throw "Nothing to resume ($($c.lifecycle)/$($c.mode)). After an END use 'start'." }
        if (-not (Watcher-Alive)) { Write-Host 'Warning: the watcher is not running; nothing will be dispatched until it is.' -ForegroundColor Yellow }
        Control @('--mode', 'RUN', '--clear-effective-task') | Out-Null; OpLog 'USER_RESUME'
    }
    'end' {
        $c = J 'operator\CONTROL.json'
        if ($c.lifecycle -in @('IDLE', 'STOPPED')) { Write-Host "Already $($c.lifecycle)."; break }
        if ($c.lifecycle -ne 'STOPPING') { Control @('--lifecycle', 'STOPPING', '--mode', 'STOP') | Out-Null; OpLog 'USER_END' }
        Start-Sleep -Seconds 3
        $xs = J 'CODEX_STATE.json'; $live = Live-Agents
        if (-not $xs.active_task -and $live.Count -eq 0 -and -not (Test-Path (Join-Path $Bridge 'locks\research.lock'))) {
            Control @('--lifecycle', 'STOPPED') | Out-Null; OpLog 'STOPPED'
            Write-Host 'STOPPED: no new work, no periodic checks, no live agent process.' -ForegroundColor Green
        } else {
            Write-Host 'PARTIALLY_STOPPED: no new work will be launched, but something is still live:' -ForegroundColor Yellow
            if ($xs.active_task) { Write-Host "  Codex worker owns $($xs.active_task.task_id) (phase $($xs.active_task.phase), experiment=$($xs.active_task.running_experiment))" }
            if ($live.Count) { Write-Host ("  agent processes: " + ($live -join ', ')) }
            Write-Host "  It stops at its next safe checkpoint and reports honestly. Run 'bridgectl end' again later to finalize STOPPED."
            Write-Host '  Emergency only: Stop-Process -Id <pid> (and any training python it started).'
        }
    }
    'request' {
        if (-not $Text) { throw 'Usage: bridgectl request "your words" [-Type IDEA|REDIRECT|REVIEW|CONFIGURE|QUESTION] [-Effective NEXT_ROUND|IMMEDIATE|REFERENCE_ONLY]' }
        $r = Py @($CB, 'request', '--type', $Type, '--effective', $Effective, '--text', $Text)
        Write-Host "Filed $($r.request_id) ($Type, $Effective). Registered, not yet executed. While ACTIVE the Research Lead is launched at once to acknowledge it; otherwise at the next start." -ForegroundColor Cyan
    }
}
