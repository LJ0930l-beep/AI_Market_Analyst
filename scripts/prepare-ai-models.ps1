<#
    本地 Qwen 模型准备 (V2.0)

    从原 scripts\v11-user-launch.ps1 中抽出。原脚本的 start / stop / status 分支
    会去探测 8000/4173 端口并打开浏览器网页入口 —— V2.0 的界面是 Tauri 窗口、
    前端资源编译期已嵌入 exe，那条链路必然得到 ERR_CONNECTION_REFUSED，因此整段
    已移除。桌面客户端的启停请看 scripts\desktop-client.ps1。

    本脚本只负责按需拉取本地模型，不启动、不停止任何进程。
#>
[CmdletBinding()]
param(
    [string[]]$RequiredModels = @("qwen3.5:4b", "qwen3.5:9b")
)

$ErrorActionPreference = "Stop"

function Get-OllamaExecutable {
    $command = Get-Command ollama -ErrorAction SilentlyContinue
    if ($null -ne $command) { return [IO.Path]::GetFullPath($command.Source) }
    $local = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path -LiteralPath $local -PathType Leaf) { return [IO.Path]::GetFullPath($local) }
    throw "Ollama was not found. Install official Ollama, then run model preparation again."
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

$ollama = Get-OllamaExecutable
$installed = @(Get-InstalledModels $ollama)
$missing = 0
foreach ($model in $RequiredModels) {
    if ($installed -contains $model) {
        Write-Host "Installed: $model" -ForegroundColor Green
        continue
    }
    Write-Host "Pulling $model from the official Ollama registry. This may take time on first use." -ForegroundColor Cyan
    & $ollama pull $model
    if ($LASTEXITCODE -ne 0) {
        $missing++
        Write-Warning "Model pull failed: $model. No substitute was selected."
        continue
    }
}
if ($missing -gt 0) {
    Write-Warning "$missing model(s) could not be prepared. The UI stays available and marks unavailable tiers."
    exit 1
}
Write-Host "Qwen 4B / 9B model preparation complete." -ForegroundColor Green
