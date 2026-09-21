<#
    AI Market Analyst 桌面客户端启动器 (V2.0)

    V2.0 的界面是 Tauri 窗口，前端资源在编译期嵌入 exe，后端是 exe 自己派生的
    sidecar（随机挑选 18765..18828 中的一个空闲端口）。它不再有任何 HTTP 托管的
    网页入口——所以旧脚本里"探测 8000/4173 再打开浏览器"的做法只会得到
    ERR_CONNECTION_REFUSED。本脚本只负责桌面客户端本身。

    用法：
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\desktop-client.ps1 -Action start
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\desktop-client.ps1 -Action status
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\desktop-client.ps1 -Action stop

    安全约定：
        * 只操作安装目录里的 ai-market-analyst*.exe，按可执行文件完整路径核对，
          不会按进程名误伤 Python / Node / Ollama / ComfyUI 等无关进程。
        * 重复启动是安全的：客户端注册了单实例，第二次启动只会唤回已有窗口。
#>
[CmdletBinding()]
param(
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "start",
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\AI Market Analyst")
)

$ErrorActionPreference = "Stop"

$AppExeName = "ai-market-analyst.exe"
$BackendExeName = "ai-market-analyst-backend.exe"
$AppExe = Join-Path $InstallDir $AppExeName
$BackendExe = Join-Path $InstallDir $BackendExeName

function Get-DataRoot {
    if (-not [string]::IsNullOrWhiteSpace($env:AIMA_DATA_ROOT)) { return $env:AIMA_DATA_ROOT }
    return (Join-Path $env:LOCALAPPDATA "AI Market Analyst")
}

function Resolve-Full([string]$Path) {
    try { return [IO.Path]::GetFullPath($Path) } catch { return $Path }
}

function Get-OwnedProcesses([string]$ExePath) {
    $target = Resolve-Full $ExePath
    $name = [IO.Path]::GetFileNameWithoutExtension($ExePath)
    return @(
        Get-Process -Name $name -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -and ((Resolve-Full $_.Path) -ieq $target)
        }
    )
}

function Test-LoopbackPort([int]$Port) {
    if ($Port -le 0) { return $false }
    $client = New-Object Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        if (-not $task.Wait(900)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Read-RuntimeRecord {
    $path = Join-Path (Get-DataRoot) "runtime\runtime.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json) } catch { return $null }
}

function Write-Header {
    Write-Host ""
    Write-Host "AI Market Analyst - 桌面客户端" -ForegroundColor Cyan
    Write-Host "  安装目录 : $InstallDir"
    Write-Host "  数据目录 : $(Get-DataRoot)"
    Write-Host "  数据库   : $(Join-Path (Get-DataRoot) 'data\market_analyst.sqlite3')"
    Write-Host ""
}

function Invoke-Start {
    if (-not (Test-Path -LiteralPath $AppExe -PathType Leaf)) {
        throw "未找到客户端可执行文件：$AppExe`n请先安装客户端，或运行 scripts\sync-client.ps1 完成构建与安装。"
    }
    $existing = @(Get-OwnedProcesses $AppExe)
    if ($existing.Count -gt 0) {
        Write-Host "客户端已在运行 (PID $($existing[0].Id))，本次启动只会唤回已有窗口。" -ForegroundColor Yellow
    }
    # 第二次启动由客户端的单实例插件接管：它会显示并聚焦已有窗口，不会新建第二个实例，
    # 因此这里可以无条件启动。
    Start-Process -FilePath $AppExe -WorkingDirectory $InstallDir | Out-Null
    Write-Host "已启动客户端。窗口出现后即可使用；日志见数据目录下的 logs。" -ForegroundColor Green
}

function Invoke-Status {
    Write-Header
    $apps = @(Get-OwnedProcesses $AppExe)
    if ($apps.Count -eq 0) {
        Write-Host "客户端状态 : 未运行" -ForegroundColor Yellow
    } else {
        foreach ($item in $apps) {
            $title = if ($item.MainWindowTitle) { $item.MainWindowTitle } else { "(窗口标题不可用)" }
            Write-Host "客户端状态 : 运行中  PID $($item.Id)  窗口 '$title'" -ForegroundColor Green
        }
    }

    $runtime = Read-RuntimeRecord
    $runtimeLabel = if ($apps.Count -eq 0) { "上次运行记录" } else { "运行态记录" }
    if ($null -ne $runtime) {
        Write-Host "$runtimeLabel : 端口 $($runtime.port)"
        if ($runtime.sidecar) {
            $started = $runtime.sidecar.started_at_utc
            Write-Host "             sidecar PID $($runtime.sidecar.pid)  启动于 $started"
        }
        Write-Host "             记录时间 $($runtime.updated_at)"
    } else {
        Write-Host "$runtimeLabel : 无 (runtime.json 不存在)"
    }

    $backends = @(Get-OwnedProcesses $BackendExe)
    Write-Host "后端进程   : $($backends.Count) 个"
    foreach ($item in $backends) { Write-Host "             PID $($item.Id)" }

    $port = 0
    if ($null -ne $runtime -and $null -ne $runtime.port) { $port = [int]$runtime.port }
    if ($port -gt 0) {
        if (Test-LoopbackPort $port) {
            Write-Host "端口探测   : 127.0.0.1:$port 可连接" -ForegroundColor Green
        } else {
            Write-Host "端口探测   : 127.0.0.1:$port 不可连接（后端可能仍在初始化）" -ForegroundColor Yellow
        }
    }
    Write-Host ""
}

function Invoke-Stop {
    Write-Header
    $apps = @(Get-OwnedProcesses $AppExe)
    if ($apps.Count -eq 0) {
        Write-Host "客户端未在运行。" -ForegroundColor Yellow
    } else {
        foreach ($item in $apps) {
            # 先请求正常退出，让客户端自己回收它派生的 sidecar。
            Stop-Process -Id $item.Id -ErrorAction SilentlyContinue
        }
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline -and @(Get-OwnedProcesses $AppExe).Count -gt 0) {
            Start-Sleep -Milliseconds 250
        }
        foreach ($item in @(Get-OwnedProcesses $AppExe)) {
            Write-Host "客户端未在 10 秒内退出，强制结束 PID $($item.Id)。" -ForegroundColor Yellow
            Stop-Process -Id $item.Id -Force -ErrorAction SilentlyContinue
        }
    }

    # 兜底：客户端异常退出时可能留下 sidecar。仅清理安装目录里那一个后端 exe，
    # 且在客户端本体已经不在的情况下才动手。
    if (@(Get-OwnedProcesses $AppExe).Count -eq 0) {
        $leftovers = @(Get-OwnedProcesses $BackendExe)
        foreach ($item in $leftovers) {
            Write-Host "清理残留 sidecar PID $($item.Id)" -ForegroundColor Yellow
            Stop-Process -Id $item.Id -Force -ErrorAction SilentlyContinue
        }
        if (@(Get-OwnedProcesses $BackendExe).Count -eq 0) {
            $runtimePath = Join-Path (Get-DataRoot) "runtime\runtime.json"
            if (Test-Path -LiteralPath $runtimePath -PathType Leaf) {
                Remove-Item -LiteralPath $runtimePath -Force -ErrorAction SilentlyContinue
            }
        }
    }
    Write-Host "已停止。" -ForegroundColor Green
}

switch ($Action) {
    "start" { Invoke-Start }
    "status" { Invoke-Status }
    "stop" { Invoke-Stop }
}
