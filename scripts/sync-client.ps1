#Requires -Version 5.1
<#
.SYNOPSIS
    把当前源码构建成可分发的桌面客户端，并同步覆盖到本机安装目录。

.DESCRIPTION
    「改完代码就同步客户端」的固定入口。执行顺序：

      1. 停止正在运行的自有进程（否则无法覆盖 exe）
      2. 构建前端产物 (web/dist) 与 PyInstaller sidecar
      3. 构建 Tauri 可执行文件与 NSIS 安装包
      4. 覆盖安装目录中的 ai-market-analyst.exe / ai-market-analyst-backend.exe
      5. 可选重新启动客户端

    只处理本产品自己的进程与文件：不按端口或进程名搜索，不触碰 Ollama /
    ComfyUI / 其它无关进程，也不清理 AppData 中的用户数据。

.PARAMETER NoRestart
    构建安装完成后不自动启动客户端。

.PARAMETER SkipBundle
    跳过 NSIS 安装包，只产出可执行文件（更快）。

.PARAMETER SkipBuild
    只安装上一次构建的产物，不重新构建。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sync-client.ps1
#>
[CmdletBinding()]
param(
    [switch]$NoRestart,
    [switch]$SkipBundle,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\AI Market Analyst"
$AppExeName = "ai-market-analyst.exe"
$BackendExeName = "ai-market-analyst-backend.exe"
$BuildTarget = "x86_64-pc-windows-msvc"
$ReleaseRoot = Join-Path $ProjectRoot "src-tauri\target\$BuildTarget\release"
$SidecarSource = Join-Path $ProjectRoot "src-tauri\binaries\ai-market-analyst-backend-$BuildTarget.exe"

. (Join-Path $PSScriptRoot "msvc-env.ps1")

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "== $Message" -ForegroundColor Cyan
}

function Test-HasPyInstaller([string]$PythonExe) {
    # 先真跑一次；在受限会话里可能拿不到退出码，所以再用 site-packages
    # 目录做一次不需要执行进程的判断，避免误判成"没装 PyInstaller"。
    & $PythonExe -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -eq 0) { return $true }
    $root = Split-Path $PythonExe -Parent
    foreach ($relative in @(
            "Lib\site-packages\PyInstaller\__init__.py",
            "lib\site-packages\PyInstaller\__init__.py"
        )) {
        if (Test-Path -LiteralPath (Join-Path $root $relative) -PathType Leaf) { return $true }
    }
    return $false
}

function Resolve-BuildPython {
    # PyInstaller 必须由带 PyInstaller 的解释器执行；WorkBuddy 托管的 python
    # 默认没有它，所以这里显式探测而不是盲信 PATH 里第一个 python。
    $candidates = @()
    if ($env:AIMA_BUILD_PYTHON) { $candidates += $env:AIMA_BUILD_PYTHON }
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        if (Test-HasPyInstaller $candidate) { return $candidate }
    }
    throw "找不到带 PyInstaller 的 Python。请先安装：<python> -m pip install pyinstaller"
}

function Resolve-NpmDirectory {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if ($npm) { return (Split-Path $npm.Source -Parent) }
    $nodeRoot = Join-Path $env:LOCALAPPDATA "Programs\nodejs"
    if (Test-Path (Join-Path $nodeRoot "npm.cmd")) { return $nodeRoot }
    throw "找不到 npm；前端产物无法构建。"
}

function Resolve-Full([string]$Path) {
    try { return [IO.Path]::GetFullPath($Path) } catch { return $Path }
}

function Get-OwnedProcesses([string]$ExePath) {
    # 按可执行文件的完整路径核对，而不是按进程名：同名进程不是我们的，不碰。
    $target = Resolve-Full $ExePath
    $name = [IO.Path]::GetFileNameWithoutExtension($ExePath)
    return @(
        Get-Process -Name $name -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -and ((Resolve-Full $_.Path) -ieq $target)
        }
    )
}

function Stop-OwnedApp {
    $targets = @(
        (Join-Path $InstallDir $AppExeName),
        (Join-Path $InstallDir $BackendExeName)
    )
    $stopped = @()
    foreach ($exe in $targets) {
        foreach ($item in @(Get-OwnedProcesses $exe)) {
            $stopped += "$($item.ProcessName)#$($item.Id)"
            # 先请求正常退出，让客户端自己回收它派生的 sidecar。
            Stop-Process -Id $item.Id -ErrorAction SilentlyContinue
        }
    }
    if ($stopped.Count -gt 0) {
        Write-Host ("已停止自有进程: " + ($stopped -join ", "))
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline) {
            $alive = @($targets | ForEach-Object { Get-OwnedProcesses $_ })
            if ($alive.Count -eq 0) { break }
            Start-Sleep -Milliseconds 250
        }
        foreach ($exe in $targets) {
            foreach ($item in @(Get-OwnedProcesses $exe)) {
                Write-Host "未在 10 秒内退出，强制结束 PID $($item.Id)。" -ForegroundColor Yellow
                Stop-Process -Id $item.Id -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Seconds 1
    } else {
        Write-Host "没有正在运行的自有进程。"
    }
    $left = @($targets | ForEach-Object { Get-OwnedProcesses $_ })
    if ($left.Count -gt 0) { throw "自有进程仍然存活，无法安全覆盖程序文件：$($left.Id -join ', ')" }
}

function Assert-BuildInputs {
    foreach ($required in @(
            (Join-Path $ProjectRoot "web\package.json"),
            (Join-Path $ProjectRoot "apps\sidecar.py"),
            (Join-Path $ProjectRoot "src-tauri\tauri.conf.json")
        )) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "构建输入缺失：$required"
        }
    }
}

Write-Host "AI Market Analyst 客户端同步" -ForegroundColor Green
Write-Host "项目根目录: $ProjectRoot"
Write-Host "安装目录  : $InstallDir"
Write-Host "构建目标  : $BuildTarget"

if (-not $SkipBuild) {
    Assert-BuildInputs

    Write-Step "停止自有进程"
    Stop-OwnedApp

    $buildPython = Resolve-BuildPython
    $pythonDir = Split-Path $buildPython -Parent
    $npmDir = Resolve-NpmDirectory
    Write-Host "PyInstaller 解释器: $buildPython"
    Write-Host "npm 目录          : $npmDir"
    # 构建脚本内部用裸命令 `python` / `npm`，这里把正确的目录提到 PATH 最前面。
    $env:PATH = "$pythonDir;$npmDir;$env:PATH"

    Write-Step "构建前端产物与 sidecar"
    & (Join-Path $PSScriptRoot "build-tauri.ps1")
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "前端/sidecar 构建失败 (exit $LASTEXITCODE)" }

    Write-Step "构建 Tauri 可执行文件"
    # beforeBuildCommand 里的相对路径 ../scripts/build-tauri.ps1 会解析到仓库
    # 之外，因此这里已经手工完成前端与 sidecar 构建并清空该钩子。
    # 链接这一步需要一个真正的 MSVC link.exe —— 普通 PowerShell 里它不在 PATH
    # 上，Git Bash 里又会被 coreutils 的 link.exe 遮蔽。
    Initialize-MsvcEnvironment
    Push-Location (Join-Path $ProjectRoot "src-tauri")
    try {
        $tauriArgs = @(
            "tauri", "build",
            "--target", $BuildTarget,
            "--config", '{"build":{"beforeBuildCommand":""}}'
        )
        if ($SkipBundle) { $tauriArgs += "--no-bundle" }
        $startedAt = Get-Date
        & cargo @tauriArgs
        $cargoExit = $LASTEXITCODE
        if ($cargoExit -and $cargoExit -ne 0) {
            # 打包（NSIS）阶段失败不应让已经产出的 exe 白跑一趟。
            $produced = Join-Path $ReleaseRoot $AppExeName
            $fresh = (Test-Path -LiteralPath $produced) -and
                ((Get-Item -LiteralPath $produced).LastWriteTime -gt $startedAt)
            if ($fresh) {
                Write-Warning "cargo tauri build 退出码 $cargoExit，但本次已产出新的可执行文件，继续安装。"
            } else {
                throw "cargo tauri build 失败 (exit $cargoExit)"
            }
        }
    } finally {
        Pop-Location
    }
}

Write-Step "安装到客户端目录"
if (-not (Test-Path -LiteralPath $InstallDir)) {
    throw "安装目录不存在：$InstallDir（请先运行 NSIS 安装包）"
}
Stop-OwnedApp

$builtExe = Join-Path $ReleaseRoot $AppExeName
if (-not (Test-Path -LiteralPath $builtExe)) { throw "未找到构建产物：$builtExe" }
if (-not (Test-Path -LiteralPath $SidecarSource)) { throw "未找到 sidecar 产物：$SidecarSource" }

$appHash = (Get-FileHash -LiteralPath $builtExe -Algorithm SHA256).Hash
$sidecarHash = (Get-FileHash -LiteralPath $SidecarSource -Algorithm SHA256).Hash

Copy-Item -LiteralPath $builtExe -Destination (Join-Path $InstallDir $AppExeName) -Force
Copy-Item -LiteralPath $SidecarSource -Destination (Join-Path $InstallDir $BackendExeName) -Force

$installedAppHash = (Get-FileHash -LiteralPath (Join-Path $InstallDir $AppExeName) -Algorithm SHA256).Hash
$installedSidecarHash = (Get-FileHash -LiteralPath (Join-Path $InstallDir $BackendExeName) -Algorithm SHA256).Hash
if ($appHash -ne $installedAppHash) { throw "ai-market-analyst.exe 安装后校验不一致" }
if ($sidecarHash -ne $installedSidecarHash) { throw "sidecar 安装后校验不一致" }

Write-Host "ai-market-analyst.exe        -> $appHash"
Write-Host "ai-market-analyst-backend.exe -> $sidecarHash"

$installer = Get-ChildItem -LiteralPath (Join-Path $ReleaseRoot "bundle\nsis") -Filter "*.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($installer) {
    Write-Host "NSIS 安装包: $($installer.FullName)"
}

if (-not $NoRestart) {
    Write-Step "启动客户端"
    Start-Process -FilePath (Join-Path $InstallDir $AppExeName) -WorkingDirectory $InstallDir
    Start-Sleep -Seconds 12
    Get-Process -Name 'ai-market-analyst' -ErrorAction SilentlyContinue |
        ForEach-Object { "已启动 pid=$($_.Id) 窗口=$($_.MainWindowHandle)" }
}

Write-Host ""
Write-Host "客户端同步完成。" -ForegroundColor Green
