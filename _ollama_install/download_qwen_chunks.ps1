param(
    [string]$OutputDir = 'D:\RJ\codex\_ollama_install\qwen3.5-4b-chunks',
    [int]$Workers = 16
)

$ErrorActionPreference = 'Stop'
$url = 'https://registry.ollama.ai/v2/library/qwen3.5/blobs/sha256:81fb60c7daa80fc1123380b98970b320ae233409f0f71a72ed7b9b0d62f40490'
$digest = '81fb60c7daa80fc1123380b98970b320ae233409f0f71a72ed7b9b0d62f40490'
$size = [int64]3389971840
$chunkSize = [int64](8 * 1024 * 1024)
$chunkCount = [int][math]::Ceiling($size / [double]$chunkSize)
$logPath = Join-Path $OutputDir 'download.log'

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
function Log([string]$Message) {
    $line = ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
    Add-Content -LiteralPath $logPath -Value $line
    Write-Output $line
}

Log ('start chunks={0} chunk_size={1} workers={2}' -f $chunkCount, $chunkSize, $Workers)
while ($true) {
    $pending = @()
    for ($index = 0; $index -lt $chunkCount; $index++) {
        $start = [int64]$index * $chunkSize
        $end = [math]::Min($size - 1, $start + $chunkSize - 1)
        $expectedLength = $end - $start + 1
        $path = Join-Path $OutputDir ('chunk-{0:D4}.bin' -f $index)
        $existing = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
        if ($existing -and $existing.Length -eq $expectedLength) { continue }
        if ($existing) { Remove-Item -LiteralPath $path }
        $pending += [pscustomobject]@{ Index = $index; Start = $start; End = $end; Expected = $expectedLength; Path = $path }
    }
    if ($pending.Count -eq 0) { break }

    Log ('pending={0}' -f $pending.Count)
    for ($offset = 0; $offset -lt $pending.Count; $offset += $Workers) {
        $batch = @($pending[$offset..([math]::Min($pending.Count - 1, $offset + $Workers - 1))])
        $jobs = @()
        foreach ($item in $batch) {
            $tmp = $item.Path + '.tmp'
            if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp }
            $range = '{0}-{1}' -f $item.Start, $item.End
            $jobs += Start-Job -ScriptBlock {
                param($downloadUrl, $downloadPath, $temporaryPath, $downloadRange, $expected)
                & curl.exe --http1.1 --location --fail --retry 12 --retry-all-errors --connect-timeout 30 --max-time 900 --range $downloadRange --output $temporaryPath $downloadUrl 2>&1 | Out-Null
                $exitCode = $LASTEXITCODE
                $length = 0
                if (Test-Path -LiteralPath $temporaryPath) { $length = (Get-Item -LiteralPath $temporaryPath).Length }
                if ($exitCode -eq 0 -and $length -eq $expected) {
                    Move-Item -LiteralPath $temporaryPath -Destination $downloadPath
                } elseif (Test-Path -LiteralPath $temporaryPath) {
                    Remove-Item -LiteralPath $temporaryPath
                }
                [pscustomobject]@{ Path = $downloadPath; Expected = $expected; Length = $length; ExitCode = $exitCode }
            } -ArgumentList $url, $item.Path, $tmp, $range, $item.Expected
        }
        $deadline = (Get-Date).AddMinutes(16)
        while (($jobs | Where-Object { $_.State -eq 'Running' }).Count -gt 0 -and (Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 15
        }
        foreach ($job in $jobs) {
            if ($job.State -eq 'Running') { Stop-Job -Job $job -ErrorAction SilentlyContinue }
            $result = Receive-Job -Job $job -ErrorAction SilentlyContinue
            if ($result) { Log ('chunk result path={0} len={1} exit={2}' -f $result.Path, $result.Length, $result.ExitCode) }
            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        }
        $done = @(Get-ChildItem -LiteralPath $OutputDir -Filter 'chunk-*.bin' | Where-Object { $_.Length -gt 0 } | Measure-Object -Property Length -Sum).Sum
        Log ('batch_done offset={0} bytes={1}/{2}' -f $offset, $done, $size)
    }
}

$blobPath = Join-Path (Split-Path -Parent $OutputDir) 'qwen3.5-4b-smallchunks.blob'
if (Test-Path -LiteralPath $blobPath) { Remove-Item -LiteralPath $blobPath }
$outStream = [IO.File]::Open($blobPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
try {
    for ($index = 0; $index -lt $chunkCount; $index++) {
        $path = Join-Path $OutputDir ('chunk-{0:D4}.bin' -f $index)
        $expectedLength = [math]::Min($size, ([int64]$index + 1) * $chunkSize) - ([int64]$index * $chunkSize)
        $item = Get-Item -LiteralPath $path -ErrorAction Stop
        if ($item.Length -ne $expectedLength) { throw ('incomplete chunk {0}' -f $index) }
        $inStream = [IO.File]::OpenRead($path)
        try { $inStream.CopyTo($outStream) } finally { $inStream.Dispose() }
    }
} finally {
    $outStream.Dispose()
}
$hash = (Get-FileHash -LiteralPath $blobPath -Algorithm SHA256).Hash.ToLowerInvariant()
$length = (Get-Item -LiteralPath $blobPath).Length
Log ('assembled blob={0} length={1} sha256={2}' -f $blobPath, $length, $hash)
if ($length -ne $size -or $hash -ne $digest) { throw 'assembled blob does not match official digest' }
Log 'SUCCESS'
