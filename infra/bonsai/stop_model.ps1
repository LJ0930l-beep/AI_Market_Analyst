# Stop PrismML Bonsai 2 27B Server
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
Set-Location $ProjectRoot

python (Join-Path $ScriptDir "server_runner.py") stop
