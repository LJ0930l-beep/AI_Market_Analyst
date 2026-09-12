$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$smokeRoot = Join-Path $root ('build\ai-trader-smoke-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $smokeRoot -Force | Out-Null
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = $listener.LocalEndpoint.Port
$listener.Stop()
$previousRoot = $env:AIMA_DATA_ROOT
$previousDatabase = $env:DATABASE_PATH
$previousHydration = $env:AIMA_PUBLIC_HYDRATION
$ownedProcess = $null
try {
    $env:AIMA_DATA_ROOT = Join-Path $smokeRoot 'data-root'
    $env:DATABASE_PATH = Join-Path $smokeRoot 'isolated.sqlite3'
    $env:AIMA_PUBLIC_HYDRATION = '0'
    $binary = Join-Path $root 'src-tauri\binaries\ai-market-analyst-backend-x86_64-pc-windows-msvc.exe'
    $ownedProcess = Start-Process -FilePath $binary -ArgumentList @('--port', "$port", '--ownership-token', 'isolated-smoke') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $smokeRoot 'stdout.log') -RedirectStandardError (Join-Path $smokeRoot 'stderr.log')
    $health = $null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 500
        try { $health = Invoke-RestMethod "http://127.0.0.1:$port/health" -TimeoutSec 1; break } catch { }
    }
    if (-not $health) { throw 'New packaged sidecar failed to start; inspect retained smoke logs.' }
    $status = Invoke-RestMethod "http://127.0.0.1:$port/v2/ai-session/status" -Headers @{'x-aima-ownership-token'='isolated-smoke'} -TimeoutSec 10
    if ($status.ai_session.decision_contract -ne 'ai_news_technical_v1') { throw 'Packaged sidecar does not contain the new decision contract.' }
    if ($status.ai_session.enabled) { throw 'Packaged sidecar unexpectedly auto-started trading.' }
    $report = [pscustomobject]@{ status='PASS'; api_version=$health.api_version; decision_contract=$status.ai_session.decision_contract; trading_enabled=$status.ai_session.enabled; policy=$status.ai_session.decision_policy; logs=$smokeRoot }
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $root 'evidence\ai_trader_sidecar_smoke.json') -Encoding UTF8
    $report | ConvertTo-Json -Depth 5
} finally {
    if ($ownedProcess -and -not $ownedProcess.HasExited) { & taskkill.exe /PID $ownedProcess.Id /T /F | Out-Null }
    $env:AIMA_DATA_ROOT = $previousRoot
    $env:DATABASE_PATH = $previousDatabase
    $env:AIMA_PUBLIC_HYDRATION = $previousHydration
}
