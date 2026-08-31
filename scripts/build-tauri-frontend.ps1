$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$webRoot = Join-Path $projectRoot "web"
$env:VITE_API_BASE_URL = "http://127.0.0.1:18765"
Push-Location $webRoot
try {
    npm run build
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "npm run build failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
}
