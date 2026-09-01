# PC one-click startup entry point.
# Usage: powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1
#        powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1 -Restart
# Service lifecycle is owned by jax-services.ps1; this wrapper must not spawn relay directly.
param(
    [switch]$Restart
)
$ErrorActionPreference = "Stop"
$SvcScript = Join-Path $PSScriptRoot "jax-services.ps1"
if (-not (Test-Path $SvcScript)) {
    Write-Error "[services][x] service manager missing: $SvcScript"
    exit 1
}

$action = if ($Restart) { "restart" } else { "start" }
Write-Host "[services] delegating $action all to jax-services.ps1"
& $SvcScript $action all
$exitCode = $LASTEXITCODE
if ($null -eq $exitCode) { $exitCode = 0 }
if ($exitCode -ne 0) {
    Write-Error "[services][x] $action all failed (exit=$exitCode)"
    exit $exitCode
}
Write-Host "[services][ok] $action all completed"
exit 0
