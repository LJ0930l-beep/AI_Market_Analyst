#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\AI Market Analyst"
$BackendExe = Join-Path $InstallDir "ai-market-analyst-backend.exe"

$buildRoot = Join-Path $ProjectRoot "build\sidecar"
$latestRun = Get-ChildItem -Directory -Path $buildRoot | Sort-Object CreationTime -Descending | Select-Object -First 1
if (-not $latestRun) {
    throw "Build directory not found in build\sidecar"
}
$builtBackend = Join-Path $latestRun.FullName "dist\ai-market-analyst-backend.exe"
if (-not (Test-Path -LiteralPath $builtBackend -PathType Leaf)) {
    $builtBackend = Join-Path $ProjectRoot "src-tauri\binaries\ai-market-analyst-backend-x86_64-pc-windows-msvc.exe"
}
if (-not (Test-Path -LiteralPath $builtBackend -PathType Leaf)) {
    throw "Built backend not found: $builtBackend"
}

Write-Host "Target: $BackendExe" -ForegroundColor Cyan
Write-Host "Source: $builtBackend" -ForegroundColor Cyan

# 1. Stop app & sidecar
Write-Host "Stopping client and sidecar..." -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "desktop-client.ps1") -Action stop

Start-Sleep -Seconds 1

# 2. Copy sidecar
Write-Host "Copying new sidecar..." -ForegroundColor Cyan
Copy-Item -LiteralPath $builtBackend -Destination $BackendExe -Force
$newHash = (Get-FileHash -LiteralPath $BackendExe -Algorithm SHA256).Hash
Write-Host "Installed SHA256: $newHash" -ForegroundColor Green

# 3. Restart client
if (-not $NoRestart) {
    Write-Host "Starting client..." -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "desktop-client.ps1") -Action start
    Start-Sleep -Seconds 2
    & (Join-Path $PSScriptRoot "desktop-client.ps1") -Action status
}
