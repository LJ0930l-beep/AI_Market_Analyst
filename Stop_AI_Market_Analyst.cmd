@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\desktop-client.ps1" -Action stop
if errorlevel 1 pause
endlocal
