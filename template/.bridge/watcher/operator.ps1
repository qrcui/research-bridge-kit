# Opens an interactive Claude Code chat in the project root acting as the Operator Console.
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location -LiteralPath $root
$exe = Get-Command claude -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$path = if ($exe) { $exe.Source } else { Join-Path $env:USERPROFILE '.local\bin\claude.exe' }
& $path 'You are the Operator for this project. Read .bridge/watcher/OPERATOR_PROMPT.md completely and follow it for this whole conversation. Then run bridgectl status and report the current state to me in Chinese.'
