$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$sidecarRoot = Join-Path $projectRoot "src-tauri"
$binaryRoot = Join-Path $sidecarRoot "binaries"
$buildRoot = Join-Path $projectRoot "build\sidecar"
$distRoot = Join-Path $buildRoot "dist"
$workRoot = Join-Path $buildRoot "work"
New-Item -ItemType Directory -Force -Path $binaryRoot, $buildRoot | Out-Null
foreach ($buildTarget in @($distRoot, $workRoot)) {
    $resolvedTarget = [IO.Path]::GetFullPath($buildTarget)
    if (-not $resolvedTarget.StartsWith($buildRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "构建清理路径超出 sidecar 构建目录，已拒绝操作。"
    }
    foreach ($checkedPath in @($buildRoot, $resolvedTarget)) {
        if ((Test-Path -LiteralPath $checkedPath) -and ((Get-Item -LiteralPath $checkedPath).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "构建路径包含链接，已拒绝自动清理。"
        }
    }
    if (Test-Path -LiteralPath $resolvedTarget) { Remove-Item -LiteralPath $resolvedTarget -Recurse -Force }
}
$python = (Get-Command python -ErrorAction Stop).Source
Push-Location $projectRoot
try {
    & $python -m PyInstaller --noconfirm --clean --onefile --name ai-market-analyst-backend `
        --hidden-import ccxt --hidden-import ccxt.gate `
        --exclude-module torch --exclude-module torchvision --exclude-module torchaudio `
        --exclude-module scipy --exclude-module matplotlib --exclude-module sympy `
        --exclude-module PIL --exclude-module cv2 --exclude-module av `
        --distpath $distRoot --workpath $workRoot --specpath $buildRoot apps\sidecar.py
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
    $built = Join-Path $distRoot "ai-market-analyst-backend.exe"
    if (-not (Test-Path $built)) { throw "PyInstaller did not produce $built" }
    foreach ($target in @(
        (Join-Path $binaryRoot "ai-market-analyst-backend-x86_64-pc-windows-gnu.exe"),
        (Join-Path $binaryRoot "ai-market-analyst-backend-x86_64-pc-windows-msvc.exe")
    )) {
        Copy-Item -LiteralPath $built -Destination $target -Force
    }
}
finally {
    Pop-Location
}
