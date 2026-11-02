@echo off
rem Read-only live view of the Claude and Codex agents in your browser. Close this window to stop the viewer.
title Agent live view
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0live_view.ps1" %*
