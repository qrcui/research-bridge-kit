@echo off
rem Starts the bridge watcher in this window. Close the window or press Ctrl+C to stop it.
title Bridge watcher
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bridge_watcher.ps1" %*
