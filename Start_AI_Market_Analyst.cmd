@echo off
setlocal
cd /d "%~dp0"
set "AMA_NO_BROWSER="
if /I "%~1"=="--no-browser" set "AMA_NO_BROWSER=-NoBrowser"
echo Starting AI Market Analyst V1.1...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\v11-user-launch.ps1" -Action start %AMA_NO_BROWSER%
if errorlevel 1 (
  echo.
  echo Start failed. Ollama, ComfyUI, and unrelated processes were not stopped.
  echo Review the message above. Project logs are under %%TEMP%%\ai-market-analyst-phase7-launcher
  pause
  exit /b 1
)
endlocal
