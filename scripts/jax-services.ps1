# ============================================================
# jax-services.ps1 — 贾克斯桌面端三件套统一服务管理（加固）
# 三件套：model(:19080 jax-model) / backend(:8000 jax-backend) / relay_client(公网中继桥接)
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
    [ValidateSet("model","backend","relay","rtc-bridge","all")]
    [string]$Service = "all"
)
$ErrorActionPreference = "Stop"
# 跨 jax-services.ps1 / start-all.ps1 的单实例闸门：避免两个入口同时通过“端口未监听”检查后各自 spawn。
$ServiceMutex = New-Object System.Threading.Mutex($false, "Global\JaxServicesStartStop")
if (-not $ServiceMutex.WaitOne(0)) {
    Write-Host "[services][busy] 另一份服务启停操作正在进行，拒绝并发执行"
    exit 2
}
$Root    = Split-Path -Parent $PSScriptRoot
$PidDir  = Join-Path $Root "data\pids"
$LogDir  = Join-Path $Root "logs"
$Py      = Join-Path $Root ".venv\Scripts\python.exe"
$PyW     = Join-Path $Root ".venv\Scripts\pythonw.exe"
# 阶段 D 品牌化：后端/模型进程不再以裸 python.exe / llama-server.exe 常驻，
# 改为 jax-backend.exe / jax-model.exe（任务管理器显示品牌化进程名，消除杀毒误报面）。
$BackendExe = Join-Path $Root "jax-backend.exe"
New-Item -ItemType Directory -Force -Path $PidDir, $LogDir | Out-Null

# ---------------- 共享函数（单一实现，消除双份定义漂移） ----------------
# 2026-08-21（审计 A2/A11）：Test-Health（含自签 https 兜底）与 Get-RelayProcesses
# 统一抽取到 lib-common.ps1，与 jax-watchdog.ps1 共用，防止两脚本再次漂移。
. (Join-Path $PSScriptRoot "lib-common.ps1")

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
# Test-Health：见 lib-common.ps1（单一实现，含自签 https TcpClient 兜底）
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
    # -Encoding UTF8 必须显式声明：.env 为 UTF-8 无 BOM，PS5.1 默认按系统 ANSI(GBK)
    # 解码会把含中文的绝对路径（SSL_CERT_FILE / RTC_BRIDGE_CONTROL_PLANE_* 等）
    # mojibake 注入子进程（2026-09-01 AC-1 P1 契约 test_loadenv_utf8_no_mojibake_contract）。
    $envFile = Join-Path $Root ".env"
    if (Test-Path $envFile) {
        Get-Content $envFile -Encoding UTF8 | ForEach-Object {
            if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
                [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
            }
        }
    }
}
function Invoke-OwnerCredentialProvision {
    # owner credential 首启 provision（ADR-022）：backend 启动前调用，非零退出即中止（fail-closed）
    $exe = $null
    $release = Join-Path $Root "pet-ui\src-tauri\target\release\provision_owner_credential.exe"
    $debug   = Join-Path $Root "pet-ui\src-tauri\target\debug\provision_owner_credential.exe"
    if (Test-Path $release) { $exe = $release }
    elseif (Test-Path $debug) { $exe = $debug }
    else {
        Write-Host "[owner-credential][!] provisioner 未编译；请先: cd pet-ui/src-tauri; cargo build --bin provision_owner_credential"
        return $false
    }
    # GUI 子系统二进制——不带 -WindowStyle（避免冲突），-Wait 等退出码
    Merge-DuplicateProxyEnv
    $p = Start-Process -FilePath $exe -Wait -PassThru
    if ($p.ExitCode -ne 0) {
        Write-Host "[owner-credential][x] provision 失败（退出码 $($p.ExitCode)），中止启动（fail-closed）"
        return $false
    }
    Write-Host "[owner-credential][ok] owner credential 已就绪"
    return $true
}

function Invoke-SidecarCredentialProvision {
    # O-018 切片 3：sidecar credential「launcher → CM → .env 同值同步」编排（2026-09-03 设计稿落地）
    # 语义：CM 与 .env 已同值 → 幂等跳过；否则跑真 launcher（CSPRNG）→ 读 CM active →
    #       备份 + 原子写 .env；若 backend 已在运行且值变化 → 温和重启（下方主流程随后重拉）。
    # fail-closed：任何一步失败返回 $false，中止启动（与 owner credential 同级）。
    $launcher = $null
    $release = Join-Path $Root "pet-ui\src-tauri\target\release\provision_sidecar_credential_launcher.exe"
    $debug   = Join-Path $Root "pet-ui\src-tauri\target\debug\provision_sidecar_credential_launcher.exe"
    if (Test-Path $release) { $launcher = $release }
    elseif (Test-Path $debug) { $launcher = $debug }
    else {
        Write-Host "[sidecar-credential][!] launcher 未编译；请先: cd pet-ui/src-tauri; cargo build --bin provision_sidecar_credential_launcher [SIDECARPROV_001]"
        return $false
    }

    $envPath = Join-Path $Root ".env"
    $cmTarget = "JaxPet/com.jax.pet/voice-sidecar/v1"
    $envValue = Get-DotEnvValue $envPath "VOICE_SIDECAR_CREDENTIAL"
    $cmValue  = Get-CredentialBlobFromCM $cmTarget

    # 幂等：两侧都有值且同值 → 不跑 launcher、不动盘、不重启
    if ($cmValue -and $envValue -and ($cmValue -eq $envValue)) {
        Write-Host "[sidecar-credential][ok] CM 与 .env 已同值，幂等跳过"
        return $true
    }

    # 需要供给：跑真 launcher（GUI 子系统，-Wait 等退出码；每次生成新值 = 轮换语义）
    Merge-DuplicateProxyEnv
    $p = Start-Process -FilePath $launcher -Wait -PassThru
    if ($p.ExitCode -ne 0) {
        Write-Host "[sidecar-credential][x] launcher 失败（退出码 $($p.ExitCode)），中止启动（fail-closed）[SIDECARPROV_002]"
        return $false
    }
    $cmValue = Get-CredentialBlobFromCM $cmTarget
    if (-not $cmValue) {
        Write-Host "[sidecar-credential][x] provision 后 CM active 不可读，中止启动（fail-closed）[SIDECARPROV_003]"
        return $false
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hashPrefix = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($cmValue)))).Replace("-","").Substring(0,16).ToLower()
    if ($envValue -eq $cmValue) {
        Write-Host "[sidecar-credential][ok] 已同值（sha16=$hashPrefix），幂等跳过"
        return $true
    }
    if (-not (Set-DotEnvValue $envPath "VOICE_SIDECAR_CREDENTIAL" $cmValue)) {
        Write-Host "[sidecar-credential][x] .env 单键写入/校验失败，中止启动（fail-closed）[SIDECARPROV_004]"
        return $false
    }
    Write-Host "[sidecar-credential][ok] .env 已同步 CM 值（len=$($cmValue.Length)，sha16=$hashPrefix，备份已落）"
    # 若 backend 已在运行且健康，值变化必须温和重启才能生效；未在运行则交给下方正常启动路径
    if ((Test-PortListen 8000) -and (Test-Health "https://127.0.0.1:8000/health")) {
        Write-Host "[sidecar-credential][!] backend 在运行但凭证已变化，温和重启使新值生效"
        if (-not (Stop-BackendProcesses)) {
            Write-Host "[sidecar-credential][x] 旧 backend 进程树未能停止，中止（防风暴）[SIDECARPROV_005]"
            return $false
        }
        Clear-PidFile "backend"
    }
    return $true
}
# Get-RelayProcesses / Get-RelayTopLevel / Stop-AllRelay：见 lib-common.ps1。
function Invoke-SvcStart([string]$Name) {
    switch ($Name) {
        "model"      { return Start-ModelService }
        "backend"    { return Start-BackendService }
        "relay"      { return Start-RelayService }
        "rtc-bridge" { return Start-RtcBridgeService }
    }
}

# ---------------- 模型服务 :19080 ----------------
function Start-ModelService {
    # 阶段 D 品牌化：llama-server.exe 重命名为 jax-model.exe（签名留阶段 G）。
    # 注意：jax-model.exe 依赖同目录下的 CUDA/ggml/llama/omni DLL，必须与这些 DLL 保持同目录。
    $ServerBin = "C:\Users\Administrator\AppData\Local\Comni\_internal\resources\build\bin\Release\jax-model.exe"
    $Model = "D:\models\MiniCPM-o-4_5-gguf\MiniCPM-o-4_5-Q4_K_M.gguf"
    $Port = 19080
    $Log = Join-Path $LogDir "jax-model-$Port.log"
    if (-not (Test-Path $ServerBin)) { Write-Host "[model][x] 引擎不存在: $ServerBin"; return $false }
    if (-not (Test-Path $Model))     { Write-Host "[model][x] 模型不存在: $Model";     return $false }
    # 幂等：已健康 → 跳过（采纳现有进程 PID，保持 PID 文件一致）
    if (Test-PortListen $Port -and (Test-Health "http://127.0.0.1:$Port/health")) {
        # 审计 A10 同款机制加固：健康 200 后核对监听进程名（期望 jax-model.exe）
        $owner = Test-PortOwner -Port $Port -ExpectedProcName "jax-model.exe"
        if ($owner.Match -eq $false) {
            Write-Host "[model][!] 端口被非预期进程占用: $($owner.ProcName) PID=$($owner.Pid)（期望 jax-model.exe），不复用"
            return $false
        }
        $cur = Get-PortPid $Port
        if ($cur) { Set-PidFile "model" $cur }
        Write-Host "[model][ok] 已在运行（幂等跳过，PID=$cur，进程=$($owner.ProcName)）"
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
    $args = @("--host","127.0.0.1","--port","$Port","--model",$Model,"-ngl","99","--ctx-size","4096","--device","CUDA0","--split-mode","none")
    Write-Host "[model] 启动 $ServerBin ..."
    Merge-DuplicateProxyEnv
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
    # owner credential 首启 provision（ADR-022）：backend 启动前，失败即中止（fail-closed）
    if (-not (Invoke-OwnerCredentialProvision)) { return $false }
    # sidecar credential「launcher → CM → .env」同值编排（O-018 切片 3）：失败即中止（fail-closed）
    if (-not (Invoke-SidecarCredentialProvision)) { return $false }
    $backendProcs = @(Get-BackendProcesses)
    if (Test-PortListen $Port -or $backendProcs.Count -gt 0) {
        # 端口已监听或 onefile bootstrap 尚在：健康则幂等跳过；不健康则报告（不盲杀）
        # 审计 A2 修复：:8000 是 https 自签端口（uvicorn ssl_certfile=certs/server.crt），
        # 原 http:// 探测必失败（Invoke-WebRequest 对 https 端口发 http 会抛异常）→ 改 https
        if (Test-Health "https://127.0.0.1:$Port/health") {
            # 审计 A10 修复（2026-08-21）：健康 200 ≠ 期望进程在服务。
            # 事故：临时 python 进程占 :8000 且 /health 200 → 幂等放行 → 服务实际不可用。
            # 现在：健康通过后追加核对监听进程名（期望 jax-backend.exe），不匹配则
            # 警告 + 不复用（跳过幂等采纳，走下方"端口被占用"分支报告，不盲杀）。
            $owner = Test-PortOwner -Port $Port -ExpectedProcName "jax-backend.exe"
            if ($owner.Match -eq $true) {
                $allBackend = @(Get-BackendProcesses)
                if ($allBackend.Count -gt 2) {
                    Write-Host "[backend][!] 检测到重复 onefile 实例（进程数=$($allBackend.Count)，监听 PID=$($owner.Pid)），拒绝幂等放行；请执行 restart backend"
                    return $false
                }
                $cur = Get-PortPid $Port
                if ($cur) { Set-PidFile "backend" $cur }
                Write-Host "[backend][ok] 已在运行（幂等跳过，PID=$cur，进程=$($owner.ProcName)，onefile 进程数=$($allBackend.Count)）"
                return $true
            }
            if ($owner.Match -eq $false) {
                Write-Host "[backend][!] 端口被非预期进程占用: $($owner.ProcName) PID=$($owner.Pid)（期望 jax-backend.exe），不复用"
                Write-Host "[backend][!] /health 虽返回 200 但应答方非 jax-backend——可能是外来 python/临时进程劫持（numpy 事故同款机制）"
                Write-Host "[backend][!] 不自动清理（强杀有风险）；如确认可手工: Stop-Process -Id $($owner.Pid)，再重启本服务"
                return $false
            }
            # Match=$null：端口健康但进程身份查不到（竞态/权限）——退回旧行为（幂等跳过），仅提示
            $cur = Get-PortPid $Port
            if ($cur) { Set-PidFile "backend" $cur }
            Write-Host "[backend][ok] 已在运行（幂等跳过，PID=$cur；进程身份未能核对: $($owner.Message)）"
            return $true
        }
        $cmd = Get-PortProcCommandLine $Port
        # 2026-08-22 修复（弹窗事故根因之二）：backend 卡死（TCP 在、HTTP 空回复）时
        # 原逻辑"跳过启动不盲杀"→ 端口被僵尸永久占用 → watchdog 每 5min 拉新实例
        # → 新实例抢不到端口 → PyInstaller 引导窗反复弹出（用户看到的弹窗）。
        # 现在：占用者确认是 jax-backend.exe（我们自己的进程）且 unhealthy → 温和重启：
        # 杀掉全部 onefile 进程树 → 端口释放 → 顺序往下走正常启动。
        $ownerHanging = Test-PortOwner -Port $Port -ExpectedProcName "jax-backend.exe"
        if ($ownerHanging.Match -eq $true) {
            Write-Host "[backend][!] backend 卡死（端口在但 /health 不通过），执行温和重启（杀 jax-backend 进程树后重拉）"
            if (Stop-BackendProcesses) {
                Clear-PidFile "backend"
                Start-Sleep -Seconds 2   # 端口 TIME_WAIT 释放
            } else {
                Write-Host "[backend][x] 卡死进程树未能停止，放弃本轮（防风暴）"
                return $false
            }
        } else {
            Write-Host "[backend][!] 端口 $Port 被占用但 /health 未通过，跳过启动（不盲杀外来进程）"
            Write-Host "           占用进程: $cmd"
            return $false
        }
    }
    $oldProcId = Get-PidFile "backend"
    if ($oldProcId -and -not (Test-ProcessAlive $oldProcId)) { Clear-PidFile "backend" }
    if (-not (Test-Path $BackendExe)) { Write-Host "[backend][x] jax-backend.exe 不存在: $BackendExe（请先 cd backend/packaging 打包）"; return $false }
    Write-Host "[backend] 启动 $BackendExe --host 127.0.0.1 --port $Port (cwd=项目根)"
    Merge-DuplicateProxyEnv
    $p = Start-Process -FilePath $BackendExe -ArgumentList "--host","127.0.0.1","--port","$Port" `
        -WorkingDirectory $Root `
        -RedirectStandardOutput $Log -RedirectStandardError "$Log.err" -WindowStyle Hidden -PassThru
    Set-PidFile "backend" $p.Id
    Write-Host "[backend] PID=$($p.Id) 等待 /health（最多 90s）..."
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        if (Test-Health "https://127.0.0.1:$Port/health") {
            $owner = Test-PortOwner -Port $Port -ExpectedProcName "jax-backend.exe"
            if ($owner.Match -eq $true) {
                # PyInstaller onefile 允许监听 PID 是本次 bootstrap 的子进程；进程树中
                # 若只有一组 jax-backend.exe，则这是合法的单实例，不要求 PID 相等。
                $allBackend = @(Get-BackendProcesses)
                if ($allBackend.Count -le 2) {
                    $listener = [int]$owner.Pid
                    $known = @($allBackend | ForEach-Object { [int]$_.ProcessId })
                    if ($known -contains $listener) {
                        Write-Host "[backend][ok] 就绪（onefile 单实例，监听 PID=$listener，进程数=$($allBackend.Count)）"
                        return $true
                    }
                }
                Write-Host "[backend][x] 检测到重复 backend 进程（监听 PID=$($owner.Pid)，进程数=$($allBackend.Count)）；终止本次进程树"
                Stop-BackendProcesses
                Clear-PidFile "backend"
                return $false
            }
            if ($owner.Match -eq $false) {
                Write-Host "[backend][x] 端口被非预期进程占用: $($owner.ProcName) PID=$($owner.Pid)；终止当前启动"
                Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
                Clear-PidFile "backend"
                return $false
            }
        }
        Start-Sleep -Seconds 2
    }
    Write-Host "[backend][x] 90s 内未就绪，终止当前 PID=$($p.Id)，查看 $Log"
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    Clear-PidFile "backend"
    return $false
}

# ---------------- relay_client（公网中继桥接） ----------------
# 待 M2-D 后续（ADR-024 D3）：relay_client 与下方 rtc_bridge 将合并为单一 jax-bridge.exe
# （共享 event loop + 统一健康检查）。本轮二者仍独立进程，不强行合并。
function Start-RelayService {
    Load-Env
    $relayUrl = "wss://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com/relay/ws"
    $gwUrl    = "wss://127.0.0.1:8000/ws/voice"
    $gwCa     = Join-Path $Root "certs\ca.crt"
    $pairCode = "JAX2026"
    $token    = $env:RELAY_TOKEN
    $e2eeKey  = $env:RELAY_E2EE_KEY
    if (-not $token)   { Write-Host "[relay][!] RELAY_TOKEN 为空（中继将拒绝配对）" }
    if (-not $e2eeKey) { Write-Host "[relay][!] RELAY_E2EE_KEY 为空（明文模式，手机需匹配）" }
    # 仅一个完整顶层 relay_client 进程树才是幂等成功。多实例会抢占配对码，必须先收敛。
    $existing = @(Get-RelayTopLevel)
    if ($existing.Count -eq 1 -and (Test-RelayProcessTree)) {
        Set-PidFile "relay" $existing[0].ProcessId
        Write-Host "[relay][ok] 已有唯一 relay_client 实例（PID $($existing[0].ProcessId)），幂等跳过"
        return $true
    }
    if ($existing.Count -gt 1) {
        $ids = ($existing | ForEach-Object { $_.ProcessId }) -join ","
        Write-Host "[relay][!] 检测到 $($existing.Count) 个 relay_client 实例（PID $ids），先受控收敛"
        if (-not (Stop-AllRelay)) {
            Write-Host "[relay][x] relay_client 残留未完全退出，拒绝启动新实例"
            return $false
        }
        if (@(Get-RelayProcesses).Count -ne 0) {
            Write-Host "[relay][x] relay_client 停止后仍有残留，拒绝启动新实例"
            return $false
        }
        Clear-PidFile "relay"
    } elseif ($existing.Count -eq 1) {
        Write-Host "[relay][x] relay_client 进程树不完整，拒绝与残留实例并存启动"
        return $false
    }
    $oldProcId = Get-PidFile "relay"
    if ($oldProcId -and -not (Test-ProcessAlive $oldProcId)) { Clear-PidFile "relay" }
    $log = Join-Path $LogDir "relay_client.log"
    # 2026-08-28 P1：凭据改由子进程继承 env（RELAY_TOKEN/RELAY_E2EE_KEY），
    # 不再经 --token/--e2ee-key 进 argv —— 进程命令行经 WMI 对本机所有用户可见。
    $relayArgs = @("-m","backend.relay.relay_client",
        "--relay", $relayUrl, "--gateway", $gwUrl, "--gateway-ca", $gwCa,
        "--pairing-code", $pairCode)
    Write-Host "[relay] 启动 $PyW $($relayArgs -join ' ')（凭据来自 env，不进 argv）"
    Merge-DuplicateProxyEnv
    $p = Start-Process -FilePath $PyW -ArgumentList $relayArgs -WorkingDirectory $Root `
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

# ---------------- rtc-bridge（TRTC sidecar ↔ apm_bridge 本地桥，RTC 通话承载） ----------------
function Get-RtcBridgeProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "rtc_bridge" }
}
function Start-RtcBridgeService {
    Load-Env
    $Port = 19092
    $HealthPort = 19093
    $Log = Join-Path $LogDir "rtc_bridge.log"
    # 幂等：健康（待命态也算健康）→ 跳过
    if (Test-Health "http://127.0.0.1:$HealthPort/health") {
        $cur = Get-PortPid $HealthPort
        if ($cur) { Set-PidFile "rtc-bridge" $cur }
        Write-Host "[rtc-bridge][ok] 已在运行（幂等跳过，PID=$cur）"
        return $true
    }
    $existing = @(Get-RtcBridgeProcesses)
    if ($existing.Count -gt 0) {
        $ids = ($existing | ForEach-Object { $_.ProcessId }) -join ","
        Set-PidFile "rtc-bridge" $existing[0].ProcessId
        Write-Host "[rtc-bridge][!] 已有进程但 /health 未通过（PID $ids），不盲杀；查看 $Log"
        return $false
    }
    $oldProcId = Get-PidFile "rtc-bridge"
    if ($oldProcId -and -not (Test-ProcessAlive $oldProcId)) { Clear-PidFile "rtc-bridge" }
    $bridgeArgs = @("-m","rtc_bridge.main")
    Write-Host "[rtc-bridge] 启动 $PyW $($bridgeArgs -join ' ')"
    Merge-DuplicateProxyEnv
    $p = Start-Process -FilePath $PyW -ArgumentList $bridgeArgs -WorkingDirectory (Join-Path $Root "backend") `
        -RedirectStandardOutput $Log -RedirectStandardError "$Log.err" -WindowStyle Hidden -PassThru
    Set-PidFile "rtc-bridge" $p.Id
    Write-Host "[rtc-bridge] PID=$($p.Id) 等待 /health（最多 30s）..."
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (Test-Health "http://127.0.0.1:$HealthPort/health") { Write-Host "[rtc-bridge][ok] 就绪"; return $true }
        Start-Sleep -Seconds 2
    }
    Write-Host "[rtc-bridge][x] 30s 内未就绪，查看 $Log"
    return $false
}

# ---------------- 停止（按 PID 文件 + 确认退出；缺失时按端口/命令行定位不盲杀） ----------------
function Stop-ServiceByName([string]$Name) {
    $procId = Get-PidFile $Name
    # relay：统一杀全部（.venv 启动器 + 子 python），避免孤儿
    if ($Name -eq "relay") {
        $rs = @(Get-RelayProcesses)
        if ($rs.Count -gt 0) {
            if (-not (Stop-AllRelay)) {
                Write-Host "[relay][x] relay_client 残留未完全退出"
                return $false
            }
            Write-Host "[relay] 已停止全部 relay_client 进程（$($rs.Count) 个，含启动器+子进程）"
            Clear-PidFile "relay"; return $true
        }
        Write-Host "[relay][ok] 未运行"
        Clear-PidFile "relay"; return $true
    }
    if ($Name -eq "backend") {
        $backendProcs = @(Get-BackendProcesses)
        if ($backendProcs.Count -gt 0) {
            Write-Host "[backend] 停止 onefile 进程树（$($backendProcs.Count) 个 jax-backend.exe）"
            if (-not (Stop-BackendProcesses)) {
                Write-Host "[backend][x] onefile 进程树未完全退出"
                return $false
            }
            Clear-PidFile "backend"
            Write-Host "[backend][ok] 已停止（进程树已收敛）"
            return $true
        }
    }
    # rtc-bridge：与 relay 对齐，一律先按命令行白名单清整棵树（.venv 启动器 + re-exec 子进程）。
    # 否则 PID 文件分支只杀启动器，留下持有 19092/19093 的孤儿子进程；restart 随后被
    # Start-RtcBridgeService 的 /health 幂等判定采纳该旧进程，改动永远不生效（静默空转）。
    if ($Name -eq "rtc-bridge") {
        $rs = @(Get-RtcBridgeProcesses)
        if ($rs.Count -gt 0) {
            foreach ($r in $rs) { Stop-Process -Id $r.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Milliseconds 800
            $left = @(Get-RtcBridgeProcesses)
            foreach ($r in $left) { Stop-Process -Id $r.ProcessId -Force -ErrorAction SilentlyContinue }
            $still = @(Get-RtcBridgeProcesses)
            if ($still.Count -gt 0) {
                $ids = ($still | ForEach-Object { $_.ProcessId }) -join ","
                Write-Host "[rtc-bridge][x] 残留 $($still.Count) 个进程未退出（PID $ids）"
                return $false
            }
            Write-Host "[rtc-bridge] 已停止全部 rtc_bridge 进程（$($rs.Count) 个，含启动器+子进程）"
            Clear-PidFile "rtc-bridge"; return $true
        }
        Write-Host "[rtc-bridge][ok] 未运行"
        Clear-PidFile "rtc-bridge"; return $true
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
            if ($cmd -match "jax-model") {
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                Write-Host "[model] 按端口 19080 定位并停止 PID=$procId（命令行含 jax-model）"
                Clear-PidFile "model"; return $true
            }
            Write-Host "[model][!] 端口 19080 被非 jax-model 进程占用（PID $procId），不盲杀"; return $false
        }
    } elseif ($Name -eq "backend") {
        $procId = Get-PortPid 8000
        if ($procId) {
            $cmd = Get-PortProcCommandLine 8000
            if ($cmd -match "jax-backend") {
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                Write-Host "[backend] 按端口 8000 定位并停止 PID=$procId（命令行含 jax-backend）"
                Clear-PidFile "backend"; return $true
            }
            Write-Host "[backend][!] 端口 8000 被非 jax-backend 进程占用（PID $procId），不盲杀"; return $false
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
    $bPort = Test-PortListen 8000; $bHealth = Test-Health "https://127.0.0.1:8000/health"
    $bState = if ($bHealth) { "OK" } elseif ($bPort) { "PORT-NOHEALTH" } elseif ($bAlive) { "PID-ALIVE" } else { "DOWN" }
    Write-Host ("[backend] :8000     {0}{1}" -f $bState, $(if ($bProcId) { "  PID=$bProcId" } else { "" }))
    # relay
    $rProcId = Get-PidFile "relay"; $rAlive = Test-ProcessAlive $rProcId
    $rTop = @(Get-RelayTopLevel)
    $rCount = $rTop.Count
    if ($rCount -gt 0) { $rState = "RUNNING(x$rCount)" } elseif ($rAlive) { $rState = "PID-ALIVE" } else { $rState = "DOWN" }
    Write-Host ("[relay]   wss-relay {0}{1}" -f $rState, $(if ($rProcId) { "  PID=$rProcId" } else { "" }))
    if ($rCount -gt 1) { Write-Host "[relay][!] 检测到多个 relay_client 实例残留（可能互相抢占配对码），建议 restart relay" }
    # rtc-bridge
    $rbProcId = Get-PidFile "rtc-bridge"; $rbAlive = Test-ProcessAlive $rbProcId
    $rbHealth = Test-Health "http://127.0.0.1:19093/health"
    $rbState = if ($rbHealth) { "OK" } elseif ($rbAlive) { "PID-ALIVE" } else { "DOWN" }
    Write-Host ("[rtc-bridge] :19092  {0}{1}" -f $rbState, $(if ($rbProcId) { "  PID=$rbProcId" } else { "" }))
    Write-Host "========================================"
}

# ---------------- fail-closed 退出码 epilogue（exit-code advisory 修复 2026-09-03） ----------------
# 语义：start/stop/restart 任一服务操作失败 → exit 1；全部成功 → exit 0。
#       status 为信息性命令，永远 exit 0（服务 DOWN 不算脚本失败）。
#       并发互斥忙 → exit 2（上方既有行为，不变）。
# 调用方安全：jax-watchdog.ps1 以子作用域 & 调用本脚本后用自身健康复查判定，
#             不消费退出码；契约 backend/tests/contract/test_jax_services_exit_code_contract.py 锁定本语义。
function Invoke-ExitCode {
    param([bool]$HadFailure, [string]$Action)
    if ($Action -eq "status") { exit 0 }
    if ($HadFailure) { exit 1 }
    exit 0
}

# ---------------- 主流程 ----------------
$svcs = @()
if ($Service -eq "all") { $svcs = @("model","backend","relay","rtc-bridge") } else { $svcs = @($Service) }
$HadFailure = $false

try {
    switch ($Action) {
        "start" {
            foreach ($s in $svcs) {
                if (-not (Invoke-SvcStart $s)) {
                    Write-Host "[services][x] $s 启动失败（fail-closed → exit 1）"
                    $HadFailure = $true
                }
            }
        }
        "stop" {
            foreach ($s in $svcs) {
                if (-not (Stop-ServiceByName $s)) {
                    Write-Host "[services][x] $s 停止失败（fail-closed → exit 1）"
                    $HadFailure = $true
                }
            }
        }
        "restart" {
            foreach ($s in $svcs) {
                # Stop 失败仍继续 Start（Start 自身幂等/身份校验会处置残留），但结果分别记账。
                if (-not (Stop-ServiceByName $s)) {
                    Write-Host "[services][x] $s 停止失败（fail-closed → exit 1）"
                    $HadFailure = $true
                }
                # Stop-ServiceByName 已等待 PID 退出；此处只给端口/句柄释放一个短暂稳定窗口。
                Start-Sleep -Seconds 1
                if (-not (Invoke-SvcStart $s)) {
                    Write-Host "[services][x] $s 启动失败（fail-closed → exit 1）"
                    $HadFailure = $true
                }
            }
        }
        "status" {
            Show-Status
        }
    }
} finally {
    $ServiceMutex.ReleaseMutex() | Out-Null
    $ServiceMutex.Dispose()
}

Invoke-ExitCode $HadFailure $Action
