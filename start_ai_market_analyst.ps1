# Gemini uses Antigravity Tools; no local GPU server.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\desktop-client.ps1') -Action start
