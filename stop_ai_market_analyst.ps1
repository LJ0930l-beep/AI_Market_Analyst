# Stop only the product's path-verified desktop and owned sidecar.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\desktop-client.ps1') -Action stop
