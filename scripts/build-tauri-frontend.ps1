$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$webRoot = Join-Path $projectRoot "web"
# The packaged desktop shell discovers its per-launch sidecar URL through the
# Rust ownership handshake. Never bake a fixed development port into dist.
$env:VITE_API_BASE_URL = ""
Push-Location $webRoot
try {
    cmd /c "npm run build"
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "npm run build failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
}
