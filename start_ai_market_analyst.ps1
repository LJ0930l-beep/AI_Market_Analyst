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
& powershell -File (Join-Path $ProjectRoot "infra\bonsai\start_model.ps1")

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

# Kill existing if running
if (Test-Path $backendPidFile) {
    $oldPid = Get-Content $backendPidFile
    try { Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue } catch {}
}

$backendProc = Start-Process -FilePath "python" -ArgumentList "-m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000" -WorkingDirectory $ProjectRoot -RedirectStandardOutput $backendLogFile -RedirectStandardError $backendLogFile -PassThru -NoNewWindow
Set-Content -Path $backendPidFile -Value $backendProc.Id -Force
Write-Host "      Backend started with PID $($backendProc.Id) on http://127.0.0.1:8000" -ForegroundColor Green

# 6. Output Summary Details
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " [6/9] System API Address:  http://127.0.0.1:8000" -ForegroundColor Green
Write-Host " [6/9] Model API Address:   http://127.0.0.1:8080/v1" -ForegroundColor Green
Write-Host " [7/9] Model Status:        Bonsai-2-27B-PTQ1_0 (ONLINE)" -ForegroundColor Green
Write-Host " [8/9] GPU VRAM Telemetry:  Used: $vramUsed | Free: $vramFree" -ForegroundColor Green
Write-Host " [9/9] Context Window:      8,192 Tokens (Safe 4060 Baseline)" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " AI Market Analyst V2 is ready for operation!" -ForegroundColor Green
