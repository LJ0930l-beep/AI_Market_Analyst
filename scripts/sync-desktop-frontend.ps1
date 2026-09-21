#Requires -Version 5.1
$ErrorActionPreference = "Stop"

$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\AI Market Analyst"
$AppExeName = "ai-market-analyst.exe"
$BuildTarget = "x86_64-pc-windows-msvc"
$ReleaseRoot = Join-Path $ProjectRoot "src-tauri\target\$BuildTarget\release"
$BuiltExe = Join-Path $ReleaseRoot $AppExeName
$TargetInstalledExe = Join-Path $InstallDir $AppExeName

Write-Host "== 1. Stop running desktop app and its exact-owned sidecar" -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "desktop-client.ps1") -Action stop -InstallDir $InstallDir
if ($LASTEXITCODE -ne 0) {
    throw "desktop-client stop failed with exit code $LASTEXITCODE"
}

Write-Host "== 2. Initialize MSVC and cargo tauri build" -ForegroundColor Cyan
. (Join-Path $PSScriptRoot "msvc-env.ps1")
Initialize-MsvcEnvironment

Push-Location (Join-Path $ProjectRoot "src-tauri")
$overrideConfig = Join-Path $env:TEMP "aima-tauri-build-override.json"
'{"build":{"beforeBuildCommand":""}}' | Set-Content -LiteralPath $overrideConfig -Encoding utf8

$startedAt = Get-Date
try {
    cargo tauri build --target $BuildTarget --config $overrideConfig --no-bundle
    if ($LASTEXITCODE -ne 0) {
        throw "cargo tauri build failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Write-Host "== 3. Overwrite installed binary" -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $BuiltExe)) {
    throw "Build artifact not found: $BuiltExe"
}

$fresh = (Get-Item -LiteralPath $BuiltExe).LastWriteTime
Write-Host "Built exe last write time: $fresh"

Copy-Item -LiteralPath $BuiltExe -Destination $TargetInstalledExe -Force
Write-Host "Overwrote $TargetInstalledExe successfully" -ForegroundColor Green

Write-Host "== 4. Launch updated desktop client" -ForegroundColor Cyan
Start-Process -FilePath $TargetInstalledExe -WorkingDirectory $InstallDir
Start-Sleep -Seconds 5

$newProcess = Get-Process -Name "ai-market-analyst" -ErrorAction SilentlyContinue
if ($newProcess) {
    Write-Host "Desktop client launched successfully! PID: $($newProcess.Id -join ', ')" -ForegroundColor Green
} else {
    Write-Warning "Client launched, please check desktop window."
}
