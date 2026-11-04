@echo off
rem Opens the read-only live view of the Claude and Codex agents in your browser.
rem The viewer runs in the background (the watcher keeps it running); this window closes by itself.
title Agent live view
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0live_view.ps1" %*
if errorlevel 1 pause
