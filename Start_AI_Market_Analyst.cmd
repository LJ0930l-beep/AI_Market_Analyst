@echo off
setlocal
cd /d "%~dp0"
echo Starting AI Market Analyst desktop client...
echo (V2.0 runs as a Tauri window; there is no browser URL to open.)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\desktop-client.ps1" -Action start
if errorlevel 1 (
  echo.
  echo Start failed. Review the message above.
  echo Unrelated processes ^(Python, Node, Ollama, ComfyUI^) were not touched.
  pause
  exit /b 1
)
endlocal
