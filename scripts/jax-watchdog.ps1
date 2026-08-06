# ============================================================
# jax-watchdog.ps1 — 贾克斯三件套自愈 watchdog（单次运行）
# 触发：计划任务（开机 AtStartup + 每 5 分钟）由 install-scheduled-tasks.ps1 注册
# 逻辑：检查三件套 → 哪个挂了自动拉起（调用 jax-services.ps1 start <svc>）
#       → 动作写 logs/watchdog.log（静默成功，不写日志）
# 中继假死检测：relay_client 日志最近 5 分钟几乎全为错误 → 重启 relay_client
# 防风暴：每服务 10 分钟内最多重启 3 次，超限写告警不再拉起
# 说明：本文件必须保持 UTF-8 with BOM（PS 5.1 中文解析依赖 BOM）
# ============================================================
$ErrorActionPreference = "SilentlyContinue"
$Root      = Split-Path -Parent $PSScriptRoot
$SvcScript = Join-Path $PSScriptRoot "jax-services.ps1"
$LogDir    = Join-Path $Root "logs"
$WatchLog  = Join-Path $LogDir "watchdog.log"
$StateFile = Join-Path $Root "data\pids\.watchdog_state.json"
$WindowMin = 10      # 防风暴窗口（分钟）
$MaxRestart = 3      # 窗口内最多重启次数
$RelayWindowMin = 5  # 中继假死判定窗口（分钟）

New-Item -ItemType Directory -Force -Path $LogDir, (Split-Path $StateFile) | Out-Null

function Write-WatchLog([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $WatchLog -Value $line -Encoding UTF8
}

# ---------------- 防风暴状态（JSON: {svc: [ISO时间戳,...]}） ----------------
function Load-State {
    # 始终返回 Hashtable（PSCustomObject 转成 Hashtable，避免属性/键访问不一致）
    if (Test-Path $StateFile) {
        try {
            $obj = Get-Content $StateFile -Raw | ConvertFrom-Json
            if ($obj) {
                $ht = @{}
                foreach ($prop in $obj.PSObject.Properties) { $ht[$prop.Name] = @($prop.Value) }
                return $ht
            }
        } catch { }
    }
    return @{}
}
function Save-State($state) {
    $state | ConvertTo-Json -Depth 4 | Set-Content -Path $StateFile -Encoding UTF8
}
function Get-RestartCount([string]$svc) {
    $state = Load-State
    $cutoff = (Get-Date).AddMinutes(-$WindowMin)
    $count = 0
    $arr = @($state[$svc])
    if ($arr.Count -gt 0 -and $null -ne $arr[0]) {
        foreach ($t in $arr) {
            try {
                if ([datetime]$t -ge $cutoff) { $count++ }
            } catch { }
        }
    }
    return $count
}
function Record-Restart([string]$svc) {
    $state = Load-State
    $arr = @()
    if ($state.ContainsKey($svc)) { $arr = @($state[$svc]) }
    $arr += (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
    # 仅保留窗口内时间戳，防止状态文件无限增长
    $cutoff = (Get-Date).AddMinutes(-$WindowMin)
    $arr = @($arr | Where-Object { try { [datetime]$_ -ge $cutoff } catch { $false } })
    $state[$svc] = $arr
    Save-State $state
}

# ---------------- 健康检查 ----------------
function Test-Health([string]$Url, [int]$TimeoutSec = 3) {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}
function Get-RelayProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "relay_client" }
}
function Test-RelayAlive {
    # relay_client 进程存在 且 未处于假死错误循环 → 健康
    $procs = @(Get-RelayProcesses)
    if ($procs.Count -eq 0) { return $false }
    return (-not (Test-RelayDeadLoop))
}
function Test-RelayDeadLoop {
    # 中继假死判定：relay_client 日志最近 5 分钟几乎全为 error/connect failed/loop end
    $log  = Join-Path $LogDir "relay_client.log"
    $err  = Join-Path $LogDir "relay_client.log.err"
    $since = (Get-Date).AddMinutes(-$RelayWindowMin)
    $lines = @()
    foreach ($f in @($log, $err)) {
        if (Test-Path $f) {
            $tail = @(Get-Content $f -Tail 300 -ErrorAction SilentlyContinue)
            foreach ($l in $tail) {
                if ($l -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
                    try {
                        $ts = [datetime]::ParseExact($matches[1], "yyyy-MM-dd HH:mm:ss", $null)
                        if ($ts -ge $since) { $lines += $l }
                    } catch { }
                }
            }
        }
    }
    if ($lines.Count -lt 3) { return $false }   # 日志太少，无法判定
    # 有健康迹象（配对/注册/网关就绪）→ 不算假死
    if ($lines | Where-Object { $_ -match "relay paired|relay registered|voice gateway ready|relay event: paired|heartbeat" }) {
        return $false
    }
    $errLines = @($lines | Where-Object { $_ -match "relay event: error|relay connect failed|relay loop end|gateway loop end" })
    return ($errLines.Count -ge 3)
}

# ---------------- 拉起服务 ----------------
function Start-One([string]$svc) {
    $count = Get-RestartCount $svc
    if ($count -ge $MaxRestart) {
        Write-WatchLog "[$svc] 异常但 10 分钟内已重启 $count 次（上限 $MaxRestart），跳过拉起（防风暴）"
        return
    }
    Write-WatchLog "[$svc] 异常，拉起第 $($count + 1) 次 ..."
    # 直接以子作用域调用 jax-services.ps1（不嵌套 powershell.exe，避免沙箱/会话回收）
    $out = & $SvcScript start $svc 2>&1
    $ok = ($null -eq $out -or ($out | Out-String) -notmatch "\[x\]")
    # 拉起后复查健康
    Start-Sleep -Seconds 2
    $healthyAfter = switch ($svc) {
        "model"   { Test-Health "http://127.0.0.1:19080/health" }
        "backend" { Test-Health "http://127.0.0.1:8000/health" }
        "relay"   { @(Get-RelayProcesses).Count -gt 0 }
    }
    if ($healthyAfter) {
        Record-Restart $svc
        Write-WatchLog "[$svc] 拉起成功（复查健康）"
    } else {
        Write-WatchLog "[$svc] 拉起后复查仍不健康：$($out -join ' ')"
    }
}

# ---------------- 主流程：只对异常服务动作，静默成功 ----------------
foreach ($svc in @("model","backend","relay")) {
    $healthy = switch ($svc) {
        "model"   { Test-Health "http://127.0.0.1:19080/health" }
        "backend" { Test-Health "http://127.0.0.1:8000/health" }
        "relay"   { Test-RelayAlive }
    }
    if ($healthy) { continue }
    Start-One $svc
}
exit 0
