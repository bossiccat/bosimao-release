# PC one-click startup (live link assembly): model -> backend -> relay_client (public relay bridge)
# Usage: powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1
#        Force restart: powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1 -Restart
# Idempotent: skips services already listening/running; -Restart kills old processes first.
# Chain: PhoneApp -> PublicRelay(wss://jax-relay.../relay/ws) -> relay_client -> LocalVoiceGateway(ws://127.0.0.1:8000/ws/voice)
param(
    [switch]$Restart
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# 0) Load .env into process env (RELAY_TOKEN / RELAY_E2EE_KEY / VOICE_TOKEN etc.)
$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
        }
    }
    Write-Host "[env] .env loaded"
} else {
    Write-Warning "[env] .env NOT FOUND; credentials missing"
}

# ---------- 1) Model service :19080 (start-model.ps1 logic: idempotent + health wait) ----------
Write-Host "==> [1/3] model http://127.0.0.1:19080/health"
& powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start-model.ps1")
if ($LASTEXITCODE -ne 0) { Write-Error "[1/3] model start failed"; exit 1 }
Write-Host "[ok] model ready (start-model.ps1 waited for /health)"

# ---------- 2) Backend uvicorn :8000 (cwd=backend/, idempotent) ----------
Write-Host "==> [2/3] backend http://127.0.0.1:8000/health"
$listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($listening -and -not $Restart) {
    Write-Host ("[backend] port 8000 already listening, skip (idempotent). PID=" + ($listening.OwningProcess -join ","))
} else {
    if ($listening -and $Restart) {
        Write-Host ("[backend] -Restart: kill PID " + ($listening.OwningProcess -join ","))
        Stop-Process -Id $listening.OwningProcess -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    $log = Join-Path $LogDir "backend.log"
    Write-Host "[backend] start: $Py -m uvicorn app.main:app --host 127.0.0.1 --port 8000 (log $log)"
    Start-Process -FilePath $Py -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000" `
        -WorkingDirectory (Join-Path $Root "backend") -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(90)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch {}
    }
    if (-not $ready) { Write-Error "[2/3] backend not ready in 90s, see $log / $log.err"; exit 1 }
    Write-Host "[ok] backend ready"
}

# ---------- 3) relay_client (bridge public relay <-> local voice gateway, idempotent) ----------
Write-Host "==> [3/3] relay_client (public relay <-> local voice gateway)"
$relayUrl = "wss://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com/relay/ws"
$gwUrl    = "ws://127.0.0.1:8000/ws/voice"
$pairCode = "JAX2026"
$token    = $env:RELAY_TOKEN
$e2eeKey  = $env:RELAY_E2EE_KEY
if (-not $token)  { Write-Warning "[relay_client] RELAY_TOKEN empty; relay will reject pair (must configure)" }
if (-not $e2eeKey){ Write-Warning "[relay_client] RELAY_E2EE_KEY empty; plaintext mode (phone must match)" }

$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "relay_client" }
if ($existing -and -not $Restart) {
    Write-Host ("[relay_client] already running, skip (idempotent). PID=" + ($existing.ProcessId -join ","))
} else {
    if ($existing -and $Restart) {
        foreach ($p in $existing) { Write-Host "[relay_client] -Restart: kill PID $($p.ProcessId)"; Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
    }
    $log = Join-Path $LogDir "relay_client.log"
    $relayArgs = @("-m", "backend.relay.relay_client",
        "--relay", $relayUrl,
        "--gateway", $gwUrl,
        "--pairing-code", $pairCode,
        "--token", $token,
        "--e2ee-key", $e2eeKey)
    Write-Host ("[relay_client] start: $Py " + ($relayArgs -join " ") + " (log $log)")
    Start-Process -FilePath $Py -ArgumentList $relayArgs -WorkingDirectory $Root `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden
    # Verify pairing via log (connect + pair takes a few seconds)
    $deadline = (Get-Date).AddSeconds(25)
    $paired = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if (Test-Path $log) {
            if (Select-String -Path $log -Pattern "relay paired" -Quiet -ErrorAction SilentlyContinue) { $paired = $true; break }
            if (Select-String -Path $log -Pattern "auth_failed|pair failed|pairing failed" -Quiet -ErrorAction SilentlyContinue) { break }
        }
    }
    if ($paired) { Write-Host "[ok] relay_client paired with relay (waiting for phone peer)" }
    else { Write-Warning "[!] pair not confirmed, see $log / $log.err (normal if phone peer not connected yet)" }
}

Write-Host "==> ALL STARTED. Phone app config: relay mode + pairing code JAX2026 + dev key jax-voice-dev-e2ee-20260803-0001"
