# ============================================================
# jax-services.ps1 — 贾克斯桌面端三件套统一服务管理（加固）
# 三件套：model(:19080 llama-server) / backend(:8000 uvicorn) / relay_client(公网中继桥接)
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts/jax-services.ps1 status
#   powershell -ExecutionPolicy Bypass -File scripts/jax-services.ps1 start            # 全部
#   powershell -ExecutionPolicy Bypass -File scripts/jax-services.ps1 start relay      # 单服务
#   powershell -ExecutionPolicy Bypass -File scripts/jax-services.ps1 stop backend
#   powershell -ExecutionPolicy Bypass -File scripts/jax-services.ps1 restart
# 特性：PID 文件管理(data/pids/*.pid) / 幂等(健康即跳过) / 启动前清理旧残留(按端口定位不盲杀)
# 说明：本文件必须保持 UTF-8 with BOM（PS 5.1 中文解析依赖 BOM）
# ============================================================
param(
    [ValidateSet("start","stop","restart","status")]
    [string]$Action = "status",
    [ValidateSet("model","backend","relay","all")]
    [string]$Service = "all"
)
$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent $PSScriptRoot
$PidDir  = Join-Path $Root "data\pids"
$LogDir  = Join-Path $Root "logs"
$Py      = Join-Path $Root ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force -Path $PidDir, $LogDir | Out-Null

# ---------------- 基础工具函数 ----------------
function Test-PortListen([int]$Port) {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $c)
}
function Get-PortPid([int]$Port) {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return [int]$c.OwningProcess }
    return $null
}
function Get-PortProcCommandLine([int]$Port) {
    $procId = Get-PortPid $Port
    if (-not $procId) { return "" }
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    if ($p) { return $p.CommandLine }
    return ""
}
function Test-Health([string]$Url, [int]$TimeoutSec = 3) {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}
function Get-PidFile([string]$Name) {
    $f = Join-Path $PidDir "$Name.pid"
    if (Test-Path $f) {
        $v = (Get-Content $f -Raw -ErrorAction SilentlyContinue).Trim()
        if ($v -match '^\d+$') { return [int]$v }
    }
    return $null
}
function Set-PidFile([string]$Name, [int]$ProcId) {
    Set-Content -Path (Join-Path $PidDir "$Name.pid") -Value $ProcId -Encoding ASCII
}
function Clear-PidFile([string]$Name) {
    $f = Join-Path $PidDir "$Name.pid"
    if (Test-Path $f) { Remove-Item $f -Force -ErrorAction SilentlyContinue }
}
function Test-ProcessAlive([int]$ProcId) {
    if (-not $ProcId) { return $false }
    return ($null -ne (Get-Process -Id $ProcId -ErrorAction SilentlyContinue))
}
function Load-Env {
    # 将 .env 注入进程环境（RELAY_TOKEN / RELAY_E2EE_KEY 等）
    $envFile = Join-Path $Root ".env"
    if (Test-Path $envFile) {
        Get-Content $envFile | ForEach-Object {
            if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
                [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
            }
        }
    }
}
function Get-RelayProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "relay_client" }
}
function Get-RelayTopLevel {
    # 顶层 relay 实例：父进程不是 relay_client 的进程（排除 .venv 重定向器拉起的子 python）
    $all = @(Get-RelayProcesses)
    $allIds = @($all | ForEach-Object { [int]$_.ProcessId })
    return @($all | Where-Object { $allIds -notcontains [int]$_.ParentProcessId })
}
function Stop-AllRelay {
    # 杀掉 relay_client 全部进程（含 .venv 启动器 + 其子 python），先杀顶层再补漏
    $rs = @(Get-RelayProcesses)
    foreach ($r in $rs) { Stop-Process -Id $r.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 800
    $left = @(Get-RelayProcesses)
    foreach ($r in $left) { Stop-Process -Id $r.ProcessId -Force -ErrorAction SilentlyContinue }
}
function Invoke-SvcStart([string]$Name) {
    switch ($Name) {
        "model"   { return Start-ModelService }
        "backend" { return Start-BackendService }
        "relay"   { return Start-RelayService }
    }
}

# ---------------- 模型服务 :19080 ----------------
function Start-ModelService {
    $ServerBin = "C:\Users\Administrator\AppData\Local\Comni\_internal\resources\build\bin\Release\llama-server.exe"
    $Model = "D:\models\MiniCPM-o-4_5-gguf\MiniCPM-o-4_5-Q4_K_M.gguf"
    $Port = 19080
    $Log = Join-Path $LogDir "llama-server-$Port.log"
    if (-not (Test-Path $ServerBin)) { Write-Host "[model][x] 引擎不存在: $ServerBin"; return $false }
    if (-not (Test-Path $Model))     { Write-Host "[model][x] 模型不存在: $Model";     return $false }
    # 幂等：已健康 → 跳过（采纳现有进程 PID，保持 PID 文件一致）
    if (Test-PortListen $Port -and (Test-Health "http://127.0.0.1:$Port/health")) {
        $cur = Get-PortPid $Port
        if ($cur) { Set-PidFile "model" $cur }
        Write-Host "[model][ok] 已在运行（幂等跳过，PID=$cur）"
        return $true
    }
    # 启动前清理旧 PID 残留：PID 文件指向的进程已死 → 清文件
    $oldProcId = Get-PidFile "model"
    if ($oldProcId -and -not (Test-ProcessAlive $oldProcId)) { Clear-PidFile "model" }
    # 端口被占用但健康检查未过（可能是残留/半死进程）→ 不盲杀，报告并跳过
    if (Test-PortListen $Port) {
        $cmd = Get-PortProcCommandLine $Port
        Write-Host "[model][!] 端口 $Port 被占用但 /health 未通过，跳过启动（不盲杀）"
        Write-Host "         占用进程: $cmd"
        return $false
    }
    # 主模型层对齐 Comni GUI（cpp_backend.py）：--device CUDA0 + --split-mode none（单卡）
    # 注意：token2wav/audio 子模型由 omni_init 请求体控制（token2wav_device=gpu:0），不在启动参数
    $env:LLAMA_ARG_DEVICE = "CUDA0"
    $args = @("--host","127.0.0.1","--port","$Port","--model",$Model,"-ngl","99","--ctx-size","8192","--device","CUDA0","--split-mode","none")
    Write-Host "[model] 启动 $ServerBin ..."
    $p = Start-Process -FilePath $ServerBin -ArgumentList $args `
        -RedirectStandardOutput $Log -RedirectStandardError "$Log.err" -WindowStyle Hidden -PassThru
    Set-PidFile "model" $p.Id
    Write-Host "[model] PID=$($p.Id) 等待 /health（模型加载约 20s-5min）..."
    $deadline = (Get-Date).AddMinutes(5)
    while ((Get-Date) -lt $deadline) {
        if (Test-Health "http://127.0.0.1:$Port/health") { Write-Host "[model][ok] 就绪"; return $true }
        Start-Sleep -Seconds 3
    }
    Write-Host "[model][x] 5 分钟内未就绪，查看 $Log"
    return $false
}

# ---------------- 后端服务 :8000 ----------------
function Start-BackendService {
    $Port = 8000
    $Log = Join-Path $LogDir "backend.log"
    if (Test-PortListen $Port) {
        # 端口已监听：健康则幂等跳过（采纳 PID）；不健康则报告（不盲杀）
        if (Test-Health "http://127.0.0.1:$Port/health") {
            $cur = Get-PortPid $Port
            if ($cur) { Set-PidFile "backend" $cur }
            Write-Host "[backend][ok] 已在运行（幂等跳过，PID=$cur）"
            return $true
        }
        $cmd = Get-PortProcCommandLine $Port
        Write-Host "[backend][!] 端口 $Port 被占用但 /health 未通过，跳过启动（不盲杀）"
        Write-Host "           占用进程: $cmd"
        return $false
    }
    $oldProcId = Get-PidFile "backend"
    if ($oldProcId -and -not (Test-ProcessAlive $oldProcId)) { Clear-PidFile "backend" }
    Write-Host "[backend] 启动 $Py -m uvicorn app.main:app --port $Port (cwd=backend)"
    $p = Start-Process -FilePath $Py -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","$Port" `
        -WorkingDirectory (Join-Path $Root "backend") `
        -RedirectStandardOutput $Log -RedirectStandardError "$Log.err" -WindowStyle Hidden -PassThru
    Set-PidFile "backend" $p.Id
    Write-Host "[backend] PID=$($p.Id) 等待 /health（最多 90s）..."
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        if (Test-Health "http://127.0.0.1:$Port/health") { Write-Host "[backend][ok] 就绪"; return $true }
        Start-Sleep -Seconds 2
    }
    Write-Host "[backend][x] 90s 内未就绪，查看 $Log"
    return $false
}

# ---------------- relay_client（公网中继桥接） ----------------
function Start-RelayService {
    Load-Env
    $relayUrl = "wss://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com/relay/ws"
    $gwUrl    = "ws://127.0.0.1:8000/ws/voice"
    $pairCode = "JAX2026"
    $token    = $env:RELAY_TOKEN
    $e2eeKey  = $env:RELAY_E2EE_KEY
    if (-not $token)   { Write-Host "[relay][!] RELAY_TOKEN 为空（中继将拒绝配对）" }
    if (-not $e2eeKey) { Write-Host "[relay][!] RELAY_E2EE_KEY 为空（明文模式，手机需匹配）" }
    # 幂等：已有顶层 relay_client 实例 → 跳过（采纳首个 PID；多实例残留时报告）
    $existing = @(Get-RelayTopLevel)
    if ($existing.Count -gt 0) {
        $ids = ($existing | ForEach-Object { $_.ProcessId }) -join ","
        Set-PidFile "relay" $existing[0].ProcessId
        Write-Host "[relay][ok] 已有 relay_client 运行（实例 PID $ids），幂等跳过"
        if ($existing.Count -gt 1) {
            Write-Host "[relay][!] 检测到 $($existing.Count) 个 relay_client 实例残留（互相抢占配对码），建议 restart relay 清理"
        }
        return $true
    }
    $oldProcId = Get-PidFile "relay"
    if ($oldProcId -and -not (Test-ProcessAlive $oldProcId)) { Clear-PidFile "relay" }
    $log = Join-Path $LogDir "relay_client.log"
    $relayArgs = @("-m","backend.relay.relay_client",
        "--relay", $relayUrl, "--gateway", $gwUrl,
        "--pairing-code", $pairCode, "--token", $token, "--e2ee-key", $e2eeKey)
    Write-Host "[relay] 启动 $Py $($relayArgs -join ' ')"
    $p = Start-Process -FilePath $Py -ArgumentList $relayArgs -WorkingDirectory $Root `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden -PassThru
    Set-PidFile "relay" $p.Id
    Write-Host "[relay] PID=$($p.Id) 等待注册/配对确认（最多 25s）..."
    $deadline = (Get-Date).AddSeconds(25)
    $ok = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if (Test-Path $log) {
            if (Select-String -Path $log -Pattern "relay registered|relay paired|voice gateway ready" -Quiet -ErrorAction SilentlyContinue) { $ok = $true; break }
        }
    }
    if ($ok) { Write-Host "[relay][ok] 已注册中继（等待手机对端配对）"; return $true }
    Write-Host "[relay][!] 25s 内未确认配对（手机未接入时属正常），查看 $log"
    return $true
}

# ---------------- 停止（按 PID 文件 + 确认退出；缺失时按端口/命令行定位不盲杀） ----------------
function Stop-ServiceByName([string]$Name) {
    $procId = Get-PidFile $Name
    # relay：统一杀全部（.venv 启动器 + 子 python），避免孤儿
    if ($Name -eq "relay") {
        $rs = @(Get-RelayProcesses)
        if ($rs.Count -gt 0) {
            Stop-AllRelay
            Write-Host "[relay] 已停止全部 relay_client 进程（$($rs.Count) 个，含启动器+子进程）"
            Clear-PidFile "relay"; return $true
        }
        Write-Host "[relay][ok] 未运行"
        Clear-PidFile "relay"; return $true
    }
    if ($procId -and (Test-ProcessAlive $procId)) {
        Write-Host "[$Name] 停止 PID=$procId"
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $deadline -and (Test-ProcessAlive $procId)) { Start-Sleep -Milliseconds 500 }
        if (Test-ProcessAlive $procId) { Write-Host "[$Name][x] 进程未退出"; return $false }
        Clear-PidFile $Name
        Write-Host "[$Name][ok] 已停止"
        return $true
    }
    # PID 文件缺失/失效 → 按端口 + 命令行白名单定位（不盲杀）
    if ($Name -eq "model") {
        $procId = Get-PortPid 19080
        if ($procId) {
            $cmd = Get-PortProcCommandLine 19080
            if ($cmd -match "llama-server") {
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                Write-Host "[model] 按端口 19080 定位并停止 PID=$procId（命令行含 llama-server）"
                Clear-PidFile "model"; return $true
            }
            Write-Host "[model][!] 端口 19080 被非 llama-server 进程占用（PID $procId），不盲杀"; return $false
        }
    } elseif ($Name -eq "backend") {
        $procId = Get-PortPid 8000
        if ($procId) {
            $cmd = Get-PortProcCommandLine 8000
            if ($cmd -match "uvicorn") {
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                Write-Host "[backend] 按端口 8000 定位并停止 PID=$procId（命令行含 uvicorn）"
                Clear-PidFile "backend"; return $true
            }
            Write-Host "[backend][!] 端口 8000 被非 uvicorn 进程占用（PID $procId），不盲杀"; return $false
        }
    }
    Write-Host "[$Name][ok] 未运行"
    return $true
}

# ---------------- 状态 ----------------
function Show-Status {
    Write-Host ""
    Write-Host "=========== 贾克斯三件套状态 ==========="
    # model
    $mProcId = Get-PidFile "model"; $mAlive = Test-ProcessAlive $mProcId
    $mPort = Test-PortListen 19080; $mHealth = Test-Health "http://127.0.0.1:19080/health"
    $mState = if ($mHealth) { "OK" } elseif ($mPort) { "PORT-NOHEALTH" } elseif ($mAlive) { "PID-ALIVE" } else { "DOWN" }
    Write-Host ("[model]   :19080    {0}{1}" -f $mState, $(if ($mProcId) { "  PID=$mProcId" } else { "" }))
    # backend
    $bProcId = Get-PidFile "backend"; $bAlive = Test-ProcessAlive $bProcId
    $bPort = Test-PortListen 8000; $bHealth = Test-Health "http://127.0.0.1:8000/health"
    $bState = if ($bHealth) { "OK" } elseif ($bPort) { "PORT-NOHEALTH" } elseif ($bAlive) { "PID-ALIVE" } else { "DOWN" }
    Write-Host ("[backend] :8000     {0}{1}" -f $bState, $(if ($bProcId) { "  PID=$bProcId" } else { "" }))
    # relay
    $rProcId = Get-PidFile "relay"; $rAlive = Test-ProcessAlive $rProcId
    $rTop = @(Get-RelayTopLevel)
    $rCount = $rTop.Count
    if ($rCount -gt 0) { $rState = "RUNNING(x$rCount)" } elseif ($rAlive) { $rState = "PID-ALIVE" } else { $rState = "DOWN" }
    Write-Host ("[relay]   wss-relay {0}{1}" -f $rState, $(if ($rProcId) { "  PID=$rProcId" } else { "" }))
    if ($rCount -gt 1) { Write-Host "[relay][!] 检测到多个 relay_client 实例残留（可能互相抢占配对码），建议 restart relay" }
    Write-Host "========================================"
}

# ---------------- 主流程 ----------------
$svcs = @()
if ($Service -eq "all") { $svcs = @("model","backend","relay") } else { $svcs = @($Service) }

switch ($Action) {
    "start" {
        foreach ($s in $svcs) { Invoke-SvcStart $s | Out-Null }
    }
    "stop" {
        foreach ($s in $svcs) { Stop-ServiceByName $s | Out-Null }
    }
    "restart" {
        foreach ($s in $svcs) {
            Stop-ServiceByName $s | Out-Null
            Start-Sleep -Seconds 1
            Invoke-SvcStart $s | Out-Null
        }
    }
    "status" {
        Show-Status
    }
}
