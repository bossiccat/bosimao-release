# ============================================================
# lib-common.ps1 — jax 服务脚本共享函数
# 由 jax-services.ps1 / jax-watchdog.ps1 / start-all.ps1 dot-source
# 2026-08-21 抽取（审计 A2/A11）：此前 Test-Health / Get-RelayProcesses 在
# jax-watchdog.ps1 与 jax-services.ps1 各有一份且已漂移（watchdog 的 relay 过滤器
# 缺 pythonw.exe；jax-services 的 Test-Health 不支持自签 https）→ 统一为单一实现。
# 说明：本文件必须保持 UTF-8 with BOM（PS 5.1 中文解析依赖 BOM）
# ============================================================

function Merge-DuplicateProxyEnv {
    # 2026-09-01 P1：WorkBuddy 等宿主进程会向子进程同时注入大小写两个变体的
    # 代理变量（HTTPS_PROXY 与 https_proxy 同存）。Windows 环境变量名不区分
    # 大小写，但 PS 5.1 Start-Process 构建子进程环境字典时按区分大小写的
    # Dictionary 逐条 Add → 「已添加项。字典中的关键字:HTTPS_PROXY」
    # ArgumentException，服务拉起直接失败（relay 首例，实测复现）。
    # 修法：每个 Start-Process 前折叠 process 级代理变量——按规范名取值后
    # 循环删除（实测 SetEnvironmentVariable(name, $null) 每次只删一个变体），
    # 再以规范名单条重设。空值在 Windows 上删除即等于重设语义（'' = 删除），
    # 故空值只删不重设。仅影响当前进程环境块，不改 User/Machine 级。
    foreach ($name in @('HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY')) {
        $value = [System.Environment]::GetEnvironmentVariable($name, 'Process')
        $guard = 0
        while ($null -ne [System.Environment]::GetEnvironmentVariable($name, 'Process') -and $guard -lt 5) {
            [System.Environment]::SetEnvironmentVariable($name, $null, 'Process')
            $guard++
        }
        if ($null -ne $value -and $value -ne '') {
            [System.Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
    }
}

function Test-Health([string]$Url, [int]$TimeoutSec = 3) {
    # backend :8000 是自签 https 端口（用 http 探测必失败）——本地健康检查按端口回退 TLS 免验
    # 2026-08-22 修复（弹窗事故根因之一）：HTTP 200 判健康后，若失败仅在"超时/连接重置"时
    # 才走 TcpClient 端口兜底；"空回复/协议错误"（TCP 可连但应用卡死）必须判不健康。
    # 原实现对 https 端口一律 TCP 兜底 → backend 事件循环卡死时端口仍监听 → 误判健康
    # → 不拉起也不清理 → 下一轮巡检再拉 → PyInstaller 引导窗反复可见（用户看到的弹窗）。
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        return ($r.StatusCode -eq 200)
    } catch [System.Net.WebException] {
        $resp = $_.Exception.Response
        # 真实 HTTP 200 但 PS 因自签证书抛错 → 健康（保留原逻辑）
        if ($resp -and [int]$resp.StatusCode -eq 200) { return $true }
        # 连接被拒绝 = 端口没人听 = 明确不健康（不需要也不应该 TCP 兜底）
        if ($_.Exception.Status -eq "ConnectFailure") { return $false }
        # 超时：端口可能被占用但应用无响应——再给一次 TCP 探测区分"完全挂"与"慢"：
        # TCP 通 = 进程还在（可能慢启动），保守判健康等下一轮；TCP 不通 = 挂了
        if ($Url -match '^https?://127\.0\.0\.1:(\d+)') {
            $port = [int]$Matches[1]
            try {
                $tcp = New-Object System.Net.Sockets.TcpClient
                $tcp.Connect("127.0.0.1", $port)
                $tcp.Close()
                return $true
            } catch { return $false }
        }
        return $false
    } catch {
        # PS7 / 其他异常路径：空回复（The server returned an empty response）等 → 不健康
        return $false
    }
}

$JaxProjectRoot = Split-Path -Parent $PSScriptRoot
$JaxPythonPaths = @(
    (Join-Path $JaxProjectRoot ".venv\Scripts\python.exe"),
    (Join-Path $JaxProjectRoot ".venv\Scripts\pythonw.exe")
) | ForEach-Object { [System.IO.Path]::GetFullPath($_) }
$JaxRelayModule = "backend.relay.relay_client"

function Get-RelayProcesses {
    # 进程名不足以证明项目归属：其他工作区也可能运行名为 relay_client 的 Python。
    # 同时约束解释器绝对路径和完整模块入口，停止逻辑才不会误杀外部进程。
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            if (-not $_.ExecutablePath -or $JaxPythonPaths -notcontains ([System.IO.Path]::GetFullPath($_.ExecutablePath))) {
                return $false
            }
            return ($_.CommandLine -match '(?i)(^|[\\s"''])-m[\\s"'']+backend\.relay\.relay_client([\\s"'']|$)')
        }
}

function Get-RelayTopLevel {
    # pythonw launcher -> pythonw worker is one relay instance. Only processes
    # whose parent is outside the matched relay set count as independent instances.
    $all = @(Get-RelayProcesses)
    $allIds = @($all | ForEach-Object { [int]$_.ProcessId })
    return @($all | Where-Object { $allIds -notcontains [int]$_.ParentProcessId })
}

function Stop-AllRelay {
    # The caller may only reach this function after matching relay_client. Never
    # enumerate or stop general python processes.
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $relayProcesses = @(Get-RelayProcesses)
        if ($relayProcesses.Count -eq 0) { return $true }
        foreach ($relay in $relayProcesses) {
            Stop-Process -Id ([int]$relay.ProcessId) -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return (@(Get-RelayProcesses).Count -eq 0)
}

function Test-RelayProcessTree {
    # 一个顶层 relay_client 与其命令行可识别的子进程构成唯一受管实例。
    # 孤儿子进程、多个顶层实例和非 relay Python 进程都不能被误判为健康。
    $all = @(Get-RelayProcesses)
    if ($all.Count -eq 0) { return $false }
    $allIds = @($all | ForEach-Object { [int]$_.ProcessId })
    $topLevel = @($all | Where-Object { $allIds -notcontains [int]$_.ParentProcessId })
    if ($topLevel.Count -ne 1) { return $false }

    $rootId = [int]$topLevel[0].ProcessId
    $knownIds = @($rootId)
    do {
        $before = $knownIds.Count
        $knownIds += @($all | Where-Object {
            $knownIds -contains [int]$_.ParentProcessId
        } | ForEach-Object { [int]$_.ProcessId })
        $knownIds = @($knownIds | Select-Object -Unique)
    } while ($knownIds.Count -gt $before)

    return (@($all | Where-Object { $knownIds -notcontains [int]$_.ProcessId }).Count -eq 0)
}

# ============================================================
# Test-PortOwner（审计 A10 修复，2026-08-21）：核对端口监听进程的身份
# 事故机制：临时 WorkBuddy python 进程（无 numpy）占用 :8000，/health 仍返回 200，
# jax-services.ps1 幂等逻辑放行 → 模型服务实际不可用但被判定健康。
# 根因：健康检查只看 HTTP 200，不核对"监听端口的进程是谁"。
#
# 用法：
#   $r = Test-PortOwner -Port 8000 -ExpectedProcName "jax-backend.exe"
#   $r.Match  → $true/$false/$null（$null=无法判定：端口未监听或查不到进程）
#   $r.ProcName / $r.Pid / $r.Message
#
# 注意：relay 与 rtc-bridge 本来就是 pythonw/python 承载，不要对它们做进程名校验
#（期望名不匹配即误报）；本函数仅用于有唯一品牌化进程名的服务（backend）。
# ============================================================
function Test-PortOwner([int]$Port, [string]$ExpectedProcName) {
    $result = @{ Match = $null; ProcName = ""; Pid = $null; Message = "" }
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) {
        $result.Message = "端口 $Port 无监听进程"
        return $result
    }
    $procId = [int]$conn.OwningProcess
    $result.Pid = $procId
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    if (-not $proc) {
        $result.Message = "端口 $Port 监听 PID=$procId 但进程信息不可查（可能刚退出）"
        return $result
    }
    $result.ProcName = $proc.Name
    if ($proc.Name -like $ExpectedProcName) {
        $result.Match = $true
        $result.Message = "端口 $Port 由 $($proc.Name) (PID=$procId) 监听，符合预期"
    } else {
        $result.Match = $false
        $result.Message = "端口 $Port 由 $($proc.Name) (PID=$procId) 监听，非预期的 $ExpectedProcName"
    }
    return $result
}

# PyInstaller onefile 进程树：Start-Process 返回 bootloader PID，真正监听端口的
# jax-backend.exe 可能是其子 PID。服务唯一性按命令行+端口归属判断，不能要求
# Start-Process 返回 PID == 监听 PID；停止时必须收敛父子两层，避免留下孤儿。
function Get-BackendProcesses {
    Get-CimInstance Win32_Process -Filter "Name='jax-backend.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match '--port\s+8000' }
}
function Stop-BackendProcesses {
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $procs = @(Get-BackendProcesses)
        foreach ($p in $procs) { Stop-Process -Id ([int]$p.ProcessId) -Force -ErrorAction SilentlyContinue }
        if (@(Get-BackendProcesses).Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return (@(Get-BackendProcesses).Count -eq 0)
}
