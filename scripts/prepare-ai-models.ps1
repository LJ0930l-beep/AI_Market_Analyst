<#
    Verify the configured local Bonsai 2 27B inference service.

    Model weights are managed by infra\bonsai\start_model.ps1. This script is
    read-only: it does not download weights, start processes, or substitute a
    model from Ollama.
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = $(if ($env:BONSAI_BASE_URL) { $env:BONSAI_BASE_URL } else { "http://127.0.0.1:8080/v1" }),
    [string]$RequiredModel = "Bonsai-2-27B-PTQ1_0"
)

$ErrorActionPreference = "Stop"
$ExpectedModel = "Bonsai-2-27B-PTQ1_0"

if ($RequiredModel -cne $ExpectedModel) {
    throw "Model routing is pinned to $ExpectedModel; alternate model overrides are rejected."
}

try {
    $parsed = [Uri]$BaseUrl
} catch {
    throw "BONSAI_BASE_URL must be a valid local inference URL."
}
if ($parsed.Scheme -cne "http" -or $parsed.Host -notin @("127.0.0.1", "localhost", "::1") -or $parsed.Port -ne 8080 -or $parsed.AbsolutePath.TrimEnd('/') -cne "/v1") {
    throw "Only the local Bonsai OpenAI-compatible endpoint http://127.0.0.1:8080/v1 is supported."
}

function Test-BonsaiIdentity([object]$Value) {
    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value)) { return $false }
    $normalized = $Value.Trim().Replace('\', '/')
    $basename = ($normalized -split '/')[-1]
    if ($basename.EndsWith('.gguf', [StringComparison]::OrdinalIgnoreCase)) {
        $basename = $basename.Substring(0, $basename.Length - 5)
    }
    if ($basename.StartsWith('Ternary-', [StringComparison]::OrdinalIgnoreCase)) {
        $basename = $basename.Substring(8)
    }
    return $basename.Equals($ExpectedModel, [StringComparison]::OrdinalIgnoreCase)
}

$healthUrl = $parsed.GetLeftPart([System.UriPartial]::Authority) + "/health"
try {
    $health = Invoke-RestMethod -Uri $healthUrl -Method Get -TimeoutSec 3
    $manifest = Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/') + "/models") -Method Get -TimeoutSec 5
} catch {
    throw "Bonsai service is not ready at $BaseUrl. Start it with infra\bonsai\start_model.ps1. $($_.Exception.Message)"
}

$rows = @()
if ($manifest.data -is [System.Collections.IEnumerable]) { $rows = @($manifest.data) }
elseif ($manifest.models -is [System.Collections.IEnumerable]) { $rows = @($manifest.models) }
$matching = @()
foreach ($row in $rows) {
    $primary = @()
    foreach ($field in @('id', 'name', 'model')) {
        $value = $row.$field
        if ($value -is [string] -and -not [string]::IsNullOrWhiteSpace($value)) { $primary += $value }
    }
    $primaryValid = $true
    foreach ($value in $primary) {
        if (-not (Test-BonsaiIdentity $value)) { $primaryValid = $false; break }
    }
    if (-not $primaryValid) { continue }
    $candidates = @($primary)
    if ($row.aliases -is [System.Collections.IEnumerable]) { $candidates += @($row.aliases) }
    if (@($candidates | Where-Object { Test-BonsaiIdentity $_ }).Count -gt 0) { $matching += $row }
}

if ($matching.Count -ne 1) {
    throw "Expected exactly one verified $ExpectedModel entry in /v1/models; found $($matching.Count). No other model will be selected."
}

$context = $null
if ($matching[0].meta -and $matching[0].meta.n_ctx) { $context = $matching[0].meta.n_ctx }
elseif ($matching[0].context_length) { $context = $matching[0].context_length }
Write-Host "Bonsai service ready: $ExpectedModel" -ForegroundColor Green
Write-Host "Manifest id: $($matching[0].id)"
Write-Host "Effective context: $(if ($context) { $context } else { 'UNKNOWN_NOT_PROVIDED' })"
