# Probe Bonsai 2 27B health and model endpoint
$HostAddr = "127.0.0.1"
$Port = 8080

Write-Host "Probing http://${HostAddr}:${Port}/health ..." -ForegroundColor Cyan
try {
    $health = Invoke-RestMethod -Uri "http://${HostAddr}:${Port}/health" -TimeoutSec 3
    Write-Host "[OK] Server status: $($health.status)" -ForegroundColor Green
    
    $models = Invoke-RestMethod -Uri "http://${HostAddr}:${Port}/v1/models" -TimeoutSec 3
    Write-Host "[OK] Available models:" -ForegroundColor Green
    $models.data | ForEach-Object {
        Write-Host "     - ID: $($_.id), Owned by: $($_.owned_by)"
    }
} catch {
    Write-Host "[ERR] Failed to connect to Bonsai server: $_" -ForegroundColor Red
    exit 1
}
