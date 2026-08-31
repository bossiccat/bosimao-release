# ============================================================
# jax-watchdog.ps1 — 贾克斯三件套自愈 watchdog（单次运行）
# 触发：计划任务（开机 AtStartup + 每 5 分钟）由 install-scheduled-tasks.ps1 注册
# 逻辑：检查后台服务 → 哪个挂了自动拉起（调用 jax-services.ps1 start <svc>）
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

# ---------------- 共享函数（单一实现，消除双份定义漂移） ----------------
# 2026-08-21（审计 A11/A2）：Test-Health（含自签 https 兜底）与 Get-RelayProcesses
# （python.exe OR pythonw.exe）统一抽取到 lib-common.ps1，与 jax-services.ps1 共用。
. (Join-Path $PSScriptRoot "lib-common.ps1")

function Test-RelayProcessTree {
    # 一个顶层 relay_client 与其已识别子进程构成唯一受管实例。
    $all = @(Get-RelayProcesses)
    if ($all.Count -eq 0) { return $false }
    $allIds = @($all | ForEach-Object { [int]$_.ProcessId })
    $topLevel = @($all | Where-Object { $allIds -notcontains [int]$_.ParentProcessId })
    if ($topLevel.Count -ne 1) { return $false }

    $knownIds = @([int]$topLevel[0].ProcessId)
    do {
        $before = $knownIds.Count
        $knownIds += @($all | Where-Object {
            $knownIds -contains [int]$_.ParentProcessId
        } | ForEach-Object { [int]$_.ProcessId })
        $knownIds = @($knownIds | Select-Object -Unique)
    } while ($knownIds.Count -gt $before)
    return (@($all | Where-Object { $knownIds -notcontains [int]$_.ProcessId }).Count -eq 0)
}

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
# Test-Health / Get-RelayProcesses 已 dot-source lib-common.ps1（单一实现）
function Test-RelayAlive {
    # 仅一个完整项目 relay_client 进程树且未处于假死错误循环才健康。
    # 多顶层实例必须交给 jax-services.ps1 受控收敛，再由 Start-One 的防风暴限制启动。
    if (-not (Test-RelayProcessTree)) { return $false }
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
    # 防风暴计数必须在拉起动作发生处记录（审计 A11 修复）：
    # 原实现仅在"拉起后复查健康"时计数 → 拉起失败永不累计 → $MaxRestart 形同虚设
    Record-Restart $svc
    # 直接以子作用域调用 jax-services.ps1（不嵌套 powershell.exe，避免沙箱/会话回收）
    $out = & $SvcScript start $svc 2>&1
    $outText = if ($null -ne $out) { @($out) -join ' ' } else { "" }
    # 拉起后复查健康
    Start-Sleep -Seconds 2
    $healthyAfter = switch ($svc) {
        "model"       { Test-Health "http://127.0.0.1:19080/health" }
        "backend"     { Test-BackendHealth }
        "relay"       { Test-RelayAlive }
        "rtc-bridge"  { Test-BridgeHealth }
    }
    if ($healthyAfter) {
        Write-WatchLog "[$svc] 拉起成功（复查健康）"
    } else {
        # $out 是 & 调用的输出数组（含 ErrorRecord），join 后写入日志供排查
        Write-WatchLog "[$svc] 拉起后复查仍不健康（已计防风暴第 $($count + 1) 次）：$outText"
    }
}

# ---------------- rtc-bridge 健康 ----------------
function Test-BridgeHealth {
    # rtc_bridge 由 pythonw 承载属正常（审计 A10：不做进程名校验，只查 status ok）
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:19093/health" -TimeoutSec 3
        return ($r.status -eq "ok")
    } catch { return $false }
}

function Test-BackendHealth {
    # 审计 A10 修复（2026-08-21）：backend 健康 = /health 200 且监听进程是 jax-backend.exe。
    # 事故机制：临时 python 占 :8000 且 /health 200 → 看门狗误判健康不拉起，服务实际不可用。
    # 现在核对进程身份（Test-PortOwner 见 lib-common.ps1）；身份查不到（$null）时保守视为
    # 不健康 → 走拉起流程（jax-services.ps1 内部有同样的身份校验兜底，不会盲杀误占进程）。
    if (-not (Test-Health "https://127.0.0.1:8000/health")) { return $false }
    $owner = Test-PortOwner -Port 8000 -ExpectedProcName "jax-backend.exe"
    if ($owner.Match -eq $true) { return $true }
    if ($owner.Match -eq $false) {
        Write-WatchLog "[backend] 端口被非预期进程占用: $($owner.ProcName) PID=$($owner.Pid)（期望 jax-backend.exe）"
    }
    return $false
}

# ---------------- 主流程：只对异常服务动作，静默成功 ----------------
# 桌面 sidecar 的实例、运行时指针和完整性校验仅由 Tauri SidecarSupervisor 管理。
# 看门狗不得直接启动 Electron，避免绕过生产运行时与单实例保证。
foreach ($svc in @("model","backend","relay","rtc-bridge")) {
    $healthy = switch ($svc) {
        "model"       { Test-Health "http://127.0.0.1:19080/health" }
        "backend"     { Test-BackendHealth }
        "relay"       { Test-RelayAlive }
        "rtc-bridge"  { Test-BridgeHealth }
    }
    if ($healthy) { continue }
    Start-One $svc
}
exit 0
