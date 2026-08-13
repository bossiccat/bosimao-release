# 本地中继启动脚本（幂等）
# 用法: powershell -ExecutionPolicy Bypass -File scripts/start-relay.ps1
# 幂等: 端口 19090 已监听则直接跳过；-Restart 强制终止旧进程后重启
param(
    [switch]$Restart
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Port = 19090
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$PyW = Join-Path $Root ".venv\Scripts\pythonw.exe"   # GUI 子系统，无命令窗

# 1) 幂等检查：端口已监听则跳过
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening -and -not $Restart) {
    Write-Host "[relay] 端口 $Port 已在监听，跳过启动（幂等）。PID: $($listening.OwningProcess)"
    exit 0
}
if ($listening -and $Restart) {
    Write-Host "[relay] -Restart：终止旧进程 PID $($listening.OwningProcess)"
    Stop-Process -Id $listening.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# 2) 加载 .env 到进程环境（RELAY_TOKEN / RELAY_E2EE_KEY 等；config.py 只读 os.environ）
$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
        }
    }
    Write-Host "[relay] 已加载 .env（含 RELAY_TOKEN/RELAY_E2EE_KEY）"
} else {
    Write-Host "[relay] 警告：未找到 .env，中继将以开发态启动（无鉴权）"
}

# 3) 启动中继（uvicorn 0.0.0.0:19090，独立进程 + 日志重定向）
$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "relay.log"
Write-Host "[relay] 启动中继: $PyW -m backend.relay.relay_server（日志 $log）"
Start-Process -FilePath $PyW -ArgumentList "-m", "backend.relay.relay_server" `
    -WorkingDirectory $Root -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden

# 4) 健康轮询（最多 30s）
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/relay/health" -TimeoutSec 2
        Write-Host "[relay] 就绪: $($h | ConvertTo-Json -Compress)"
        exit 0
    } catch {}
}
Write-Host "[relay] 未在 30s 内就绪，请查看 $log"
exit 1
