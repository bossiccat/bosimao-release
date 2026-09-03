# 一键开发启动：模型 server（提示/健康检查）→ 后端 uvicorn → 前端 Vite
# 用法: powershell -ExecutionPolicy Bypass -File scripts/dev.ps1

$ErrorActionPreference = "Stop"
# scripts 的父目录即项目根
$Root = Split-Path $PSScriptRoot -Parent
# O-018 切片 3：复用 lib-common 的 CM 读取与 .env 原子写助手（与 jax-services.ps1 同源，防漂移）
. (Join-Path $PSScriptRoot "lib-common.ps1")

$backendProc = $null

function Invoke-OwnerCredentialProvision {
    # owner credential 首启 provision（ADR-022）：backend 启动前调用，非零退出即中止（fail-closed）
    $exe = $null
    $release = Join-Path $Root "pet-ui\src-tauri\target\release\provision_owner_credential.exe"
    $debug   = Join-Path $Root "pet-ui\src-tauri\target\debug\provision_owner_credential.exe"
    if (Test-Path $release) { $exe = $release }
    elseif (Test-Path $debug) { $exe = $debug }
    else {
        Write-Warning "[owner-credential] provisioner 未编译；请先: cd pet-ui/src-tauri; cargo build --bin provision_owner_credential"
        return $false
    }
    # GUI 子系统二进制——不带 -WindowStyle（避免冲突），-Wait 等退出码
    $p = Start-Process -FilePath $exe -Wait -PassThru
    if ($p.ExitCode -ne 0) {
        Write-Error "[owner-credential] provision 失败（退出码 $($p.ExitCode)），中止启动（fail-closed）"
        return $false
    }
    Write-Host "[owner-credential][ok] owner credential 已就绪"
    return $true
}

function Invoke-SidecarCredentialProvision {
    # O-018 切片 3：sidecar credential「launcher → CM → .env 同值同步」编排
    # dev.ps1 形态：backend 尚未启动（在下方才拉起），故无重启分支；同值幂等跳过。
    $launcher = $null
    $release = Join-Path $Root "pet-ui\src-tauri\target\release\provision_sidecar_credential_launcher.exe"
    $debug   = Join-Path $Root "pet-ui\src-tauri\target\debug\provision_sidecar_credential_launcher.exe"
    if (Test-Path $release) { $launcher = $release }
    elseif (Test-Path $debug) { $launcher = $debug }
    else {
        Write-Warning "[sidecar-credential] launcher 未编译；请先: cd pet-ui/src-tauri; cargo build --bin provision_sidecar_credential_launcher [SIDECARPROV_001]"
        return $false
    }

    $envPath = Join-Path $Root ".env"
    $cmTarget = "JaxPet/com.jax.pet/voice-sidecar/v1"
    $envValue = Get-DotEnvValue $envPath "VOICE_SIDECAR_CREDENTIAL"
    $cmValue  = Get-CredentialBlobFromCM $cmTarget

    # 幂等：两侧都有值且同值 → 不跑 launcher、不动盘
    if ($cmValue -and $envValue -and ($cmValue -eq $envValue)) {
        Write-Host "[sidecar-credential][ok] CM 与 .env 已同值，幂等跳过"
        return $true
    }

    Merge-DuplicateProxyEnv
    $p = Start-Process -FilePath $launcher -Wait -PassThru
    if ($p.ExitCode -ne 0) {
        Write-Error "[sidecar-credential] launcher 失败（退出码 $($p.ExitCode)），中止启动（fail-closed）[SIDECARPROV_002]"
        return $false
    }
    $cmValue = Get-CredentialBlobFromCM $cmTarget
    if (-not $cmValue) {
        Write-Error "[sidecar-credential] provision 后 CM active 不可读，中止启动（fail-closed）[SIDECARPROV_003]"
        return $false
    }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hashPrefix = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($cmValue)))).Replace("-","").Substring(0,16).ToLower()
    if ($envValue -eq $cmValue) {
        Write-Host "[sidecar-credential][ok] 已同值（sha16=$hashPrefix），幂等跳过"
        return $true
    }
    if (-not (Set-DotEnvValue $envPath "VOICE_SIDECAR_CREDENTIAL" $cmValue)) {
        Write-Error "[sidecar-credential] .env 单键写入/校验失败，中止启动（fail-closed）[SIDECARPROV_004]"
        return $false
    }
    Write-Host "[sidecar-credential][ok] .env 已同步 CM 值（len=$($cmValue.Length)，sha16=$hashPrefix，备份已落）"
    return $true
}

function Stop-Backend {
    if ($backendProc -and -not $backendProc.HasExited) {
        Write-Host "==> 清理后端进程 PID $($backendProc.Id)"
        Stop-Process -Id $backendProc.Id -Force -ErrorAction SilentlyContinue
        $backendProc = $null
    }
}

function Wait-Health([string]$url, [int]$timeoutSec) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { return $true }
        } catch {
            # 未就绪，继续等
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

try {
    # 1. 读取 .env 注入环境变量
    # -Encoding UTF8 必须显式声明：.env 为 UTF-8 无 BOM，PS5.1 默认按系统 ANSI(GBK)
    # 解码会把含中文的绝对路径（SSL_CERT_FILE / RTC_BRIDGE_CONTROL_PLANE_* 等）
    # mojibake 注入子进程，再经 Start-Process(:94) 传给 backend、经 npm run tauri
    # dev(:126) 传给 Tauri -> sidecar 全链继承（2026-09-01 编码审计 P1-B）。
    if (Test-Path (Join-Path $Root ".env")) {
        # 过滤必须是严格 KV 正则（对齐 jax-services.ps1:86 / start-relay.ps1:33）：
        # .env 含带 = 的注释行（首行 `# ===== 环境变量模板 =====`、第 34 行
        # `# voice 网关鉴权（V1.5 M1）：...；留空=不校验`），宽松的 $_ -match "="
        # 会把它们当 KV 注入 —— PS 5.1 下 Set-Item -Path "Env:# " 会**成功**，
        # 静默创建名为 `#` 的垃圾环境变量（2026-09-01 Load-Env 专项 P2）。
        Get-Content (Join-Path $Root ".env") -Encoding UTF8 | ForEach-Object {
            if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
                Set-Item -Path "Env:$($matches[1])" -Value $matches[2]
            }
        }
    }

    # 2. 模型服务：文件存在 + :19080 健康检查；不代为拉起，给出启动命令
    Write-Host "==> 检查模型服务"
    $modelDir = $env:MODEL_DIR
    $modelFile = $env:MODEL_FILE
    $modelPort = if ($env:MODEL_SERVER_PORT) { $env:MODEL_SERVER_PORT } else { "19080" }
    $modelPath = if ($modelDir -and $modelFile) { Join-Path $modelDir $modelFile } else { $null }

    if ($modelPath -and (Test-Path $modelPath)) {
        Write-Host "模型文件存在: $modelPath"
        $modelReady = $false
        try {
            $health = Invoke-WebRequest -Uri "http://127.0.0.1:$modelPort/health" -UseBasicParsing -TimeoutSec 3
            if ($health.StatusCode -eq 200) { $modelReady = $true }
        } catch {
            $modelReady = $false
        }
        if ($modelReady) {
            Write-Host "模型服务健康检查通过: http://127.0.0.1:$modelPort/health"
        } else {
            $ngl = if ($env:MODEL_NGL) { $env:MODEL_NGL } else { "99" }
            $ctx = if ($env:MODEL_CTX_SIZE) { $env:MODEL_CTX_SIZE } else { "4096" }
            Write-Host "[提示] 模型服务未就绪（:$modelPort 健康检查失败）"
            Write-Host "  → 请启动 Comni 桌面版，或手动运行："
            Write-Host "    llama-omni-server --host 127.0.0.1 --port $modelPort --model `"$modelPath`" -ngl $ngl --ctx-size $ctx"
            Write-Host "  （模型服务由 Comni 桌面版或手动启动，dev.ps1 仅提示，不代为拉起）"
        }
    } else {
        Write-Host "[提示] 模型未就绪，先运行 scripts/download_model.ps1"
    }

    # 3. 启动后端 uvicorn :8000
    Write-Host "==> 启动后端 (uvicorn :8000)"
    # owner credential 首启 provision（ADR-022）：backend 之前，失败即中止（fail-closed）
    if (-not (Invoke-OwnerCredentialProvision)) { throw "[owner-credential] provision 失败，中止启动" }
    # sidecar credential「launcher → CM → .env」同值编排（O-018 切片 3）：失败即中止（fail-closed）
    if (-not (Invoke-SidecarCredentialProvision)) { throw "[sidecar-credential] provision 失败，中止启动" }
    $backendProc = Start-Process -FilePath (Join-Path $Root ".venv/Scripts/pythonw.exe") `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000" `
        -WorkingDirectory (Join-Path $Root "backend") -PassThru -NoNewWindow
    Write-Host "后端 PID: $($backendProc.Id)"

    # 4. 轮询后端健康检查（最多 30s）
    Write-Host "==> 等待后端就绪 http://127.0.0.1:8000/health (最多 30s)"
    if (-not (Wait-Health "http://127.0.0.1:8000/health" 30)) {
        Write-Host "[警告] 后端 30s 内未就绪，仍尝试启动前端（请检查 backend 日志）"
    } else {
        Write-Host "后端就绪"
    }

    # 5. 前端启动前检查 Tauri icons
    $iconsDir = Join-Path $Root "pet-ui/src-tauri/icons"
    if (-not (Test-Path $iconsDir)) {
        Write-Host "[警告] pet-ui/src-tauri/icons 缺失，Tauri 构建会失败"
        Write-Host "  请先在 pet-ui 目录执行: npm run tauri icon <图标png路径>"
    }

    # 6. 启动前端 (Vite :5173 + Tauri)
    Write-Host "==> 启动前端 (Vite :5173 + Tauri)"
    Push-Location (Join-Path $Root "pet-ui")
    npm install
    npm run tauri dev
    Pop-Location
} finally {
    Stop-Backend
}
