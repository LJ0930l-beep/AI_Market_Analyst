param(
    [string]$OutputDir = 'D:\RJ\codex\_ollama_install\qwen3.5-4b-parts'
)

$ErrorActionPreference = 'Stop'
$url = 'https://registry.ollama.ai/v2/library/qwen3.5/blobs/sha256:81fb60c7daa80fc1123380b98970b320ae233409f0f71a72ed7b9b0d62f40490'
$digest = '81fb60c7daa80fc1123380b98970b320ae233409f0f71a72ed7b9b0d62f40490'
$size = [int64]3389971840
$partCount = 4
$partSize = [int64]($size / $partCount)

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$jobs = @()
for ($index = 0; $index -lt $partCount; $index++) {
    $start = [int64]$index * $partSize
    $end = if ($index -eq ($partCount - 1)) { $size - 1 } else { $start + $partSize - 1 }
    $partPath = Join-Path $OutputDir ('part-{0:D2}.bin' -f $index)
    $expectedLength = $end - $start + 1
    $existing = Get-Item -LiteralPath $partPath -ErrorAction SilentlyContinue
    if ($existing -and $existing.Length -eq $expectedLength) {
        Write-Output ('reuse part {0:D2} length={1}' -f $index, $existing.Length)
        continue
    }
    if ($existing) {
        Remove-Item -LiteralPath $partPath
    }
    $range = '{0}-{1}' -f $start, $end
    $jobs += Start-Job -ScriptBlock {
        param($downloadUrl, $downloadPath, $downloadRange, $expected)
        & curl.exe --http1.1 --location --fail --retry 12 --retry-all-errors --connect-timeout 30 --max-time 3600 --range $downloadRange --output $downloadPath $downloadUrl 2>&1 | Out-Null
        $exitCode = $LASTEXITCODE
        $length = 0
        if (Test-Path -LiteralPath $downloadPath) {
            $length = (Get-Item -LiteralPath $downloadPath).Length
        }
        [pscustomobject]@{
            Path = $downloadPath
            Range = $downloadRange
            Expected = $expected
            Length = $length
            ExitCode = $exitCode
        }
    } -ArgumentList $url, $partPath, $range, $expectedLength
}

while (($jobs | Where-Object { $_.State -eq 'Running' }).Count -gt 0) {
    $progress = foreach ($job in $jobs) {
        $part = Get-Item -LiteralPath (Join-Path $OutputDir ('part-{0:D2}.bin' -f $jobs.IndexOf($job))) -ErrorAction SilentlyContinue
        if ($part) { $part.Length } else { 0 }
    }
    Write-Output ('running={0} bytes={1}' -f (($jobs | Where-Object { $_.State -eq 'Running' }).Count), (($progress | Measure-Object -Sum).Sum))
    Start-Sleep -Seconds 15
}

$results = foreach ($job in $jobs) {
    Receive-Job -Job $job -Keep
    Remove-Job -Job $job -Force
}
$results | Format-Table -AutoSize | Out-String | Write-Output

$bad = @($results | Where-Object { $_.ExitCode -ne 0 -or $_.Length -ne $_.Expected })
if ($bad.Count -gt 0) {
    throw ('download failed for {0} parts' -f $bad.Count)
}

$blobPath = Join-Path (Split-Path -Parent $OutputDir) 'qwen3.5-4b.blob'
if (Test-Path -LiteralPath $blobPath) {
    Remove-Item -LiteralPath $blobPath
}
$outStream = [IO.File]::Open($blobPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
try {
    for ($index = 0; $index -lt $partCount; $index++) {
        $partPath = Join-Path $OutputDir ('part-{0:D2}.bin' -f $index)
        $inStream = [IO.File]::OpenRead($partPath)
        try { $inStream.CopyTo($outStream) } finally { $inStream.Dispose() }
    }
} finally {
    $outStream.Dispose()
}

$hash = (Get-FileHash -LiteralPath $blobPath -Algorithm SHA256).Hash.ToLowerInvariant()
$length = (Get-Item -LiteralPath $blobPath).Length
Write-Output ('blob={0} length={1} sha256={2}' -f $blobPath, $length, $hash)
if ($length -ne $size -or $hash -ne $digest) {
    throw 'assembled blob does not match the official digest'
}
