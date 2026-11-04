<#
.SYNOPSIS
  V3.0 event-driven dispatcher for the Claude Code x Codex file bridge.

.DESCRIPTION
  Replaces the two native scheduled dispatchers (protocol section 6) with ONE local
  FileSystemWatcher process.

  Phase A  = this script: on a bridge file change it runs the standard-library probes
             (dispatcher.py probe / claude_bridge.py probe). No model call, no tokens.
  Phase B  = only when a probe returns CANDIDATE (Codex) or WORK (Claude) does it launch
             `codex exec` / `claude -p` with the worker prompt. When one agent publishes,
             the other starts within the debounce interval (default 1.5 s).

  The watcher never writes task/result/signal/Claude/Operator files. Its only writes are
  .bridge/watcher/* and the idempotent Codex lifecycle readback `dispatcher.py record-watcher`
  (plus `connectivity-check` while STARTING and the mechanical `claude_bridge.py resignal`).

  Lifecycle (protocol section 8):
    IDLE / STOPPING / STOPPED : launches nothing; no periodic rescans; reacts only to CONTROL.json.
    ACTIVE + non-RUN mode     : the probes themselves decide (e.g. Claude may finish a review).
    STARTING / ACTIVE + RUN   : normal event-driven operation plus a cheap safety rescan.

  It also keeps the read-only live view (live_view.py) running in the background: an HTTP health
  check every 30 s and a restart if it stopped answering. Disable with "live_view": {"enabled": false}.

  Stopping this window does NOT stop an agent already running (it has its own hidden console);
  it finishes its current task. A restarted watcher re-adopts such processes from WATCHER_STATE.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .bridge\watcher\bridge_watcher.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File .bridge\watcher\bridge_watcher.ps1 -Once -DryRun
#>
[CmdletBinding()]
param(
    [string]$Root,
    [string]$ConfigPath,
    [switch]$Once,      # run a single dispatch pass, then exit (diagnostics)
    [switch]$DryRun     # never launch agents; log what would be launched
)

$ErrorActionPreference = 'Stop'
if (-not $Root) { $Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path }
$Root = (Resolve-Path $Root).Path
if (-not $ConfigPath) { $ConfigPath = Join-Path $PSScriptRoot 'watcher_config.json' }

$OnWindows = ($PSVersionTable.PSEdition -ne 'Core') -or $IsWindows
$Bridge = Join-Path $Root '.bridge'
$WDir = Join-Path $Bridge 'watcher'
$LogDir = Join-Path $WDir 'logs'
$StateFile = Join-Path $WDir 'WATCHER_STATE.json'
$LogFile = Join-Path $WDir 'watcher.log'
$Dispatcher = Join-Path $Bridge 'dispatcher.py'
$ClaudeBridge = Join-Path $Bridge 'claude_bridge.py'
$Utf8 = New-Object System.Text.UTF8Encoding($false)
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
function CfgVal($obj, [string]$name, $default) {
    if ($null -ne $obj -and $obj.PSObject.Properties.Name -contains $name -and $null -ne $obj.$name) { return $obj.$name }
    return $default
}
$Py = [string](CfgVal $Cfg 'python' 'python')
$DebounceMs = [int](CfgVal $Cfg 'debounce_ms' 1500)
$RescanSec = [int](CfgVal $Cfg 'rescan_seconds' 60)
$MaxAttempts = [int](CfgVal $Cfg 'max_attempts_per_state' 2)
$RetryDelaySec = [int](CfgVal $Cfg 'retry_delay_seconds' 120)
$HeartbeatSec = [int](CfgVal $Cfg 'heartbeat_seconds' 300)
# Read-only live view (live_view.py): kept running in the background by this watcher.
$LiveViewPy = Join-Path $WDir 'live_view.py'
$LvCfg = CfgVal $Cfg 'live_view' $null
$LvEnabled = [bool](CfgVal $LvCfg 'enabled' $true)
$LvPort = [int](CfgVal $LvCfg 'port' 8765)
$LvCheckSec = 30

$env:PYTHONIOENCODING = 'utf-8'
if ($OnWindows) {
    # A window opened by double-click inherits Explorer's PATH, which misses tools installed since
    # login. Rebuild PATH from the registry (Machine + User) and keep anything already present.
    $parts = @()
    foreach ($scope in @('Machine', 'User')) { $v = [Environment]::GetEnvironmentVariable('Path', $scope); if ($v) { $parts += $v.Split(';') } }
    $parts += $env:Path.Split(';')
    $seen = @{}; $clean = @()
    foreach ($x in $parts) { $t = $x.Trim(); if ($t -and -not $seen.ContainsKey($t.ToLowerInvariant())) { $seen[$t.ToLowerInvariant()] = 1; $clean += $t } }
    $env:Path = $clean -join ';'
}
try { [Console]::OutputEncoding = $Utf8 } catch { }

# ------------------------------------------------------------------ utilities

function Write-Log([string]$msg, [string]$level = 'INFO') {
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $level, $msg
    [System.IO.File]::AppendAllText($LogFile, $line + [Environment]::NewLine, $Utf8)
    $color = switch ($level) { 'WARN' { 'Yellow' } 'ERROR' { 'Red' } 'LAUNCH' { 'Cyan' } 'DONE' { 'Green' } default { 'Gray' } }
    Write-Host $line -ForegroundColor $color
}

function Write-JsonAtomic([string]$path, $obj) {
    $tmp = $path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    [System.IO.File]::WriteAllText($tmp, ($obj | ConvertTo-Json -Depth 8), $Utf8)
    if (Test-Path -LiteralPath $path) { [System.IO.File]::Replace($tmp, $path, [NullString]::Value) }
    else { [System.IO.File]::Move($tmp, $path) }
}

function Read-JsonFile([string]$path) {
    try { return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

function Invoke-Py([string[]]$argv) {
    # Returns @{ ok; json; raw; text }. Never throws on Python stderr/exit codes.
    $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $Py @argv 2>&1 | ForEach-Object { "$_" })
        $code = $LASTEXITCODE
    } catch {
        return @{ ok = $false; json = $null; raw = ''; text = $_.Exception.Message; code = -1 }
    } finally { $ErrorActionPreference = $prev }
    $raw = ($lines | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1)
    $obj = $null
    if ($raw) { try { $obj = $raw | ConvertFrom-Json } catch { $obj = $null } }
    return @{ ok = ($null -ne $obj); json = $obj; raw = [string]$raw; text = ($lines -join "`n"); code = $code }
}

function Quote-Arg([string]$a) {
    if ($a -match '^[A-Za-z0-9_\-\.:/\\=,+@]+$') { return $a }
    return '"' + ($a -replace '"', '\"') + '"'
}

function Resolve-Exe([string]$command) {
    if ([System.IO.Path]::IsPathRooted($command) -and (Test-Path -LiteralPath $command)) { return $command }
    # -CommandType Application skips npm's *.ps1 shims and returns .exe/.cmd.
    $c = Get-Command $command -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Source }
    # Common install locations that are often missing from a stale PATH.
    $dirs = @()
    foreach ($v in @('APPDATA', 'USERPROFILE', 'LOCALAPPDATA')) {
        $base = [Environment]::GetEnvironmentVariable($v)
        if (-not $base) { continue }
        if ($v -eq 'APPDATA') { $dirs += (Join-Path $base 'npm') }
        if ($v -eq 'USERPROFILE') { $dirs += (Join-Path $base '.local\bin') }
        if ($v -eq 'LOCALAPPDATA') { $dirs += (Join-Path $base 'Programs\claude'); $dirs += (Join-Path $base 'Programs\codex') }
    }
    foreach ($d in $dirs) {
        foreach ($ext in @('.exe', '.cmd', '.bat')) {
            $f = Join-Path $d ($command + $ext)
            if (Test-Path -LiteralPath $f) { return $f }
        }
    }
    return $null
}

function Test-LiveView {
    # True when this project's live view answers /health. No proxy, short timeout, never throws.
    $lv = Read-JsonFile (Join-Path $WDir 'LIVE_VIEW.json')
    if ($null -eq $lv -or -not $lv.port) { return $false }
    try {
        $req = [System.Net.WebRequest]::Create("http://127.0.0.1:$([int]$lv.port)/health")
        $req.Proxy = $null; $req.Timeout = 3000; $req.ReadWriteTimeout = 3000
        $resp = $req.GetResponse()
        try { $sr = New-Object System.IO.StreamReader($resp.GetResponseStream()); $h = $sr.ReadToEnd() | ConvertFrom-Json } finally { $resp.Close() }
        return ([bool]$h.ok -and [string]$h.instance -eq [string]$lv.instance)
    } catch { return $false }
}

function Ensure-LiveView {
    # Starts the hidden viewer when it is not answering; it survives this window and is re-checked every $LvCheckSec s.
    if (-not $LvEnabled -or $DryRun -or -not (Test-Path -LiteralPath $LiveViewPy)) { return }
    if (Test-LiveView) { return }
    $r = Invoke-Py @($LiveViewPy, 'ensure', '--port', [string]$LvPort)
    if ($r.ok -and $r.json.url) {
        $verb = if ($r.json.action -eq 'STARTED') { 'started' } else { 'running' }
        Write-Log "Live view $verb at $($r.json.url) (read-only; open it with live_view.cmd or bridgectl view)."
    } else {
        Note-Once 'liveview' "Live view could not be started: $($r.raw)$(if (-not $r.ok) { $r.text }). See .bridge\watcher\logs\live_view.out.log." 'WARN'
    }
}

# ------------------------------------------------------------------ single instance

$sha = [System.Security.Cryptography.SHA256]::Create()
$rootKey = ([BitConverter]::ToString($sha.ComputeHash($Utf8.GetBytes($Root.ToLowerInvariant())))).Replace('-', '').Substring(0, 16)
$mutex = New-Object System.Threading.Mutex($false, "bridge-watcher-$rootKey")
$haveMutex = $false
try { $haveMutex = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $haveMutex = $true }
if (-not $haveMutex) {
    Write-Host 'Another bridge watcher is already running for this project. Exiting.' -ForegroundColor Yellow
    exit 2
}

$WatcherId = 'watch-{0}-{1}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $PID
$StartedAt = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')

# ------------------------------------------------------------------ agent bookkeeping

function New-AgentRecord([string]$name) {
    return [ordered]@{ name = $name; process = $null; pid = $null; start_time = $null; fingerprint = $null;
        attempts = 0; last_exit = $null; next_retry = [datetime]::MinValue; stalled = $false;
        log_base = $null; launched_at = $null; reason = $null; exe_missing_logged = $false }
}
$Agents = [ordered]@{ claude = (New-AgentRecord 'claude'); codex = (New-AgentRecord 'codex') }
$LastSummary = ''
$ResignalTried = $null
$LastNotes = @{}

function Save-WatcherState([string]$status = 'RUNNING', $extra = $null) {
    $agentInfo = [ordered]@{}
    foreach ($a in $Agents.Values) {
        $agentInfo[$a.name] = [ordered]@{ running = (Test-AgentRunning $a); pid = $a.pid; start_time = $a.start_time;
            launched_at = $a.launched_at; fingerprint = $a.fingerprint; attempts = $a.attempts;
            last_exit = $a.last_exit; stalled = $a.stalled; log_base = $a.log_base }
    }
    $obj = [ordered]@{ watcher_id = $WatcherId; status = $status; pid = $PID; host = [Environment]::MachineName;
        project_root = $Root; started_at = $StartedAt; heartbeat_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz');
        dry_run = [bool]$DryRun; last_summary = $script:LastSummary; agents = $agentInfo }
    Write-JsonAtomic $StateFile $obj
}

function Test-AgentRunning($a) {
    if ($null -eq $a.process) { return $false }
    try { return -not $a.process.HasExited } catch { return $false }
}

function Note-Once([string]$key, [string]$msg, [string]$level = 'INFO') {
    # Log only when the message for this key changes (keeps the console quiet while idle).
    if ($LastNotes[$key] -ne $msg) { $LastNotes[$key] = $msg; Write-Log $msg $level }
}

function Restore-Adopted {
    $old = Read-JsonFile $StateFile
    if ($null -eq $old -or $null -eq $old.agents) { return }
    foreach ($name in @('claude', 'codex')) {
        $o = $old.agents.$name
        if ($null -eq $o -or -not $o.pid) { continue }
        try {
            $p = Get-Process -Id ([int]$o.pid) -ErrorAction Stop
            $st = $p.StartTime.ToUniversalTime().Ticks
            if ($o.start_time -and ([math]::Abs($st - [long]$o.start_time) -lt 30000000)) {   # within 3 s
                $a = $Agents[$name]
                $a.process = $p; $a.pid = $p.Id; $a.start_time = $o.start_time; $a.fingerprint = $o.fingerprint
                $a.attempts = [int]$o.attempts; $a.log_base = $o.log_base; $a.launched_at = $o.launched_at
                Write-Log "Adopted still-running $name agent (pid $($p.Id)) from previous watcher." 'WARN'
            }
        } catch { }
    }
}

# ------------------------------------------------------------------ launching

function Start-Agent([string]$name, [string]$probeRaw, [string]$reason) {
    $a = $Agents[$name]
    $ac = $Cfg.agents.$name
    $exe = Resolve-Exe ([string]$ac.command)
    if (-not $exe) {
        if (-not $a.exe_missing_logged) { Write-Log "$name CLI '$($ac.command)' not found on PATH; set agents.$name.command in watcher_config.json." 'ERROR'; $a.exe_missing_logged = $true }
        return
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $base = Join-Path $LogDir "$stamp-$name"
    $promptPath = Join-Path $Root ([string]$ac.prompt_file)
    $header = @(
        "[bridge watcher $WatcherId launched you at $((Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz'))]",
        "[trigger: $reason]",
        "[probe output that triggered this launch: $probeRaw]",
        ''
    ) -join "`n"
    $prompt = $header + (Get-Content -LiteralPath $promptPath -Raw -Encoding UTF8)
    [System.IO.File]::WriteAllText("$base.prompt.txt", $prompt, $Utf8)

    $argv = @()
    foreach ($x in @($ac.args)) {
        $argv += ([string]$x).Replace('{ROOT}', $Root).Replace('{LOGBASE}', $base).Replace('{LASTMSG}', "$base.last.txt")
    }
    $cmdline = ((@($exe) + $argv) | ForEach-Object { Quote-Arg $_ }) -join ' '
    $cmdline += ' < ' + (Quote-Arg "$base.prompt.txt") + ' > ' + (Quote-Arg "$base.out.log") + ' 2> ' + (Quote-Arg "$base.err.log")

    if ($DryRun) {
        Write-Log "DRY-RUN would launch $name ($reason): $cmdline" 'LAUNCH'
        $a.fingerprint = $probeRaw; $a.attempts = $MaxAttempts; $a.stalled = $true; $a.last_exit = 0
        return
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    if ($OnWindows) {
        $psi.FileName = $env:ComSpec
        $psi.Arguments = '/d /s /c "' + $cmdline + '"'
    } else {
        $psi.FileName = '/bin/sh'
        $psi.Arguments = '-c ' + (Quote-Arg $cmdline)
    }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true      # own hidden console: Ctrl+C in this window never reaches the agent
    $psi.WorkingDirectory = $Root
    $psi.EnvironmentVariables['BRIDGE_WATCHER_ID'] = $WatcherId
    $psi.EnvironmentVariables['BRIDGE_AGENT_ROLE'] = $name
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $p = [System.Diagnostics.Process]::Start($psi)
    $a.process = $p; $a.pid = $p.Id; $a.start_time = $p.StartTime.ToUniversalTime().Ticks
    $a.launched_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz'); $a.reason = $reason; $a.log_base = $base
    if ($a.fingerprint -ne $probeRaw) { $a.attempts = 0 }
    $a.fingerprint = $probeRaw; $a.attempts++; $a.stalled = $false; $a.last_exit = $null
    Write-Log "Launched $name (pid $($p.Id), attempt $($a.attempts)) for: $reason. Logs: $base.*" 'LAUNCH'
    Save-WatcherState
}

function Consider-Launch([string]$name, [string]$probeRaw, [string]$reason) {
    $a = $Agents[$name]
    if (-not [bool](CfgVal $Cfg.agents.$name 'enabled' $true)) { Note-Once "$name-disabled" "$name has work ($reason) but is disabled in watcher_config.json." 'WARN'; return }
    if ($null -ne $a.process) {
        if (Test-AgentRunning $a) { return }
        Check-Exits | Out-Null   # it exited during this pass: record the exit before deciding
    }
    if ($a.fingerprint -ne $probeRaw) {
        $a.attempts = 0; $a.stalled = $false; $a.next_retry = [datetime]::MinValue; $a.last_exit = $null
        Start-Agent $name $probeRaw $reason; return
    }
    # Same work is still pending after an agent run for it ended.
    if ($a.stalled) { return }
    if ($a.attempts -ge 1 -and $a.last_exit -eq 0) {
        $a.stalled = $true
        Write-Log "$name exited normally but its work is unchanged ($reason). Not relaunching; check $($a.log_base).out.log. A file change or a watcher restart re-arms it." 'WARN'
        try { [Console]::Beep(880, 300) } catch { }
        Save-WatcherState; return
    }
    if ($a.attempts -ge $MaxAttempts) {
        $a.stalled = $true
        Write-Log "$name failed $($a.attempts) times on the same work ($reason). Stopped retrying (protocol 10.4); see $($a.log_base).err.log." 'ERROR'
        try { [Console]::Beep(440, 600) } catch { }
        Save-WatcherState; return
    }
    if ((Get-Date) -lt $a.next_retry) { return }
    Start-Agent $name $probeRaw "$reason (retry)"
}

function Check-Exits {
    $changed = $false
    foreach ($a in $Agents.Values) {
        if ($null -eq $a.process) { continue }
        $exited = $false
        try { $exited = $a.process.HasExited } catch { $exited = $true }
        if (-not $exited) { continue }
        $code = $null
        try { $code = $a.process.ExitCode } catch { $code = 0 }   # adopted processes: unknown -> conservative
        if ($null -eq $code) { $code = 0 }
        $a.process = $null; $a.pid = $null; $a.last_exit = $code
        if ($code -ne 0) { $a.next_retry = (Get-Date).AddSeconds($RetryDelaySec) }
        $lvl = 'DONE'; if ($code -ne 0) { $lvl = 'WARN' }
        Write-Log "$($a.name) exited with code $code. Logs: $($a.log_base).*" $lvl
        $changed = $true
    }
    if ($changed) { Save-WatcherState }
    return $changed
}

# ------------------------------------------------------------------ Phase A dispatch (zero tokens)

function Get-Control { return Read-JsonFile (Join-Path $Bridge 'operator\CONTROL.json') }

function Record-Watcher([string]$status) {
    $r = Invoke-Py @($Dispatcher, 'record-watcher', '--status', $status, '--watcher-id', $WatcherId)
    $act = if ($r.ok) { $r.json.action } else { 'ERROR' }
    if ($act -eq 'SCHEDULER_RECORDED') { Write-Log "Codex lifecycle recorded: watcher $status (CODEX_STATE.lifecycle=$($r.json.lifecycle))." }
    elseif ($act -ne 'ALREADY_RECORDED') {
        $why = if ($r.ok) { $r.json.reason } else { $r.text }
        Note-Once 'record' "record-watcher $status did not record: $why" 'WARN'
    }
}

function Invoke-Resignal([string]$probeRaw) {
    # Purely mechanical Research-Lead step: re-point the unfinished task at the new run_id.
    # Attempted once per probe state, so a failure can never become a lock/unlock loop.
    if ($script:ResignalTried -eq $probeRaw) { return }
    $script:ResignalTried = $probeRaw
    $lk = Invoke-Py @($ClaudeBridge, 'lock')
    if (-not $lk.ok -or $lk.json.action -ne 'LOCKED') { Note-Once 'resignal' "resignal: research.lock busy ($($lk.raw))" 'WARN'; return }
    $id = $lk.json.execution_id
    try {
        $again = Invoke-Py @($ClaudeBridge, 'probe', '--ignore-lock')
        if ($again.ok -and $again.json.action -eq 'WORK' -and (@($again.json.reasons) -join ',') -eq 'RESIGNAL_RUN') {
            $r = Invoke-Py @($ClaudeBridge, 'resignal', '--execution-id', $id)
            $lvl = 'INFO'; if (-not $r.ok -or $r.json.action -eq 'ERROR') { $lvl = 'ERROR' }
            Write-Log "Resignalled unfinished task to the current run: $($r.raw)" $lvl
        } else {
            Write-Log "resignal skipped; state changed under the lock: $($again.raw)" 'WARN'
        }
    } finally {
        $u = Invoke-Py @($ClaudeBridge, 'unlock', '--execution-id', $id)
        if (-not $u.ok -or $u.json.action -ne 'UNLOCKED') { Write-Log "research.lock could not be released: $($u.raw)" 'ERROR' }
    }
}

function Invoke-Dispatch([string]$why) {
    Check-Exits | Out-Null       # an exit must be recorded before deciding on a relaunch
    $ctl = Get-Control
    $lifecycle = if ($ctl) { [string]$ctl.lifecycle } else { 'MISSING' }
    $mode = if ($ctl) { [string]$ctl.mode } else { 'MISSING' }

    $cx = Invoke-Py @($Dispatcher, 'probe')
    $cxAct = if ($cx.ok) { [string]$cx.json.action } else { 'PROBE_ERROR' }

    if ($cxAct -eq 'LIFECYCLE_STOP') {
        Record-Watcher 'PAUSED'          # closes the run in CODEX_STATE; idempotent
    } elseif ($cxAct -eq 'CONNECTIVITY_ONLY') {
        Record-Watcher 'ACTIVE'
        $c = Invoke-Py @($Dispatcher, 'connectivity-check')
        Note-Once 'connectivity' "connectivity-check: $($c.raw)"
    } elseif ($lifecycle -in @('STARTING', 'ACTIVE') -and $mode -eq 'RUN') {
        Record-Watcher 'ACTIVE'
    }

    $clAct = 'SKIPPED'
    if ($lifecycle -in @('STARTING', 'ACTIVE')) {
        if ($cxAct -eq 'CANDIDATE') { Consider-Launch 'codex' $cx.raw "Codex CANDIDATE $($cx.json.task_id)" }
        $cl = Invoke-Py @($ClaudeBridge, 'probe')
        $clAct = if ($cl.ok) { [string]$cl.json.action } else { 'PROBE_ERROR' }
        if ($clAct -eq 'WORK') {
            $reasons = @($cl.json.reasons) -join ','
            if ($reasons -eq 'RESIGNAL_RUN' -and -not (Test-AgentRunning $Agents.claude)) { Invoke-Resignal $cl.raw }
            else { Consider-Launch 'claude' $cl.raw "Claude WORK [$reasons]" }
        } elseif ($clAct -eq 'BLOCKED' -or $clAct -eq 'PROBE_ERROR') {
            Note-Once 'claude-probe' "Claude probe: $($cl.raw)$($cl.text)" 'WARN'
        }
    }
    if ($cxAct -in @('BLOCKED', 'RECOVERY_REQUIRED', 'PROBE_ERROR', 'LIMIT_REACHED', 'AWAITING_R0000')) {
        Note-Once 'codex-probe' "Codex probe: $($cx.raw)$(if (-not $cx.ok) { $cx.text })" 'WARN'
    }
    $summary = "CONTROL $lifecycle/$mode | codex=$cxAct | claude=$clAct | running: claude=$(Test-AgentRunning $Agents.claude) codex=$(Test-AgentRunning $Agents.codex)"
    if ($summary -ne $script:LastSummary) { $script:LastSummary = $summary; Write-Log "$summary  ($why)"; Save-WatcherState }
    $script:Life = $lifecycle
}

# ------------------------------------------------------------------ main loop

$fsw = $null
try {
    Restore-Adopted
    Save-WatcherState 'RUNNING'
    Write-Log "Bridge watcher $WatcherId started for $Root (debounce ${DebounceMs}ms, dry-run=$([bool]$DryRun))."
    foreach ($n in @('claude', 'codex')) {
        $exe = Resolve-Exe ([string]$Cfg.agents.$n.command)
        if ($exe) { Write-Log "$n CLI: $exe" } else { Write-Log "$n CLI '$($Cfg.agents.$n.command)' NOT found on PATH." 'WARN' }
    }

    $script:Life = 'UNKNOWN'
    Invoke-Dispatch 'startup' | Out-Null
    if ($Once) { return }
    try { Ensure-LiveView } catch { Write-Log "Live view check failed: $($_.Exception.Message)" 'WARN' }

    $fsw = New-Object System.IO.FileSystemWatcher $Bridge
    $fsw.IncludeSubdirectories = $true
    $fsw.InternalBufferSize = 65536
    $fsw.NotifyFilter = [System.IO.NotifyFilters]'FileName, DirectoryName, LastWrite, Size'
    foreach ($ev in @('Changed', 'Created', 'Deleted', 'Renamed')) {
        Register-ObjectEvent -InputObject $fsw -EventName $ev -SourceIdentifier "bridge.$ev" | Out-Null
    }
    Register-ObjectEvent -InputObject $fsw -EventName Error -SourceIdentifier 'bridge.Error' | Out-Null
    $fsw.EnableRaisingEvents = $true

    $relevant = '^(signals|operator|locks)([\\/]|$)|^(CLAUDE_STATE|CODEX_STATE)\.json$'
    $dirty = $false
    $lastEvent = Get-Date
    $lastScan = Get-Date
    $lastBeat = Get-Date
    $lastLv = Get-Date
    while ($true) {
        Wait-Event -Timeout 1 | Out-Null
        foreach ($e in @(Get-Event -ErrorAction SilentlyContinue)) {
            if ($e.SourceIdentifier -like 'bridge.*') {
                if ($e.SourceIdentifier -eq 'bridge.Error') { $dirty = $true; $lastEvent = Get-Date; Write-Log 'FileSystemWatcher buffer overflow; forcing a full dispatch.' 'WARN' }
                else {
                    $full = [string]$e.SourceEventArgs.FullPath
                    if ($full.Length -gt $Bridge.Length) {
                        $rel = $full.Substring($Bridge.Length).TrimStart('\', '/')
                        if ($rel -notmatch '\.tmp$' -and $rel -match $relevant) { $dirty = $true; $lastEvent = Get-Date }
                    }
                }
            }
            Remove-Event -EventIdentifier $e.EventIdentifier -ErrorAction SilentlyContinue
        }
        $now = Get-Date
        if ((Check-Exits) -eq $true) { Invoke-Dispatch 'agent exited' | Out-Null; $lastScan = $now; continue }
        if ($dirty -and ($now - $lastEvent).TotalMilliseconds -ge $DebounceMs) {
            $dirty = $false; Invoke-Dispatch 'file change' | Out-Null; $lastScan = $now
        } elseif ($script:Life -in @('STARTING', 'ACTIVE') -and ($now - $lastScan).TotalSeconds -ge $RescanSec) {
            # Safety net for missed events and retry timers. Python only; never a model call.
            # Disabled outside STARTING/ACTIVE, so STOPPED really has no periodic checking (8.4).
            Invoke-Dispatch 'rescan' | Out-Null; $lastScan = $now
        }
        if (($now - $lastBeat).TotalSeconds -ge $HeartbeatSec) { Save-WatcherState; $lastBeat = $now }
        if (($now - $lastLv).TotalSeconds -ge $LvCheckSec) {
            # Viewer upkeep only: an HTTP health check, and a restart if it stopped answering. Never dispatches.
            $lastLv = $now
            try { Ensure-LiveView } catch { Note-Once 'liveview' "Live view check failed: $($_.Exception.Message)" 'WARN' }
        }
    }
} finally {
    if ($fsw) { $fsw.EnableRaisingEvents = $false; $fsw.Dispose() }
    foreach ($ev in @('Changed', 'Created', 'Deleted', 'Renamed', 'Error')) { Unregister-Event -SourceIdentifier "bridge.$ev" -ErrorAction SilentlyContinue }
    try {
        Save-WatcherState 'STOPPED'
        $live = @($Agents.Values | Where-Object { Test-AgentRunning $_ } | ForEach-Object { "$($_.name) pid $($_.pid)" })
        if ($live.Count) { Write-Log ("Watcher stopped. Still running (will finish on their own): " + ($live -join ', ')) 'WARN' }
        else { Write-Log 'Watcher stopped.' }
    } catch { }
    if ($haveMutex) { try { $mutex.ReleaseMutex() } catch { } }
}
