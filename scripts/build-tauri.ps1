$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$frontendScript = Join-Path $PSScriptRoot "build-tauri-frontend.ps1"
$sidecarScript = Join-Path $PSScriptRoot "build-sidecar.ps1"
foreach ($required in @($frontendScript, $sidecarScript, (Join-Path $projectRoot "web\package.json"), (Join-Path $projectRoot "apps\sidecar.py"))) {
  if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
    throw "V1.2 release build input is missing: $required"
  }
}
Push-Location $projectRoot
try {
  & $frontendScript
  if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "frontend release build failed with exit code $LASTEXITCODE" }
  & $sidecarScript
  if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "sidecar release build failed with exit code $LASTEXITCODE" }
}
finally {
  Pop-Location
}
