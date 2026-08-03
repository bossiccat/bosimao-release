# 下载 MiniCPM-o 4.5 GGUF 模型（国内走 ModelScope）
# 只下载关键文件（Q4_K_M + mmproj，约 8.3GB），避免整仓 ~80GB
# 幂等：Q4_K_M 与 mmproj 均已存在时跳过下载（支持续传/跳过已有，--force 未启用）
#
# modelscope 语法（已实测 modelscope 1.39.0 / modelscope_hub 0.2.0）：
#   modelscope download <repo_id> --local-dir <dir> --include <p1> <p2> ...
#   --include / --exclude 均为 nargs='+' 多值、可重复；--local-dir 直下到目录；
#   不传 --force 即幂等（已有文件跳过，.incomplete 续传）。
#
# 用法: powershell -ExecutionPolicy Bypass -File scripts/download_model.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$target = "D:\models\MiniCPM-o-4_5-gguf"

# ---- 1. 找一个带 modelscope 的 python 入口（venv 优先，其次 py -3.11，最后 python）----
function Select-MsEntry {
    $candidates = @()
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { $candidates += @{ label = "venv"; cmd = $venvPy } }
    $candidates += @{ label = "py311"; cmd = "py" }
    $candidates += @{ label = "python"; cmd = "python" }

    foreach ($c in $candidates) {
        if ($c.label -eq "py311") {
            & py -3.11 -c "import modelscope" 2>$null
            if ($LASTEXITCODE -eq 0) { return "py -3.11 -m modelscope.cli.cli" }
        } else {
            & $c.cmd -c "import modelscope" 2>$null
            if ($LASTEXITCODE -eq 0) { return "& `"$($c.cmd)`" -m modelscope.cli.cli" }
        }
    }
    return $null
}

$msEntry = Select-MsEntry
if (-not $msEntry) {
    Write-Host "==> 未找到 modelscope，尝试安装"
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        & $venvPy -m pip install -U modelscope
    } else {
        python -m pip install -U modelscope
    }
    if ($LASTEXITCODE -ne 0) { throw "modelscope 安装失败" }
    $msEntry = Select-MsEntry
    if (-not $msEntry) { throw "modelscope 仍不可用" }
}
Write-Host "==> 使用入口: $msEntry"

# ---- 2. 幂等判断 ----
if (-not (Test-Path $target)) { New-Item -ItemType Directory -Force -Path $target | Out-Null }

$q4km = Join-Path $target "MiniCPM-o-4_5-Q4_K_M.gguf"
$mmproj = Get-ChildItem -Path $target -Filter "mmproj-*.gguf" -ErrorAction SilentlyContinue | Select-Object -First 1

if ((Test-Path $q4km) -and $mmproj) {
    Write-Host "==> 关键文件已存在，跳过下载（幂等）"
    Write-Host "  Q4_K_M : $([math]::Round((Get-Item $q4km).Length / 1GB, 2)) GB"
    Write-Host "  mmproj : $($mmproj.Name) ($([math]::Round($mmproj.Length / 1GB, 2)) GB)"
    Write-Host "==> 完成"
    exit 0
}

$partial = Get-ChildItem -Path $target -Filter "*.incomplete" -ErrorAction SilentlyContinue
if ($partial) {
    Write-Host "==> 检测到 $($partial.Count) 个未完成文件（*.incomplete），不在 include 清单内的不受影响"
}

# ---- 3. 下载关键文件 ----
Write-Host "==> 下载关键文件（约 8.3GB：Q4_K_M + mmproj，请保持网络稳定）"
Write-Host "    仅拉取: MiniCPM-o-4_5-Q4_K_M.gguf / mmproj-*.gguf，跳过整仓 (~80GB)"
Invoke-Expression "$msEntry download OpenBMB/MiniCPM-o-4_5-gguf --local-dir `"$target`" --include `"MiniCPM-o-4_5-Q4_K_M.gguf`" `"mmproj-*.gguf`""
if ($LASTEXITCODE -ne 0) { throw "modelscope download 失败" }

# ---- 4. 验证 ----
Write-Host "==> 验证关键文件"
if (Test-Path $q4km) {
    Write-Host "OK: Q4_K_M = $([math]::Round((Get-Item $q4km).Length / 1GB, 2)) GB"
} else {
    Write-Host "[警告] Q4_K_M 未找到，请检查模型仓库实际文件清单"
}
$mmprojAfter = Get-ChildItem -Path $target -Filter "mmproj-*.gguf" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($mmprojAfter) {
    Write-Host "OK: mmproj = $($mmprojAfter.Name) ($([math]::Round($mmprojAfter.Length / 1GB, 2)) GB)"
} else {
    Write-Host "[警告] mmproj-*.gguf 未找到；仓库 mmproj 实际文件名可能是 mmproj-MiniCPM-o-4_5-f16.gguf 等，请用 modelscope 文件列表确认"
}
Write-Host "==> 完成"
