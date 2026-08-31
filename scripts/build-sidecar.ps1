$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$sidecarRoot = Join-Path $projectRoot "src-tauri"
$binaryRoot = Join-Path $sidecarRoot "binaries"
$buildRoot = Join-Path $projectRoot "build\sidecar"
$distRoot = Join-Path $buildRoot "dist"
$workRoot = Join-Path $buildRoot "work"
New-Item -ItemType Directory -Force -Path $binaryRoot, $buildRoot | Out-Null
if (Test-Path $distRoot) { Remove-Item -LiteralPath $distRoot -Recurse -Force }
if (Test-Path $workRoot) { Remove-Item -LiteralPath $workRoot -Recurse -Force }
$python = (Get-Command python -ErrorAction Stop).Source
Push-Location $projectRoot
try {
    & $python -m PyInstaller --noconfirm --clean --onefile --name ai-market-analyst-backend `
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
