# Read-only live view of the two bridge agents, served as a local page with foldable rows (live_view.html).
# Codex: newest rollout under $CODEX_HOME\sessions (default ~\.codex\sessions) whose cwd is this project.
# Claude: newest .bridge\watcher\logs\*-claude.out.log. Opens agent files for reading only, listens on
# localhost only, never writes bridge state. Start: live_view.cmd or `bridgectl view`. Stop: close its window.
param([int]$Port = 8765, [switch]$NoBrowser)

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } elseif ($env:USERPROFILE) { Join-Path $env:USERPROFILE '.codex' } else { Join-Path $HOME '.codex' }
$sessions = Join-Path $codexHome 'sessions'
$leaf = Split-Path $root -Leaf
$rootNorm = $root.TrimEnd('\', '/').Replace('/', '\').ToLowerInvariant()
$stateFile = Join-Path $PSScriptRoot 'LIVE_VIEW.json'
$logs = Join-Path $root '.bridge\watcher\logs'
$pageFile = Join-Path $PSScriptRoot 'live_view.html'
$utf8 = New-Object Text.UTF8Encoding($false)
$codex = @{ path = $null; pos = 0L }
$claude = @{ path = $null; pos = 0L; live = $false }
$checked = @{}
$events = New-Object System.Collections.ArrayList

# Display only: turns the JSON escapes in tool calls and tool output back into real line breaks and characters.
function Readable($text) {
    $t = ([string]$text) -replace '(?<!\\)\\r\\n|(?<!\\)\\n', "`n" -replace '(?<!\\)\\t', "`t" -replace '(?<!\\)\\"', '"'
    $t = $t -replace '\\u003c', '<' -replace '\\u003e', '>' -replace '\\u0026', '&' -replace '\\u0027', "'" -replace '\\\\', '\'
    return $t.TrimEnd()
}

function Show($time, $agent, $kind, $text, $summary) {
    $text = [string]$text
    # Keep memory bounded on long runs; the agent's own log keeps the full text.
    if ($text.Length -gt 200000) { $text = $text.Substring(0, 200000) + "`n... (truncated for display; the full text is in the agent log)" }
    [void]$events.Add(@{ time = $time; agent = $agent; kind = $kind; text = $text; summary = [string]$summary })
}

# Counts up while Codex has not written its next event; hidden after 10 minutes in case it exited abnormally.
function Status-Text {
    if (-not $codex.last -or $codex.kind -eq 'done') { return '' }
    $secs = [Math]::Max(0, [int]((Get-Date) - $codex.last).TotalSeconds)
    if ($secs -gt 600) { return '' }
    $what = if ($codex.kind -in @('custom_tool_call', 'function_call')) { 'tool' } else { 'model' }
    return "$what $secs"
}

function Local-Time($stamp) {
    try { return ([DateTime]::Parse([string]$stamp)).ToLocalTime().ToString('HH:mm:ss') } catch { return (Get-Date).ToString('HH:mm:ss') }
}

function Read-NewLines($s) {
    if (-not $s.path) { return }
    $fs = [IO.File]::Open($s.path, 'Open', 'Read', 'ReadWrite,Delete')
    try {
        if ($fs.Length -le $s.pos) { return }
        $fs.Position = $s.pos
        $n = [int][Math]::Min($fs.Length - $s.pos, 16MB)
        $b = New-Object byte[] $n
        $r = $fs.Read($b, 0, $n)
        $last = -1
        for ($i = $r - 1; $i -ge 0; $i--) { if ($b[$i] -eq 10) { $last = $i; break } }
        if ($last -lt 0) { return }
        $s.pos += $last + 1
        $utf8.GetString($b, 0, $last).Split("`n")
    } finally { $fs.Dispose() }
}

# A rollout belongs to this project when its first line (session_meta) records this project as cwd.
function Is-ProjectRollout($file) {
    if ($checked.ContainsKey($file)) { return $checked[$file] }
    $first = ''
    try {
        $fs = [IO.File]::Open($file, 'Open', 'Read', 'ReadWrite,Delete')
        try { $sr = New-Object IO.StreamReader($fs, $utf8); $first = [string]$sr.ReadLine() } finally { $fs.Dispose() }
    } catch { return $false }
    $cwd = $null
    try { $cwd = [string](($first | ConvertFrom-Json).payload.cwd) } catch { }
    if (-not $cwd) { return $first.Contains($leaf) }   # unparsed or unknown format: folder-name fallback, re-checked later
    $hit = ($cwd.TrimEnd('\', '/').Replace('/', '\').ToLowerInvariant() -eq $rootNorm)
    $checked[$file] = $hit
    return $hit
}

function Find-Latest {
    if (Test-Path $sessions) {
        $latest = Get-ChildItem $sessions -Recurse -Filter 'rollout-*.jsonl' | Sort-Object Name -Descending | Where-Object { Is-ProjectRollout $_.FullName } | Select-Object -First 1
        if ($latest -and $latest.FullName -ne $codex.path) {
            $codex.path = $latest.FullName; $codex.pos = 0L; $codex.last = $null; $codex.kind = $null
            Show '' 'codex' 'session' $latest.Name ''
        }
    }
    $log = Get-ChildItem $logs -Filter '*-claude.out.log' -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
    if ($log -and $log.FullName -ne $claude.path) {
        $claude.path = $log.FullName; $claude.pos = 0L
        Show '' 'claude' 'session' $log.Name ''
    }
}

function Output-Text($x) {
    if ($x -is [string]) { return $x }
    return (@($x) | ForEach-Object { if ($_.text) { $_.text } elseif ($_.content) { Output-Text $_.content } else { $_ | ConvertTo-Json -Compress -Depth 4 } }) -join ' '
}

function Show-Codex($line) {
    try { $o = $line | ConvertFrom-Json } catch { return }
    $p = $o.payload; $t = Local-Time $o.timestamp
    # A reasoning row is written when reasoning ends, so its duration is the gap since the previous row.
    # item_completed rows are skipped: each one lands right before the item it reports.
    $prev = $codex.last
    $when = $null
    try { $when = [DateTime]::Parse([string]$o.timestamp) } catch { }
    if ($when -and -not ($o.type -eq 'event_msg' -and $p.type -eq 'item_completed')) { $codex.last = $when }
    if ($o.type -eq 'event_msg' -and $p.type -in @('task_complete', 'turn_aborted')) { $codex.kind = 'done' }
    if ($o.type -ne 'response_item') { return }
    $codex.kind = $p.type
    switch ($p.type) {
        'message' {
            if ($p.role -eq 'assistant') { Show $t 'codex' 'say' (($p.content | ForEach-Object { $_.text }) -join ' ') '' }
        }
        'reasoning' {
            $took = if ($prev -and $when) { '{0}s' -f [int][Math]::Round(($when - $prev).TotalSeconds) } else { '?s' }
            Show $t 'codex' 'think' (($p.summary | ForEach-Object { $_.text }) -join ' ') $took
        }
        'custom_tool_call' { Show $t 'codex' 'run' (Readable $p.input) '' }
        'function_call' { Show $t 'codex' 'run' (Readable "$($p.name) $($p.arguments)") '' }
        'custom_tool_call_output' { Show $t 'codex' 'output' (Readable (Output-Text $p.output)) '' }
        'function_call_output' { Show $t 'codex' 'output' (Readable (Output-Text $p.output)) '' }
    }
}

function Show-Claude($line) {
    try { $o = $line | ConvertFrom-Json } catch { return }
    # Log rows carry no reliable time, so rows replayed at startup are left without one.
    $t = if ($o.timestamp) { Local-Time $o.timestamp } elseif ($claude.live) { (Get-Date).ToString('HH:mm:ss') } else { '' }
    foreach ($c in @($o.message.content)) {
        if ($null -eq $c) { continue }
        switch ($c.type) {
            'text' { Show $t 'claude' 'say' $c.text '' }
            'thinking' { Show $t 'claude' 'think' $c.thinking '' }
            'tool_use' {
                $what = @($c.input.description, $c.input.file_path, $c.input.pattern) | Where-Object { $_ } | Select-Object -First 1
                $brief = if ($what) { "$($c.name): $what" } else { [string]$c.name }
                Show $t 'claude' 'run' (Readable ("$($c.name) " + ($c.input | ConvertTo-Json -Compress -Depth 4))) $brief
            }
            'tool_result' { Show $t 'claude' 'output' (Output-Text $c.content) '' }
        }
    }
    if ($o.type -eq 'result') { Show $t 'claude' 'done' $o.result '' }
}

function Serve($ctx) {
    $res = $ctx.Response
    try {
        if ($ctx.Request.Url.AbsolutePath -eq '/events') {
            $since = 0
            [void][int]::TryParse($ctx.Request.QueryString['since'], [ref]$since)
            $slice = @()
            if ($since -lt $events.Count) { $slice = $events.GetRange($since, [Math]::Min($events.Count - $since, 200)).ToArray() }
            $json = ConvertTo-Json -InputObject @{ total = $events.Count; status = (Status-Text); project = $leaf; events = @($slice) } -Depth 3 -Compress
            $bytes = $utf8.GetBytes($json)
            $res.ContentType = 'application/json; charset=utf-8'
        } else {
            $bytes = [IO.File]::ReadAllBytes($pageFile)
            $res.ContentType = 'text/html; charset=utf-8'
        }
        $res.Headers.Add('Cache-Control', 'no-store')
        $res.ContentLength64 = $bytes.Length
        $res.OutputStream.Write($bytes, 0, $bytes.Length)
    } catch { } finally { $res.Close() }
}

function Open-Page([int]$p) {
    if ($NoBrowser) { return }
    try { Start-Process "http://localhost:$p/" } catch { Write-Host "Open http://localhost:$p/ in your browser." }
}

# One viewer per project: if this project's viewer is already serving, just open it.
try {
    $old = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
    $probe = Invoke-WebRequest -Uri "http://localhost:$($old.port)/events?since=2147483647" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
    if ((($probe.Content | ConvertFrom-Json).project) -eq $leaf) {
        Write-Host "The live view for $leaf is already running at http://localhost:$($old.port)/" -ForegroundColor Cyan
        Open-Page ([int]$old.port)
        exit 0
    }
} catch { }

# First free port from $Port upward, so several projects can each have a viewer.
$listener = $null
for ($p = $Port; $p -lt $Port + 20; $p++) {
    $l = New-Object Net.HttpListener
    $l.Prefixes.Add("http://localhost:$p/")
    try { $l.Start(); $listener = $l; $Port = $p; break } catch { $l.Close() }
}
if (-not $listener) { Write-Host "No free port in $Port-$($Port + 19); pass -Port <n>." -ForegroundColor Red; exit 1 }
try { [IO.File]::WriteAllText($stateFile, (ConvertTo-Json @{ port = $Port; pid = $PID; project = $leaf } -Compress), $utf8) } catch { }
Write-Host "Live view of $leaf : http://localhost:$Port/  (read-only). Close THIS window to stop the viewer; the agents keep running." -ForegroundColor Cyan
Open-Page $Port
$task = $listener.GetContextAsync()
$tick = 0
$next = Get-Date
try {
    while ($true) {
        if ((Get-Date) -ge $next) {
            if ($tick % 5 -eq 0) { Find-Latest }
            foreach ($l in @(Read-NewLines $codex)) { if ($l) { Show-Codex $l } }
            foreach ($l in @(Read-NewLines $claude)) { if ($l) { Show-Claude $l } }
            $claude.live = $true
            $tick++
            $next = (Get-Date).AddSeconds(1)
        }
        if ($task.Wait(200)) { Serve $task.Result; $task = $listener.GetContextAsync() }
    }
} finally {
    try { $listener.Stop(); $listener.Close() } catch { }
    try { $cur = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json; if ($cur.pid -eq $PID) { Remove-Item -LiteralPath $stateFile -Force } } catch { }
}
