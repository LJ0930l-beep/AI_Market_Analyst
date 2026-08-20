[CmdletBinding()]
param(
    [int]$ApiPort = 18030,
    [int]$WebPort = 14103
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Launcher = Join-Path $PSScriptRoot "phase7-local.ps1"
$StatePath = Join-Path ([IO.Path]::GetTempPath()) "ai-market-analyst-phase7-launcher\state.json"
$database = Join-Path ([IO.Path]::GetTempPath()) ("ai-market-analyst-phase7-ownership-" + [guid]::NewGuid().ToString("N") + ".sqlite3")
$shellCommand = Get-Command powershell -ErrorAction SilentlyContinue
if ($null -eq $shellCommand) { $shellCommand = Get-Command pwsh -ErrorAction Stop }
$shell = [IO.Path]::GetFullPath($shellCommand.Source)
$pythonCommand = Get-Command python -ErrorAction Stop
$python = [IO.Path]::GetFullPath($pythonCommand.Source)
$helper = $null
$originalState = $null
$tampered = $false
$startedBySmoke = $false
$LauncherOutputFiles = @()

function Invoke-Launcher([string]$Action) {
    $arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Launcher,
        "-Action", $Action, "-DatabasePath", $database,
        "-ApiPort", "$ApiPort", "-WebPort", "$WebPort"
    )
    $stamp = [guid]::NewGuid().ToString("N")
    $stdoutPath = Join-Path ([IO.Path]::GetTempPath()) ("ai-market-analyst-phase7-launcher-smoke-$stamp.out.log")
    $stderrPath = Join-Path ([IO.Path]::GetTempPath()) ("ai-market-analyst-phase7-launcher-smoke-$stamp.err.log")
    $script:LauncherOutputFiles += @($stdoutPath, $stderrPath)
    $process = $null
    try {
        $process = Start-Process -FilePath $shell -ArgumentList $arguments -WorkingDirectory $ProjectRoot -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -WindowStyle Hidden -PassThru
        $deadline = [DateTime]::UtcNow.AddSeconds(60)
        do {
            if ($process.HasExited) { break }
            Start-Sleep -Milliseconds 100
        } while ([DateTime]::UtcNow -lt $deadline)
        if (-not $process.HasExited) {
            try { $process.Kill() } catch { }
            throw "launcher command exceeded the 60 second smoke timeout"
        }
        [void]$process.WaitForExit()
        $process.Refresh()
    } finally {
        if ($null -ne $process -and -not $process.HasExited) {
            try { $process.Kill() } catch { }
        }
    }
    $output = @()
    if (Test-Path -LiteralPath $stdoutPath) {
        $stdout = Get-Content -LiteralPath $stdoutPath -Raw
        if (-not [string]::IsNullOrWhiteSpace($stdout)) { $output += $stdout.TrimEnd() }
    }
    if (Test-Path -LiteralPath $stderrPath) {
        $stderr = Get-Content -LiteralPath $stderrPath -Raw
        if (-not [string]::IsNullOrWhiteSpace($stderr)) { $output += $stderr.TrimEnd() }
    }
    $exitCode = $process.ExitCode
    if ($null -eq $exitCode -and ($output -join " ") -match '"status"\s*:') {
        # Windows PowerShell can expose a null ExitCode for a redirected,
        # already-exited child after descendant handles close. The launcher
        # response is the bounded command result in that case.
        $exitCode = 0
    }
    try { $process.Dispose() } catch { }
    if (Test-Path -LiteralPath $stdoutPath) { try { Remove-Item -LiteralPath $stdoutPath -Force } catch { } }
    if (Test-Path -LiteralPath $stderrPath) { try { Remove-Item -LiteralPath $stderrPath -Force } catch { } }
    return @{ exit_code = $exitCode; output = $output }
}

try {
    if (Test-Path -LiteralPath $StatePath) {
        throw "launcher state already exists; refusing to overwrite it"
    }
    $started = Invoke-Launcher "start"
    if ($started.exit_code -ne 0) { throw "normal launcher start failed: $($started.output -join ' ')" }
    $startedBySmoke = $true
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { throw "launcher did not create state" }
    $originalState = Get-Content -LiteralPath $StatePath -Raw
    $state = $originalState | ConvertFrom-Json
    if ($state.format_version -ne "phase7_launcher_v2") { throw "launcher did not write v2 ownership state" }
    foreach ($record in @($state.api, $state.web)) {
        if ([int64]$record.start_time_utc_ticks -le 0 -or ([string]$record.command_line_sha256).Length -ne 64) {
            throw "launcher state is missing durable ownership fingerprints"
        }
    }

    $helper = Start-Process -FilePath $python -ArgumentList @("-c", '"import time; time.sleep(30)"') -WindowStyle Hidden -PassThru
    Start-Sleep -Milliseconds 500
    if ($helper.HasExited) { throw "same-executable helper exited before the ownership check" }
    $state.api.pid = $helper.Id
    $state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $StatePath -Encoding utf8
    $tampered = $true

    $status = Invoke-Launcher "status"
    if ($status.exit_code -ne 0) { throw "status did not report tampered ownership safely: $($status.output -join ' ')" }
    $statusJson = ($status.output -join "`n") | ConvertFrom-Json
    if ($statusJson.status -ne "ownership_mismatch") { throw "status did not report ownership_mismatch" }

    $stopped = Invoke-Launcher "stop"
    if ($stopped.exit_code -eq 0) { throw "tampered ownership record was accepted for stop" }
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { throw "state was removed after ownership mismatch" }
    if ($helper.HasExited) { throw "same-executable unrelated helper was stopped" }

    $originalState | Set-Content -LiteralPath $StatePath -Encoding utf8
    $tampered = $false
    $normalStop = Invoke-Launcher "stop"
    if ($normalStop.exit_code -ne 0) { throw "normal launcher stop failed after ownership repair: $($normalStop.output -join ' ')" }
    if (Test-Path -LiteralPath $StatePath) { throw "normal launcher stop retained state" }
    @{ status = "PASS"; stale_record = "refused"; same_executable_helper_alive = $true; normal_lifecycle = "passed" } | ConvertTo-Json -Compress
} finally {
    if (-not $startedBySmoke -and (Test-Path -LiteralPath $StatePath -PathType Leaf) -and $null -eq $originalState) {
        $startedBySmoke = $true
    }
    if ($null -ne $helper) {
        try {
            if (-not $helper.HasExited) { $helper.Kill(); [void]$helper.WaitForExit(5000) }
        } catch { }
    }
    if ($startedBySmoke) {
        if ($tampered -and $null -ne $originalState -and (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
            try { $originalState | Set-Content -LiteralPath $StatePath -Encoding utf8 } catch { }
        }
        if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
            try { [void](Invoke-Launcher "stop") } catch { }
        }
    }
    if (Test-Path -LiteralPath $database -PathType Leaf) {
        try { Remove-Item -LiteralPath $database -Force } catch { }
    }
    foreach ($outputPath in $LauncherOutputFiles) {
        for ($attempt = 0; $attempt -lt 10 -and (Test-Path -LiteralPath $outputPath); $attempt++) {
            try { Remove-Item -LiteralPath $outputPath -Force } catch { Start-Sleep -Milliseconds 250 }
        }
    }
}
