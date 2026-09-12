$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$webRoot = Join-Path $projectRoot "web"
$distRoot = Join-Path $webRoot "dist"
$archiveRoot = Join-Path $projectRoot "build\web-dist-archive"
$KeepArchives = 3

# Vite 清空 outDir 的方式是逐个删除旧文件。在受限的宿主里这会被批量删除保护
# 拦下、把构建打断在半途；而且它同时也是旧产物唯一的一份拷贝。改名挪走是一次
# 元数据操作：构建随后写进干净的 dist，上一版产物也留了下来可以对比。
if (Test-Path -LiteralPath $distRoot) {
    New-Item -ItemType Directory -Force -Path $archiveRoot | Out-Null
    $archive = Join-Path $archiveRoot (Get-Date -Format "yyyyMMdd-HHmmss")
    Move-Item -LiteralPath $distRoot -Destination $archive -Force
    Write-Host "已归档上一版 web/dist -> $archive"
}

# The packaged desktop shell discovers its per-launch sidecar URL through the
# Rust ownership handshake. Never bake a fixed development port into dist.
$env:VITE_API_BASE_URL = ""
Push-Location $webRoot
try {
    cmd /c "npm run build"
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "npm run build failed with exit code $LASTEXITCODE" }
    # 不用"退出码为 0"冒充成功：产物必须真的在。
    if (-not (Test-Path -LiteralPath (Join-Path $distRoot "index.html") -PathType Leaf)) {
        throw "npm run build 未产出 dist\index.html"
    }
}
finally {
    Pop-Location
}

# 清理旧归档。删除是尽力而为：宿主若禁止批量删除，不能因此判定构建失败。
$archives = @(
    Get-ChildItem -LiteralPath $archiveRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending
)
foreach ($stale in @($archives | Select-Object -Skip $KeepArchives)) {
    try {
        Remove-Item -LiteralPath $stale.FullName -Recurse -Force -ErrorAction Stop
    } catch {
        Write-Warning "未能清理旧归档 $($stale.FullName)：$($_.Exception.Message)"
    }
}
