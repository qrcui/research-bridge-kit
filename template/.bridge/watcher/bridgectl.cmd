@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bridgectl.ps1" %*
