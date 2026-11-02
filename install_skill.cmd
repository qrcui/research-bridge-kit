@echo off
setlocal
set "DEST=%USERPROFILE%\.claude\skills\research-bridge"
if not exist "%DEST%" mkdir "%DEST%"
copy /Y "%~dp0skill\research-bridge\SKILL.md" "%DEST%\SKILL.md" >nul
if errorlevel 1 (echo Copy failed. & pause & exit /b 1)
> "%DEST%\KIT_PATH.txt" echo %~dp0
echo Installed the research-bridge skill to %DEST%
echo Kit location recorded: %~dp0
echo.
echo Next: open the Claude desktop app, Code tab, Local session in your new project folder,
echo and say: deploy the multi-agent research system to this project.
pause
