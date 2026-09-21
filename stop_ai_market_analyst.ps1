# Stop All Services for AI Market Analyst V2
$ProjectRoot = $PSScriptRoot

Write-Host "Shutting down AI Market Analyst V2..." -ForegroundColor Cyan

# 1. Stop Backend
$backendPidFile = Join-Path $ProjectRoot "logs\backend.pid"
if (Test-Path $backendPidFile) {
    $bPid = (Get-Content $backendPidFile).Trim()
    if ($bPid) {
        try {
            Stop-Process -Id $bPid -Force -ErrorAction SilentlyContinue
            Write-Host "[OK] Stopped backend service (PID: $bPid)" -ForegroundColor Green
        } catch {}
    }
    Remove-Item $backendPidFile -Force -ErrorAction SilentlyContinue
}

# 2. Stop Bonsai Model Server
& powershell -File (Join-Path $ProjectRoot "infra\bonsai\stop_model.ps1")

# 3. Clean up any remaining zombie uvicorn / llama-server processes
$zombies = Get-Process | Where-Object { $_.ProcessName -eq "llama-server" }
if ($zombies) {
    $zombies | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Host "[WARN] Cleaned up lingering llama-server processes" -ForegroundColor Yellow
}

Write-Host "[OK] All AI Market Analyst V2 services have been terminated safely." -ForegroundColor Green
