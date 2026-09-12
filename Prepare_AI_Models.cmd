@echo off
setlocal
cd /d "%~dp0"
echo Preparing local Qwen 4B / 9B models. Installed models will not be downloaded again.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\prepare-ai-models.ps1"
if errorlevel 1 (
  echo Model preparation failed. No substitute model was selected.
  pause
  exit /b 1
)
pause
endlocal
