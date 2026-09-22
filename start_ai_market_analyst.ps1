# Complete Startup Script for AI Market Analyst V2 (Bonsai 2 27B)
$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Starting AI Market Analyst V2 (Bonsai 2 27B)" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# 1. Detect GPU
Write-Host "[1/9] Detecting GPU telemetry..." -ForegroundColor Yellow
$gpuName = "NVIDIA GeForce RTX 4060"
$vramUsed = "Unknown"
$vramFree = "Unknown"
try {
    $smi = nvidia-smi --query-gpu=name,memory.used,memory.free --format=csv,noheader,nounits
    $parts = $smi.Split(",")
    $gpuName = $parts[0].Trim()
    $vramUsed = "$($parts[1].Trim()) MiB"
    $vramFree = "$($parts[2].Trim()) MiB"
    Write-Host "      GPU: $gpuName | Used: $vramUsed | Free: $vramFree" -ForegroundColor Green
} catch {
    Write-Host "      [WARN] Could not query nvidia-smi directly" -ForegroundColor Yellow
}

# 2. Check Model File
Write-Host "[2/9] Checking Bonsai 2 27B model file..." -ForegroundColor Yellow
$modelPath = "D:\RJ\models\bonsai2\Ternary-Bonsai-2-27B-PTQ1_0.gguf"
if (-not (Test-Path $modelPath)) {
    Write-Host "[ERR] Model weight not found at $modelPath!" -ForegroundColor Red
    Write-Host "      Please ensure download is complete." -ForegroundColor Yellow
    exit 1
}
$modelSizeGB = [math]::Round((Get-Item $modelPath).Length / 1GB, 2)
Write-Host "      Model Weight: $modelPath ($modelSizeGB GB)" -ForegroundColor Green

# 3. Start Bonsai llama-server
Write-Host "[3/9] Launching PrismML llama-server (127.0.0.1:8080)..." -ForegroundColor Yellow
# start_model.ps1 has no idempotency guard of its own. Reloading a 27B model
# when one is already serving wastes minutes and contends for VRAM, so probe first.
$modelAlreadyOnline = $false
try {
    $probe = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health" -TimeoutSec 3 -ErrorAction Stop
    if ($probe.status -eq "ok" -or $probe -eq "ok") { $modelAlreadyOnline = $true }
} catch {}
if ($modelAlreadyOnline) {
    Write-Host "      llama-server already healthy on 8080; skipping relaunch." -ForegroundColor Green
} else {
    & powershell -File (Join-Path $ProjectRoot "infra\bonsai\start_model.ps1")
}

# 4. Wait for /health
Write-Host "[4/9] Confirming Bonsai /health probe..." -ForegroundColor Yellow
$serverOnline = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $res = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health" -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($res.status -eq "ok" -or $res -eq "ok") {
            $serverOnline = $true
            break
        }
    } catch {}
    Start-Sleep -Seconds 1
}
if (-not $serverOnline) {
    Write-Host "[ERR] Bonsai llama-server health check timed out!" -ForegroundColor Red
    exit 1
}
Write-Host "      Bonsai Server is online and responding." -ForegroundColor Green

# 5. Start Backend API
Write-Host "[5/9] Starting AI Market Analyst backend service..." -ForegroundColor Yellow
$backendPidFile = Join-Path $ProjectRoot "logs\backend.pid"
$backendLogFile = Join-Path $ProjectRoot "logs\backend.log"
$backendErrFile = Join-Path $ProjectRoot "logs\backend_err.log"

# Stop whatever actually holds the port. The pid file goes stale whenever the
# backend is restarted by anything other than this script; trusting it left the
# port occupied, so the new process died on bind while we still printed success.
$backendPort = 18765
$owner = Get-NetTCPConnection -State Listen -LocalPort $backendPort -ErrorAction SilentlyContinue
if ($owner) {
    $ownerPid = $owner[0].OwningProcess
    $ownerProc = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" -ErrorAction SilentlyContinue
    if ($ownerProc -and $ownerProc.CommandLine -match "uvicorn apps\.api\.main:app") {
        Write-Host "      Stopping existing backend PID $ownerPid" -ForegroundColor Yellow
        Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
    } else {
        Write-Host "[ERR] Port $backendPort is held by PID $ownerPid, which is not the AI Market Analyst backend." -ForegroundColor Red
        Write-Host "      Refusing to stop an unrelated process. Free the port and re-run." -ForegroundColor Yellow
        exit 1
    }
}

$backendProc = Start-Process -FilePath "python" -ArgumentList "-m uvicorn apps.api.main:app --host 127.0.0.1 --port $backendPort" -WorkingDirectory $ProjectRoot -RedirectStandardOutput $backendLogFile -RedirectStandardError $backendErrFile -PassThru -NoNewWindow

# Never claim success until the port is actually serving.
$backendOnline = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 1
    if ($backendProc.HasExited) { break }
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:$backendPort/health" -TimeoutSec 2 -ErrorAction Stop
        if ($h.status -eq "ok") { $backendOnline = $true; break }
    } catch {}
}
if (-not $backendOnline) {
    Write-Host "[ERR] Backend did not become healthy on port $backendPort." -ForegroundColor Red
    if (Test-Path $backendErrFile) {
        Write-Host "      Last lines of $backendErrFile :" -ForegroundColor Yellow
        Get-Content $backendErrFile -Tail 15 | ForEach-Object { Write-Host "        $_" -ForegroundColor Yellow }
    }
    exit 1
}
Set-Content -Path $backendPidFile -Value $backendProc.Id -Force
Write-Host "      Backend started with PID $($backendProc.Id) on http://127.0.0.1:$backendPort" -ForegroundColor Green

# 6. Output Summary Details
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " [6/9] System API Address:  http://127.0.0.1:18765" -ForegroundColor Green
Write-Host " [6/9] Model API Address:   http://127.0.0.1:8080/v1" -ForegroundColor Green
Write-Host " [7/9] Model Status:        Bonsai-2-27B-PTQ1_0 (ONLINE)" -ForegroundColor Green
Write-Host " [8/9] GPU VRAM Telemetry:  Used: $vramUsed | Free: $vramFree" -ForegroundColor Green
Write-Host " [9/9] Context Window:      8,192 Tokens (Safe 4060 Baseline)" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " AI Market Analyst V2 is ready for operation!" -ForegroundColor Green
