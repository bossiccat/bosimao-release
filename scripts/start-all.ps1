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
$PyW = Join-Path $Root ".venv\Scripts\pythonw.exe"   # GUI 子系统，无命令窗（relay 用）
$BackendExe = Join-Path $Root "jax-backend.exe"      # 阶段 D 品牌化：后端不再裸 pythonw -m uvicorn
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ---------- owner credential 首启 provision（ADR-022：backend 之前、Load-Env 之前，fail-closed） ----------
function Invoke-OwnerCredentialProvision {
    $exe = $null
    $release = Join-Path $Root "pet-ui\src-tauri\target\release\provision_owner_credential.exe"
    $debug   = Join-Path $Root "pet-ui\src-tauri\target\debug\provision_owner_credential.exe"
    if (Test-Path $release) { $exe = $release }
    elseif (Test-Path $debug) { $exe = $debug }
    else {
        Write-Warning "[owner-credential] provisioner 未编译；请先: cd pet-ui/src-tauri; cargo build --bin provision_owner_credential"
        return $false
    }
    # GUI 子系统二进制（windows_subsystem="windows"）——不带 -WindowStyle（避免冲突），-Wait 等退出码
    $p = Start-Process -FilePath $exe -Wait -PassThru
    if ($p.ExitCode -ne 0) {
        Write-Error "[owner-credential] provision 失败（退出码 $($p.ExitCode)），中止启动（fail-closed）"
        return $false
    }
    Write-Host "[owner-credential][ok] owner credential 已就绪"
    return $true
}
if (-not (Invoke-OwnerCredentialProvision)) { exit 1 }

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
# 2026-08-13 无窗口修复：隐藏窗口调用 start-model.ps1（不再 & powershell 弹窗）
Start-Process -FilePath "powershell" -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File",(Join-Path $PSScriptRoot "start-model.ps1") -WindowStyle Hidden -Wait
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
    if (-not (Test-Path $BackendExe)) { Write-Error "[2/3] jax-backend.exe 不存在: $BackendExe（请先 cd backend/packaging 打包）"; exit 1 }
    Write-Host "[backend] start: $BackendExe --host 127.0.0.1 --port 8000 (log $log)"
    Start-Process -FilePath $BackendExe -ArgumentList "--host", "127.0.0.1", "--port", "8000" `
        -WorkingDirectory $Root -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden
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
    Write-Host ("[relay_client] start: $PyW " + ($relayArgs -join " ") + " (log $log)")
    Start-Process -FilePath $PyW -ArgumentList $relayArgs -WorkingDirectory $Root `
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
