# watchdog-check.ps1 —— 单次检查：rtc_bridge + sidecar 双看门（由计划任务每分钟调用）
# 由 watchdog-register.ps1 注册为计划任务 "jax-watchdog"，系统级常驻，不依赖任何会话。
$ErrorActionPreference = 'SilentlyContinue'

$Root      = Split-Path -Parent $PSScriptRoot
$LogDir    = Join-Path $Root "logs"
$WatchLog  = Join-Path $LogDir "watchdog.log"
$Py        = Join-Path $Root ".venv\Scripts\python.exe"
$SidecarDir = Join-Path $Root "sidecar"
$Electron  = Join-Path $SidecarDir "node_modules\.bin\electron.cmd"
$SignUrl   = "https://jinhong-d2g55ycl591208475-1436773060.ap-shanghai.app.tcloudbase.com"

function Write-WatchLog($msg) {
    Add-Content -Path $WatchLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
}

# ---------- 1. rtc_bridge 健康（19093）----------
$bridgeOk = $false
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:19093/health" -TimeoutSec 3
    if ($r.status -eq "ok") { $bridgeOk = $true }
} catch { }
if (-not $bridgeOk) {
    Write-WatchLog "rtc_bridge 不健康，拉起"
    $ts = Get-Date -Format "yyyyMMdd-HHmmss"
    Start-Process -FilePath $Py -ArgumentList @("-m", "rtc_bridge.main") -WorkingDirectory (Join-Path $Root "backend") -RedirectStandardOutput (Join-Path $LogDir "rtc_bridge_wd_$ts.log") -RedirectStandardError (Join-Path $LogDir "rtc_bridge_wd_$ts.err") -WindowStyle Hidden
    Start-Sleep -Seconds 8
}

# ---------- 2. sidecar 健康（19093 sidecar_connected）----------
$sidecarOk = $false
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:19093/health" -TimeoutSec 3
    if ($r.sidecar_connected -eq $true) { $sidecarOk = $true }
} catch { }
if (-not $sidecarOk) {
    Write-WatchLog "sidecar 不健康（sidecar_connected=$sidecarOk），恢复"
    Get-Process -Name electron -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    $ts = Get-Date -Format "yyyyMMdd-HHmmss"
    Start-Process -FilePath $Electron -ArgumentList @(".", "--in-process-gpu", "--role=sidecar", "--device=sidecar-dev-1", "--sign-url=$SignUrl", "--bridge-url=ws://127.0.0.1:19092", "--hold=86400") -WorkingDirectory $SidecarDir -RedirectStandardOutput (Join-Path $LogDir "sidecar_wd_$ts.out") -RedirectStandardError (Join-Path $LogDir "sidecar_wd_$ts.err") -WindowStyle Hidden
    Write-WatchLog "sidecar 已重新拉起"
}
