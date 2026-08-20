[CmdletBinding()]
param(
    [ValidateSet("start", "stop", "status", "restart")]
    [string]$Action = "status",
    [string]$DatabasePath = "data\market_analyst.sqlite3",
    [string]$ApiHost = "127.0.0.1",
    [ValidateRange(1024, 65535)]
    [int]$ApiPort = 8000,
    [string]$WebHost = "127.0.0.1",
    [ValidateRange(1024, 65535)]
    [int]$WebPort = 4173,
    [switch]$Build
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$StateRoot = Join-Path ([IO.Path]::GetTempPath()) "ai-market-analyst-phase7-launcher"
$StatePath = Join-Path $StateRoot "state.json"

function Resolve-ProjectPath([string]$Candidate) {
    if ([string]::IsNullOrWhiteSpace($Candidate) -or $Candidate.Length -gt 512 -or $Candidate.IndexOfAny([char[]]([char]0, [char]10, [char]13)) -ge 0) {
        throw "database path is empty, too long or contains control characters"
    }
    $full = if ([IO.Path]::IsPathRooted($Candidate)) { [IO.Path]::GetFullPath($Candidate) } else { [IO.Path]::GetFullPath((Join-Path $ProjectRoot $Candidate)) }
    if ([IO.Path]::GetPathRoot($full) -eq $full -or [IO.Path]::GetFileName($full) -in @("", ".", "..")) {
        throw "database path must name a file"
    }
    $probe = $full
    while ($true) {
        if (Test-Path -LiteralPath $probe) {
            $item = Get-Item -LiteralPath $probe -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "symlink/reparse-point database paths are not allowed"
            }
        }
        $parent = [IO.Directory]::GetParent($probe)
        if ($null -eq $parent -or $parent.FullName -eq $probe) { break }
        $probe = $parent.FullName
    }
    if ([IO.Path]::GetExtension($full).ToLowerInvariant() -notin @(".sqlite3", ".sqlite", ".db")) {
        throw "database path must use .sqlite3, .sqlite or .db"
    }
    $parentPath = Split-Path -Parent $full
    if (-not (Test-Path -LiteralPath $parentPath -PathType Container)) {
        New-Item -ItemType Directory -Path $parentPath -Force | Out-Null
    }
    return $full
}

function Get-Executable([string]$Name) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $command) { throw "required executable not found: $Name" }
    return [IO.Path]::GetFullPath($command.Source)
}

function Test-PortAvailable([string]$HostName, [int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($null -ne $listener) { return $false }
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        if ($task.Wait(250) -and $client.Connected) { return $false }
        return $true
    } catch {
        return $true
    } finally {
        $client.Dispose()
    }
}

function Read-State {
    Assert-StateStorage
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { return $null }
    try {
        $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        if ($state.format_version -ne "phase7_launcher_v2") { throw "unsupported launcher state" }
        return $state
    } catch {
        throw "launcher state is invalid; inspect $StatePath before removing only that file"
    }
}

function Assert-StateStorage {
    foreach ($candidate in @($StateRoot, $StatePath)) {
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -LiteralPath $candidate -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "launcher state path may not be a symlink/reparse point: $candidate"
            }
        }
    }
}

function Get-TextHash([string]$Text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-ProcessCommandLine([int]$ProcessId) {
    try {
        $entry = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        if ($null -eq $entry -or [string]::IsNullOrWhiteSpace([string]$entry.CommandLine)) { return $null }
        return [string]$entry.CommandLine
    } catch {
        return $null
    }
}

function Assert-ExpectedCommandLine($Record, [string]$CommandLine) {
    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        throw "cannot verify command line for recorded PID $($Record.pid); refusing to stop it"
    }
    $actual = $CommandLine.ToLowerInvariant()
    $role = [string]$Record.role
    $required = if ($role -eq "api") {
        @("uvicorn apps.api.main:app", "--host $($Record.host)", "--port $($Record.port)")
    } elseif ($role -eq "web") {
        @("web/e2e/preview-server.mjs", "--host $($Record.host)", "--port $($Record.port)")
    } else {
        throw "recorded launcher role is invalid; refusing to stop it"
    }
    foreach ($marker in $required) {
        if (-not $actual.Contains($marker.ToLowerInvariant())) {
            throw "recorded PID $($Record.pid) command line no longer matches its $role child role/port; refusing to stop it"
        }
    }
    return $actual
}

function New-OwnedRecord($Process, [string]$Role, [string]$HostName, [int]$Port) {
    if ($null -eq $Process) { throw "cannot record a null launcher child" }
    $captured = $false
    $actualPath = $null
    $startTicks = 0L
    $commandLine = $null
    for ($attempt = 0; $attempt -lt 20 -and -not $captured; $attempt++) {
        try {
            $Process.Refresh()
            if ($Process.HasExited) { throw "launcher child exited while capturing ownership" }
            $actualPath = [IO.Path]::GetFullPath([string]$Process.Path)
            $startTicks = [int64]$Process.StartTime.ToUniversalTime().Ticks
            $commandLine = Get-ProcessCommandLine ([int]$Process.Id)
            if ([string]::IsNullOrWhiteSpace($commandLine)) { throw "launcher command line is not available yet" }
            $captured = $true
        } catch {
            if ($attempt -lt 19) { Start-Sleep -Milliseconds 250 }
        }
    }
    if (-not $captured) {
        throw "cannot capture executable/start time/command line for launcher $Role PID $($Process.Id); refusing unverified ownership"
    }
    $record = @{
        role = $Role
        pid = [int]$Process.Id
        executable = $actualPath
        start_time_utc_ticks = $startTicks
        command_line_sha256 = Get-TextHash $commandLine
        host = $HostName
        port = [int]$Port
    }
    Assert-ExpectedCommandLine $record $commandLine | Out-Null
    return $record
}

function Get-OwnedProcess($Record) {
    if ($null -eq $Record -or $null -eq $Record.pid) { throw "launcher ownership fingerprint is incomplete; state retained" }
    $startTicks = 0L
    try { $startTicks = [int64]$Record.start_time_utc_ticks } catch { throw "launcher ownership start-time fingerprint is invalid; state retained" }
    if ($startTicks -le 0 -or [string]::IsNullOrWhiteSpace([string]$Record.command_line_sha256)) {
        throw "launcher ownership fingerprint is incomplete; state retained"
    }
    $process = Get-Process -Id ([int]$Record.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    $actualPath = $null
    try { $actualPath = [IO.Path]::GetFullPath([string]$process.Path) } catch { throw "cannot verify executable path for recorded PID $($Record.pid); refusing to stop it" }
    $expectedPath = [IO.Path]::GetFullPath([string]$Record.executable)
    if ([string]::IsNullOrWhiteSpace($actualPath) -or -not [StringComparer]::OrdinalIgnoreCase.Equals($actualPath, $expectedPath)) {
        throw "recorded PID $($Record.pid) no longer matches its owned executable; refusing to stop it"
    }
    try { $actualStartTicks = [int64]$process.StartTime.ToUniversalTime().Ticks } catch { throw "cannot verify start time for recorded PID $($Record.pid); refusing to stop it" }
    if ($actualStartTicks -ne $startTicks) {
        throw "recorded PID $($Record.pid) no longer matches its owned process start time; refusing to stop it"
    }
    $commandLine = Get-ProcessCommandLine ([int]$Record.pid)
    if ([string]::IsNullOrWhiteSpace($commandLine) -or (Get-TextHash $commandLine) -ne ([string]$Record.command_line_sha256).ToLowerInvariant()) {
        throw "recorded PID $($Record.pid) no longer matches its owned command line; refusing to stop it"
    }
    Assert-ExpectedCommandLine $Record $commandLine | Out-Null
    return $process
}

function Close-UnfingerprintedProcess($Process) {
    if ($null -eq $Process) { return }
    try {
        if (-not $Process.HasExited) {
            [void]$Process.CloseMainWindow()
            [void]$Process.WaitForExit(2000)
        }
    } catch { }
}

function Stop-OwnedProcess($Record, $KnownProcess = $null) {
    $process = if ($null -ne $KnownProcess) { $KnownProcess } else { Get-OwnedProcess $Record }
    if ($null -eq $process) { return }
    if (-not $process.HasExited) {
        [void]$process.CloseMainWindow()
        if (-not $process.WaitForExit(10000)) {
            # Re-verify start-time, executable and command-line ownership after
            # the graceful wait. PID reuse must never turn this into a kill of a
            # different process.
            $stillOwned = Get-OwnedProcess $Record
            if ($null -ne $stillOwned -and -not $stillOwned.HasExited) {
                $stillOwned.Kill()
                [void]$stillOwned.WaitForExit(5000)
            }
        }
    }
}

function Wait-Http([string]$Uri, [int]$TimeoutSeconds = 30) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { return }
        } catch { }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "local service did not become ready: $Uri"
}

function Invoke-Status {
    $state = Read-State
    if ($null -eq $state) {
        @{ status = "stopped"; state_file = $StatePath } | ConvertTo-Json -Compress
        return
    }
    $errors = @()
    $api = $null
    $web = $null
    try { $api = Get-OwnedProcess $state.api } catch { $errors += $_.Exception.Message }
    try { $web = Get-OwnedProcess $state.web } catch { $errors += $_.Exception.Message }
    @{
        status = if ($errors.Count -gt 0) { "ownership_mismatch" } elseif ($null -ne $api -and $null -ne $web) { "running" } else { "degraded" }
        api_pid = $state.api.pid
        web_pid = $state.web.pid
        api_running = ($null -ne $api)
        web_running = ($null -ne $web)
        database = $state.database
        scheduler_default_enabled = $false
        state_file = $StatePath
        ownership_errors = $errors
    } | ConvertTo-Json -Compress
}

function Invoke-Stop {
    $state = Read-State
    if ($null -eq $state) {
        @{ status = "stopped"; message = "no owned local services recorded" } | ConvertTo-Json -Compress
        return
    }
    $errors = @()
    $web = $null
    $api = $null
    try { $web = Get-OwnedProcess $state.web } catch { $errors += $_.Exception.Message }
    try { $api = Get-OwnedProcess $state.api } catch { $errors += $_.Exception.Message }
    if ($errors.Count -gt 0) {
        throw "refusing to stop due to launcher ownership mismatch; state retained at $StatePath; $($errors -join '; ')"
    }
    Stop-OwnedProcess $state.web $web
    Stop-OwnedProcess $state.api $api
    Remove-Item -LiteralPath $StatePath -Force
    @{ status = "stopped"; logs = @($state.api.log, $state.web.log) } | ConvertTo-Json -Compress
}

function Invoke-Start {
    Assert-StateStorage
    $existing = Read-State
    if ($null -ne $existing) {
        $apiExisting = Get-OwnedProcess $existing.api
        $webExisting = Get-OwnedProcess $existing.web
        if ($null -ne $apiExisting -or $null -ne $webExisting) { throw "owned local services are already running; use -Action status or stop" }
        Remove-Item -LiteralPath $StatePath -Force
    }
    if ($ApiHost -notin @("127.0.0.1", "localhost", "::1") -or $WebHost -notin @("127.0.0.1", "localhost", "::1")) {
        throw "local launcher binds only to loopback hosts"
    }
    $database = Resolve-ProjectPath $DatabasePath
    $python = Get-Executable "python"
    $node = Get-Executable "node"
    $npm = Get-Executable "npm"
    & $python -c "import uvicorn" 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Python uvicorn is unavailable; install the API extra first" }
    if (-not (Test-PortAvailable $ApiHost $ApiPort)) { throw "API port is occupied; no process was stopped" }
    if (-not (Test-PortAvailable $WebHost $WebPort)) { throw "web port is occupied; no process was stopped" }
    $distIndex = Join-Path $ProjectRoot "web\dist\index.html"
    if ($Build -or -not (Test-Path -LiteralPath $distIndex -PathType Leaf)) {
        Push-Location (Join-Path $ProjectRoot "web")
        try {
            $env:VITE_API_BASE_URL = "/api"
            & $npm run build
            if ($LASTEXITCODE -ne 0) { throw "web build failed" }
        } finally { Pop-Location }
    }
    New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
    Assert-StateStorage
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $apiLog = Join-Path $StateRoot "api-$stamp.log"
    $apiErrorLog = Join-Path $StateRoot "api-$stamp.err.log"
    $webLog = Join-Path $StateRoot "web-$stamp.log"
    $webErrorLog = Join-Path $StateRoot "web-$stamp.err.log"
    $apiArguments = @("-m", "uvicorn", "apps.api.main:app", "--host", $ApiHost, "--port", "$ApiPort", "--log-level", "warning")
    $previousDatabase = $env:DATABASE_PATH
    $previousCors = $env:API_CORS_ORIGINS
    $env:DATABASE_PATH = $database
    $env:API_CORS_ORIGINS = "http://$WebHost`:$WebPort"
    $apiProcess = $null
    $webProcess = $null
    $apiRecord = $null
    $webRecord = $null
    try {
        $apiProcess = Start-Process -FilePath $python -ArgumentList $apiArguments -WorkingDirectory $ProjectRoot -RedirectStandardOutput $apiLog -RedirectStandardError $apiErrorLog -WindowStyle Hidden -PassThru
        $apiRecord = New-OwnedRecord $apiProcess "api" $ApiHost $ApiPort
        $env:DATABASE_PATH = $previousDatabase
        $env:API_CORS_ORIGINS = $previousCors
        Wait-Http "http://$ApiHost`:$ApiPort/health"
        $webArguments = @("web/e2e/preview-server.mjs", "--dist", $distIndex.Replace("\index.html", ""), "--api", "http://$ApiHost`:$ApiPort", "--host", $WebHost, "--port", "$WebPort")
        $webProcess = Start-Process -FilePath $node -ArgumentList $webArguments -WorkingDirectory $ProjectRoot -RedirectStandardOutput $webLog -RedirectStandardError $webErrorLog -WindowStyle Hidden -PassThru
        $webRecord = New-OwnedRecord $webProcess "web" $WebHost $WebPort
        Wait-Http "http://$WebHost`:$WebPort/"
    } catch {
        $env:DATABASE_PATH = $previousDatabase
        $env:API_CORS_ORIGINS = $previousCors
        if ($null -ne $webRecord) {
            Stop-OwnedProcess $webRecord
        } elseif ($null -ne $webProcess) {
            Close-UnfingerprintedProcess $webProcess
        }
        if ($null -ne $apiRecord) {
            Stop-OwnedProcess $apiRecord
        } elseif ($null -ne $apiProcess) {
            Close-UnfingerprintedProcess $apiProcess
        }
        throw
    } finally {
        $env:DATABASE_PATH = $previousDatabase
        $env:API_CORS_ORIGINS = $previousCors
    }
    $apiRecord["log"] = $apiLog
    $webRecord["log"] = @($webLog, $webErrorLog)
    $state = @{
        format_version = "phase7_launcher_v2"
        started_at = [DateTime]::UtcNow.ToString("o")
        database = $database
        api = $apiRecord
        web = $webRecord
    }
    try {
        $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $StatePath -Encoding utf8
    } catch {
        Stop-OwnedProcess $state.web
        Stop-OwnedProcess $state.api
        throw
    }
    @{ status = "running"; api = "http://$ApiHost`:$ApiPort"; web = "http://$WebHost`:$WebPort"; database = $database; scheduler_default_enabled = $false } | ConvertTo-Json -Compress
}

switch ($Action) {
    "status" { Invoke-Status }
    "stop" { Invoke-Stop }
    "start" { Invoke-Start }
    "restart" { Invoke-Stop; Invoke-Start }
}
