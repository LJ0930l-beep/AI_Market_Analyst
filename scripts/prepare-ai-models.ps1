# Read-only Gemini completion probe; no downloads or trading.
[CmdletBinding()]
param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Push-Location (Join-Path $PSScriptRoot '..')
try {
    & $Python (Join-Path $PSScriptRoot 'verify_model_connection.py')
    if ($LASTEXITCODE -ne 0) { throw 'Gemini connection probe failed.' }
} finally { Pop-Location }
