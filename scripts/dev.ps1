# 一键开发启动：模型 server（提示/健康检查）→ 后端 uvicorn → 前端 Vite
# 用法: powershell -ExecutionPolicy Bypass -File scripts/dev.ps1

$ErrorActionPreference = "Stop"
# scripts 的父目录即项目根
$Root = Split-Path $PSScriptRoot -Parent

$backendProc = $null

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
    if (Test-Path (Join-Path $Root ".env")) {
        $envVars = Get-Content (Join-Path $Root ".env") | Where-Object { $_ -match "=" }
        foreach ($line in $envVars) {
            $kv = $line -split "=", 2
            if ($kv[0]) { Set-Item -Path "Env:$($kv[0])" -Value $kv[1] }
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
            $ctx = if ($env:MODEL_CTX_SIZE) { $env:MODEL_CTX_SIZE } else { "8192" }
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
    $backendProc = Start-Process -FilePath (Join-Path $Root ".venv/Scripts/python.exe") `
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
