@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\v11-user-launch.ps1" -Action stop
if errorlevel 1 pause
endlocal
