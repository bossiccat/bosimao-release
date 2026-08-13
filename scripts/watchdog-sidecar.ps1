# watchdog-sidecar.ps1 —— sidecar 看门狗（v0.6.4）
# 每 30s 检查 sidecar 健康（19093 /health 的 sidecar_connected），不健康则：
#   1. 强杀所有 electron 残留（含僵尸壳）
#   2. 重新拉起 sidecar（--in-process-gpu --hold=86400 常驻）
# 运行方式：powershell -WindowStyle Hidden -File watchdog-sidecar.ps1（脱离会话常驻）
$ErrorActionPreference = 'SilentlyContinue'

$Root      = Split-Path -Parent $PSScriptRoot
$SidecarDir = Join-Path $Root "sidecar"
$Electron  = Join-Path $SidecarDir "node_modules\.bin\electron.cmd"
$SignUrl   = "https://jinhong-d2g55ycl591208475-1436773060.ap-shanghai.app.tcloudbase.com"
$LogDir    = Join-Path $Root "logs"
$WatchLog  = Join-Path $LogDir "watchdog.log"

function Write-WatchLog($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $WatchLog -Value $line
}

Write-WatchLog "watchdog started (poll 30s, sidecar dir: $SidecarDir)"

while ($true) {
    $healthy = $false
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:19093/health" -TimeoutSec 3
        if ($r.sidecar_connected -eq $true) { $healthy = $true }
    } catch { }

    if (-not $healthy) {
        Write-WatchLog "sidecar 不健康（sidecar_connected=$healthy），开始恢复"
        # 1. 清理所有 electron 残留（僵尸壳/半死实例）
        Get-Process -Name electron -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
        # 2. 重新拉起 sidecar
        $ts = Get-Date -Format "yyyyMMdd-HHmmss"
        $out = Join-Path $LogDir "sidecar_wd_$ts.out"
        $err = Join-Path $LogDir "sidecar_wd_$ts.err"
        Start-Process -FilePath $Electron -ArgumentList @(".", "--in-process-gpu", "--role=sidecar", "--device=sidecar-dev-1", "--sign-url=$SignUrl", "--bridge-url=ws://127.0.0.1:19092", "--hold=86400") -WorkingDirectory $SidecarDir -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden
        Write-WatchLog "已重新拉起 sidecar（out=$out）"
        Start-Sleep -Seconds 10
    }

    Start-Sleep -Seconds 30
}
