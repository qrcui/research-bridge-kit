# Opens the read-only live view of the two bridge agents in the browser.
# The viewer itself is live_view.py (Python standard library). It runs hidden in the background: the
# watcher starts it and restarts it if it stops, so this script only makes sure it is running and opens
# the page. Stop it with: <python> .bridge\watcher\live_view.py stop   (the watcher restarts it within
# a minute unless watcher_config.json has "live_view": {"enabled": false}).
param([int]$Port = 0, [switch]$NoBrowser)

$cfg = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'watcher_config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$py = [string]$cfg.python
if (-not $Port) {
    $Port = 8765
    if ($cfg.PSObject.Properties.Name -contains 'live_view' -and $cfg.live_view.port) { $Port = [int]$cfg.live_view.port }
}
$argv = @((Join-Path $PSScriptRoot 'live_view.py'), 'open', '--port', [string]$Port)
if ($NoBrowser) { $argv += '--no-browser' }
$env:PYTHONIOENCODING = 'utf-8'
$out = @(& $py @argv 2>&1 | ForEach-Object { "$_" })
$line = $out | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1
$r = $null
if ($line) { try { $r = $line | ConvertFrom-Json } catch { } }
if ($r -and $r.url) {
    Write-Host "Live view: $($r.url)  (read-only; runs in the background, the agents are not affected)" -ForegroundColor Cyan
    exit 0
}
Write-Host "The live view could not be started:" -ForegroundColor Red
$out | ForEach-Object { Write-Host $_ }
exit 1
