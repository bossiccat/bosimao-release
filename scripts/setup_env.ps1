# 环境初始化：Python venv + 依赖安装 + 目录创建
# 用法: powershell -ExecutionPolicy Bypass -File scripts/setup_env.ps1
# 说明: 优先用系统 py -3.11 创建 pip 完整的 venv（与项目 py311 目标一致）；
#       若环境变量 HTTP_PROXY/HTTPS_PROXY 指向不可达代理导致 pip 网络失败，请临时清除后重跑。

$ErrorActionPreference = "Stop"
# scripts 的父目录即项目根
$Root = Split-Path $PSScriptRoot -Parent

Write-Host "==> 创建目录结构"
@("tmp/captures", "logs") | ForEach-Object {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root $_) | Out-Null
}

Write-Host "==> 创建 Python venv（优先 py -3.11，退回 python）"
$venv = Join-Path $Root ".venv"
$venvPy = Join-Path $venv "Scripts/python.exe"
$venvPip = Join-Path $venv "Scripts/pip.exe"
if (-not (Test-Path $venvPy) -or -not (Test-Path $venvPip)) {
    # venv 缺失或 pip 不完整（如 python -m venv 未装 pip）→ 重建
    if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
    $py311 = & py -3.11 -c "import sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $py311) {
        Write-Host "  使用 py -3.11 ($py311)"
        & py -3.11 -m venv $venv
    } else {
        Write-Host "  使用默认 python"
        python -m venv $venv
    }
    if ($LASTEXITCODE -ne 0) { throw "venv 创建失败" }
} else {
    Write-Host "  venv 已存在: $venvPy"
}

Write-Host "==> 安装 Python 依赖"
# 默认升级 pip；设 $env:SKIP_PIP_UPGRADE=1 可跳过（升级需卸载旧版 pip，部分受控环境会拦截批量删除）
if (-not $env:SKIP_PIP_UPGRADE) {
    & $venvPy -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { Write-Host "[警告] pip 升级失败，尝试继续安装依赖（代理问题请清除 HTTP_PROXY/HTTPS_PROXY 后重跑）" }
}
& $venvPy -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install 失败（若为代理问题请清除 HTTP_PROXY/HTTPS_PROXY 后重跑）" }

Write-Host "==> 复制环境变量模板（若不存在）"
$envExample = Join-Path $Root ".env.example"
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item $envExample $envFile
    Write-Host "已创建 .env，请填写 WECOM_WEBHOOK_URL / NTFY_TOPIC"
}

Write-Host "==> 完成"
