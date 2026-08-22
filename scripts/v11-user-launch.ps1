[CmdletBinding()]
param(
    [ValidateSet("start", "stop", "status", "prepare-models")]
    [string]$Action = "start",
    [switch]$NoBrowser,
    [switch]$Build
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$SafeLauncher = Join-Path $PSScriptRoot "phase7-local.ps1"
$LogRoot = Join-Path ([IO.Path]::GetTempPath()) "ai-market-analyst-phase7-launcher"
$RequiredModels = @("qwen3.5:4b", "qwen3.5:9b")

function Invoke-SafeLauncher([string]$LauncherAction, [switch]$ForceBuild) {
    $arguments = @{ Action = $LauncherAction }
    if ($ForceBuild) { $arguments["Build"] = $true }
    $output = & $SafeLauncher @arguments
    $launcherSucceeded = $?
    if (-not $launcherSucceeded) { throw "The safe project launcher failed." }
    return $output | Select-Object -Last 1
}

function Read-LauncherStatus {
    $raw = Invoke-SafeLauncher "status"
    try { return $raw | ConvertFrom-Json } catch { throw "Cannot read launcher status. Logs: $LogRoot" }
}

function Get-OllamaExecutable {
    $command = Get-Command ollama -ErrorAction SilentlyContinue
    if ($null -ne $command) { return [IO.Path]::GetFullPath($command.Source) }
    $local = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path -LiteralPath $local -PathType Leaf) { return [IO.Path]::GetFullPath($local) }
    throw "Ollama was not found. Install official Ollama, then run model preparation."
}

function Get-InstalledModels([string]$Ollama) {
    $names = @()
    $lines = & $Ollama list 2>$null
    if ($LASTEXITCODE -ne 0) { return $names }
    foreach ($line in $lines | Select-Object -Skip 1) {
        $name = ([string]$line -split '\s+')[0]
        if (-not [string]::IsNullOrWhiteSpace($name)) { $names += $name }
    }
    return $names
}

function Invoke-ModelPreparation {
    $ollama = Get-OllamaExecutable
    $installed = @(Get-InstalledModels $ollama)
    foreach ($model in $RequiredModels) {
        if ($installed -contains $model) {
            Write-Host "Installed: $model" -ForegroundColor Green
            continue
        }
        Write-Host "Pulling $model from the official Ollama registry. This may take time on first use." -ForegroundColor Cyan
        & $ollama pull $model
        if ($LASTEXITCODE -ne 0) { throw "Model pull failed: $model. No substitute was selected." }
    }
    Write-Host "Qwen 4B / 9B model preparation complete." -ForegroundColor Green
}

function Invoke-Start {
    $status = Read-LauncherStatus
    if ($status.status -eq "running" -and $status.ownership_errors.Count -eq 0) {
        Write-Host "AI Market Analyst is already running; no duplicate processes were started." -ForegroundColor Green
    } elseif ($status.status -eq "ownership_mismatch") {
        throw "Launcher ownership mismatch. Refusing to affect Python, Node, Ollama, ComfyUI, or unrelated processes. Inspect: $($status.state_file)"
    } else {
        $result = Invoke-SafeLauncher "start" -ForceBuild:$Build
        Write-Host $result
    }
    try {
        $ollama = Get-OllamaExecutable
        $installed = @(Get-InstalledModels $ollama)
        $missing = @($RequiredModels | Where-Object { $installed -notcontains $_ })
        if ($missing.Count -gt 0) {
            Write-Warning "Missing Qwen models: $($missing -join ', '). The UI remains available and marks unavailable tiers. Run Prepare_AI_Models.cmd once."
        }
    } catch {
        Write-Warning $_.Exception.Message
    }
    $api = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/health" -TimeoutSec 5
    $ui = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:4173/" -TimeoutSec 5
    if ($api.StatusCode -ne 200 -or $ui.StatusCode -ne 200) { throw "Services are not ready. Logs: $LogRoot" }
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:4173/" | Out-Null }
    Write-Host "Ready: http://127.0.0.1:4173/" -ForegroundColor Green
    Write-Host "Logs: $LogRoot"
}

switch ($Action) {
    "prepare-models" { Invoke-ModelPreparation }
    "start" { Invoke-Start }
    "stop" { Write-Host (Invoke-SafeLauncher "stop") }
    "status" { Write-Host (Invoke-SafeLauncher "status") }
}
