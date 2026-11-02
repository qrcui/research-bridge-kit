# Installs the two CLIs the bridge watcher launches: Claude Code (claude) and Codex (codex).
# Official sources only: claude.ai native installer, npm package @openai/codex (Node via winget if missing).
$ErrorActionPreference = 'Continue'
function Refresh-Path {
    $p = @()
    foreach ($s in @('Machine', 'User')) { $v = [Environment]::GetEnvironmentVariable('Path', $s); if ($v) { $p += $v.Split(';') } }
    $env:Path = (($p + $env:Path.Split(';')) | Where-Object { $_ } | Select-Object -Unique) -join ';'
}
function Have([string]$n) { [bool](Get-Command $n -CommandType Application -ErrorAction SilentlyContinue) }
function Add-UserPath([string]$dir) {
    $u = [Environment]::GetEnvironmentVariable('Path', 'User'); if (-not $u) { $u = '' }
    if (($u.Split(';') | Where-Object { $_ -ieq $dir }).Count -eq 0) {
        [Environment]::SetEnvironmentVariable('Path', ($u.TrimEnd(';') + ';' + $dir).TrimStart(';'), 'User')
        Write-Host "Added $dir to your user PATH."
    }
}
Refresh-Path

Write-Host "`n== Claude Code CLI ==" -ForegroundColor Cyan
if (Have 'claude') { Write-Host 'Already installed.'; claude --version }
else {
    Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
    $bin = Join-Path $env:USERPROFILE '.local\bin'
    if (Test-Path (Join-Path $bin 'claude.exe')) { Add-UserPath $bin }
    Refresh-Path
}

Write-Host "`n== Codex CLI ==" -ForegroundColor Cyan
if (Have 'codex') { Write-Host 'Already installed.'; codex --version }
else {
    if (-not (Have 'npm')) {
        if (Have 'winget') {
            Write-Host 'Node.js not found; installing Node.js LTS with winget...'
            winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
            Refresh-Path
        } else { Write-Host 'Neither npm nor winget found. Install Node.js LTS from https://nodejs.org and run this again.' -ForegroundColor Red }
    }
    if (Have 'npm') { npm install -g @openai/codex; Add-UserPath (Join-Path $env:APPDATA 'npm'); Refresh-Path }
}

Write-Host "`n== Result ==" -ForegroundColor Cyan
foreach ($n in @('claude', 'codex')) {
    $c = Get-Command $n -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { Write-Host ("{0,-7} OK   {1}" -f $n, $c.Source) -ForegroundColor Green } else { Write-Host ("{0,-7} MISSING" -f $n) -ForegroundColor Red }
}

Write-Host "`n== Log in (needs you, once) ==" -ForegroundColor Cyan
if (Have 'codex') {
    $a = Read-Host 'Log in to Codex now? A browser window opens. [Y/n]'
    if ($a -notmatch '^[nN]') { codex login }
}
if (Have 'claude') {
    $a = Read-Host 'Log in to Claude Code now? It starts claude; finish the login, then type /exit. [Y/n]'
    if ($a -notmatch '^[nN]') { claude }
}
Write-Host "`nDone. Close this window and restart start_watcher.cmd; its first lines should show both CLI paths." -ForegroundColor Green
Read-Host 'Press Enter to close'
