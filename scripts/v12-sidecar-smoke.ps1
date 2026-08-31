$ErrorActionPreference = "Stop"
$port = 18765
if ($args.Count -gt 0) {
    $parsedPort = 0
    if (-not [int]::TryParse($args[0], [ref]$parsedPort) -or $parsedPort -lt 1024 -or $parsedPort -gt 65535) {
        throw "usage: v12-sidecar-smoke.ps1 [port 1024-65535]"
    }
    $port = $parsedPort
}
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$baseUrl = "http://127.0.0.1:$port"
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    throw "refusing to touch an occupied loopback port: $port"
}
$smokeRoot = Join-Path $env:TEMP "aima-v12-sidecar-smoke-$PID-$port"
New-Item -ItemType Directory -Force -Path $smokeRoot | Out-Null
$stdoutPath = Join-Path $smokeRoot "stdout.log"
$stderrPath = Join-Path $smokeRoot "stderr.log"
$previousDataRoot = $env:AIMA_DATA_ROOT
$previousDatabase = $env:DATABASE_PATH
$previousFixtureFallback = $env:ALLOW_FIXTURE_FALLBACK
$env:AIMA_DATA_ROOT = Join-Path $smokeRoot "appdata"
Remove-Item Env:DATABASE_PATH -ErrorAction SilentlyContinue
$env:ALLOW_FIXTURE_FALLBACK = "0"
$process = $null
try {
    $binary = Join-Path $projectRoot "src-tauri\binaries\ai-market-analyst-backend-x86_64-pc-windows-msvc.exe"
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
        throw "packaged MSVC sidecar is missing: $binary"
    }
    $process = Start-Process -FilePath $binary -ArgumentList @("--host", "127.0.0.1", "--port", "$port") -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    $health = $null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        try {
            $health = Invoke-RestMethod "$baseUrl/health" -TimeoutSec 2
            break
        }
        catch { }
    }
    if ($null -eq $health) {
        $stderr = if (Test-Path $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
        throw "packaged sidecar did not become ready: $stderr"
    }
    $release = Invoke-RestMethod "$baseUrl/health/release" -TimeoutSec 5
    if ([int]$release.database.schema_version -ne 12) {
        throw "unexpected schema version: $($release.database.schema_version)"
    }
    [pscustomobject]@{
        pid = $process.Id
        health = $health
        schema_version = $release.database.schema_version
        app_data_created = Test-Path (Join-Path $env:AIMA_DATA_ROOT "data")
        fixture_fallback = $env:ALLOW_FIXTURE_FALLBACK
    } | ConvertTo-Json -Depth 8
}
finally {
    if ($null -ne $process -and -not $process.HasExited) {
        & taskkill.exe /PID $process.Id /T /F | Out-Null
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    }
    if ($null -eq $previousDataRoot) { Remove-Item Env:AIMA_DATA_ROOT -ErrorAction SilentlyContinue } else { $env:AIMA_DATA_ROOT = $previousDataRoot }
    if ($null -eq $previousDatabase) { Remove-Item Env:DATABASE_PATH -ErrorAction SilentlyContinue } else { $env:DATABASE_PATH = $previousDatabase }
    if ($null -eq $previousFixtureFallback) { Remove-Item Env:ALLOW_FIXTURE_FALLBACK -ErrorAction SilentlyContinue } else { $env:ALLOW_FIXTURE_FALLBACK = $previousFixtureFallback }
    if (Test-Path -LiteralPath $smokeRoot) { Remove-Item -LiteralPath $smokeRoot -Recurse -Force -ErrorAction SilentlyContinue }
}
