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
        if ($state.format_version -ne "phase7_launcher_v1") { throw "unsupported launcher state" }
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

function Get-OwnedProcess($Record) {
    if ($null -eq $Record -or $null -eq $Record.pid) { return $null }
    $process = Get-Process -Id ([int]$Record.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    $actualPath = $null
    try { $actualPath = $process.Path } catch { throw "cannot verify executable path for recorded PID $($Record.pid); refusing to stop it" }
    if ([string]::IsNullOrWhiteSpace($actualPath) -or [IO.Path]::GetFullPath($actualPath) -ne [IO.Path]::GetFullPath([string]$Record.executable)) {
        throw "recorded PID $($Record.pid) no longer matches its owned executable; refusing to stop it"
    }
    return $process
}

function Stop-OwnedProcess($Record) {
    $process = Get-OwnedProcess $Record
    if ($null -eq $process) { return }
    if (-not $process.HasExited) {
        [void]$process.CloseMainWindow()
        if (-not $process.WaitForExit(10000)) {
            # This is reached only after PID and executable-path ownership was
            # verified above; unrelated processes are never selected by name.
            $process.Kill()
            [void]$process.WaitForExit(5000)
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
    $api = Get-OwnedProcess $state.api
    $web = Get-OwnedProcess $state.web
    @{
        status = if ($null -ne $api -and $null -ne $web) { "running" } else { "degraded" }
        api_pid = $state.api.pid
        web_pid = $state.web.pid
        api_running = ($null -ne $api)
        web_running = ($null -ne $web)
        database = $state.database
        scheduler_default_enabled = $false
        state_file = $StatePath
    } | ConvertTo-Json -Compress
}

function Invoke-Stop {
    $state = Read-State
    if ($null -eq $state) {
        @{ status = "stopped"; message = "no owned local services recorded" } | ConvertTo-Json -Compress
        return
    }
    Stop-OwnedProcess $state.web
    Stop-OwnedProcess $state.api
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
    try {
        $apiProcess = Start-Process -FilePath $python -ArgumentList $apiArguments -WorkingDirectory $ProjectRoot -RedirectStandardOutput $apiLog -RedirectStandardError $apiErrorLog -WindowStyle Hidden -PassThru
    } finally {
        $env:DATABASE_PATH = $previousDatabase
        $env:API_CORS_ORIGINS = $previousCors
    }
    try {
        Wait-Http "http://$ApiHost`:$ApiPort/health"
        $webArguments = @("web/e2e/preview-server.mjs", "--dist", $distIndex.Replace("\index.html", ""), "--api", "http://$ApiHost`:$ApiPort", "--host", $WebHost, "--port", "$WebPort")
        $webProcess = Start-Process -FilePath $node -ArgumentList $webArguments -WorkingDirectory $ProjectRoot -RedirectStandardOutput $webLog -RedirectStandardError $webErrorLog -WindowStyle Hidden -PassThru
        Wait-Http "http://$WebHost`:$WebPort/"
    } catch {
        if ($null -ne $webProcess) {
            Stop-OwnedProcess @{ pid = $webProcess.Id; executable = $node }
        }
        Stop-OwnedProcess @{ pid = $apiProcess.Id; executable = $python }
        throw
    }
    $state = @{
        format_version = "phase7_launcher_v1"
        started_at = [DateTime]::UtcNow.ToString("o")
        database = $database
        api = @{ pid = $apiProcess.Id; executable = $python; host = $ApiHost; port = $ApiPort; log = $apiLog }
        web = @{ pid = $webProcess.Id; executable = $node; host = $WebHost; port = $WebPort; log = @($webLog, $webErrorLog) }
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
