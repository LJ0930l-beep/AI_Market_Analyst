$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$sidecarRoot = Join-Path $projectRoot "src-tauri"
$binaryRoot = Join-Path $sidecarRoot "binaries"
$buildRoot = Join-Path $projectRoot "build\sidecar"

# 每次构建写进带时间戳的新目录，而不是先删掉上一次的 dist/work：
#   * 产物可追溯，不会出现"拿旧 exe 冒充新 exe"的歧义；
#   * 不触发递归删除。受限宿主里的批量删除保护会把构建直接打断
#     （实测：SAFE_DELETE_BULK_CONFIRM_REQUIRED，进程被 SystemExit 终止）。
$runTag = Get-Date -Format "yyyyMMdd-HHmmss"
$runRoot = Join-Path $buildRoot $runTag
$distRoot = Join-Path $runRoot "dist"
$workRoot = Join-Path $runRoot "work"
New-Item -ItemType Directory -Force -Path $binaryRoot, $runRoot, $distRoot, $workRoot | Out-Null

$python = (Get-Command python -ErrorAction Stop).Source
$entryPoint = Join-Path $projectRoot "apps\sidecar.py"
Push-Location $projectRoot
try {
    # 不使用 --clean：dist/work 每次都是全新的时间戳目录，PyInstaller 自身的
    # --clean 还会再去清缓存目录并触发批量删除确认。
    & $python -m PyInstaller --noconfirm --onefile --name ai-market-analyst-backend `
        --hidden-import ccxt --hidden-import ccxt.gate `
        --exclude-module torch --exclude-module torchvision --exclude-module torchaudio `
        --exclude-module scipy --exclude-module matplotlib --exclude-module sympy `
        --exclude-module PIL --exclude-module cv2 --exclude-module av `
        --distpath $distRoot --workpath $workRoot --specpath $runRoot $entryPoint
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
    $built = Join-Path $distRoot "ai-market-analyst-backend.exe"
    if (-not (Test-Path -LiteralPath $built -PathType Leaf)) { throw "PyInstaller did not produce $built" }
    foreach ($target in @(
        (Join-Path $binaryRoot "ai-market-analyst-backend-x86_64-pc-windows-gnu.exe"),
        (Join-Path $binaryRoot "ai-market-analyst-backend-x86_64-pc-windows-msvc.exe")
    )) {
        Copy-Item -LiteralPath $built -Destination $target -Force
    }
    Write-Host ("sidecar: {0} ({1:N0} bytes, tag {2})" -f $built, (Get-Item -LiteralPath $built).Length, $runTag)
}
finally {
    Pop-Location
}
