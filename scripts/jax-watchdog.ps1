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

# ---------------- 共享函数（单一实现，消除双份定义漂移） ----------------
# 2026-08-21（审计 A11/A2）：Test-Health（含自签 https 兜底）与 Get-RelayProcesses
# （python.exe OR pythonw.exe）统一抽取到 lib-common.ps1，与 jax-services.ps1 共用。
. (Join-Path $PSScriptRoot "lib-common.ps1")

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
        "relay"       { @(Get-RelayProcesses).Count -gt 0 }
        "rtc-bridge"  { Test-BridgeHealth }
        "sidecar"     { Test-SidecarHealthy }
    }
    if ($healthyAfter) {
        Write-WatchLog "[$svc] 拉起成功（复查健康）"
    } else {
        # $out 是 & 调用的输出数组（含 ErrorRecord），join 后写入日志供排查
        Write-WatchLog "[$svc] 拉起后复查仍不健康（已计防风暴第 $($count + 1) 次）：$outText"
    }
}

# ---------------- rtc-bridge / sidecar 健康（2026-08-21 补：桌面常驻闭环） ----------------
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

function Test-SidecarConnected {
    # 仅记录用途：sidecar_connected 语义是"进房+bridge.startSession 后才 true"
    # （bridge /health 接口），空闲轮询态本来就是 false → 不能作健康判据
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:19093/health" -TimeoutSec 3
        return ($r.sidecar_connected -eq $true)
    } catch { return $false }
}

function Test-SidecarHealthy {
    # 审计 A1 修复：sidecar 健康 = electron 进程存在（空闲是正常态，不是病态）。
    # 原实现用 sidecar_connected 判健康 → 每 5 分钟误杀健康空闲 sidecar。
    return ($null -ne (Get-Process -Name electron -ErrorAction SilentlyContinue))
}

function Start-Sidecar {
    # sidecar 不在 jax-services.ps1 里（Electron 启动参数复杂），单独拉起
    $SidecarDir = Join-Path $Root "sidecar"
    $Electron   = Join-Path $SidecarDir "node_modules\electron\dist\electron.exe"
    if (-not (Test-Path $Electron)) { Write-WatchLog "[sidecar] electron.exe 不存在: $Electron"; return }
    # 环境净化 + 凭证（.env 已由调用方 Load-Env；此处兜底读取）
    if (-not $env:VOICE_SIDECAR_CREDENTIAL) {
        $envFile = Join-Path $Root ".env"
        if (Test-Path $envFile) {
            $m = (Get-Content $envFile | Select-String "^VOICE_SIDECAR_CREDENTIAL=(.+)$" | Select-Object -First 1)
            if ($m) { $env:VOICE_SIDECAR_CREDENTIAL = $m.Matches[0].Groups[1].Value.Trim() }
        }
    }
    $env:NODE_EXTRA_CA_CERTS = Join-Path $SidecarDir "ca-tmp.crt"
    Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
    if ($env:NODE_OPTIONS) {
        $kept = ($env:NODE_OPTIONS -split '\s+') | Where-Object { $_ -and $_ -notmatch '--use-system-ca|--use-openssl-ca|--require' }
        if ($kept) { $env:NODE_OPTIONS = $kept -join ' ' } else { Remove-Item Env:NODE_OPTIONS -ErrorAction SilentlyContinue }
    }
    $SignUrl = "https://jinhong-d2g55ycl591208475-1436773060.ap-shanghai.app.tcloudbase.com"
    $ts = Get-Date -Format "yyyyMMdd-HHmmss"
    Start-Process -FilePath $Electron -ArgumentList ".","--in-process-gpu","--role=sidecar","--sign-url=$SignUrl","--bridge-url=ws://127.0.0.1:19092","--hold=86400" -WorkingDirectory $SidecarDir -WindowStyle Hidden
    Write-WatchLog "[sidecar] 已拉起（ts=$ts）"
}

# ---------------- 主流程：只对异常服务动作，静默成功 ----------------
# 2026-08-21：补 rtc-bridge + sidecar（用户需求：桌面端一直常驻、手机随时可连）
foreach ($svc in @("model","backend","relay","rtc-bridge","sidecar")) {
    # sidecar 依赖 rtc-bridge：bridge 不健康时先拉 bridge，sidecar 本轮跳过
    if ($svc -eq "sidecar" -and -not (Test-BridgeHealth)) { continue }
    $healthy = switch ($svc) {
        "model"       { Test-Health "http://127.0.0.1:19080/health" }
        "backend"     { Test-BackendHealth }
        "relay"       { Test-RelayAlive }
        "rtc-bridge"  { Test-BridgeHealth }
        "sidecar"     { Test-SidecarHealthy }
    }
    if ($healthy) {
        # sidecar_connected 只作记录不作判据（空闲态为 false 属正常）
        if ($svc -eq "sidecar" -and (Test-SidecarConnected)) {
            Write-WatchLog "[sidecar] 会话已连接（sidecar_connected=true）"
        }
        continue
    }
    if ($svc -eq "sidecar") {
        # 审计 A1 修复：sidecar 不健康 = electron 进程不存在 → 普通拉起即可。
        # 原实现"进程在但未连 bridge 就杀掉重启"会在空闲态误杀健康 sidecar，已移除。
        Start-Sidecar
    } else {
        Start-One $svc
    }
}
exit 0
