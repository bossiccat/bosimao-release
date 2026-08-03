# 下载 MiniCPM-o 4.5 GGUF 模型（国内走 ModelScope）
# 只下载关键文件（约 8.3GB），避免整仓 ~80GB：
#   MiniCPM-o-4_5-Q4_K_M.gguf       主模型（运行必需）
#   vision/*.gguf                   视觉投影（即"mmproj"等价物）
#   audio/*.gguf + tts/*.gguf       语音输入/输出组件
#   token2wav-gguf/*.gguf           语音合成组件
# 幂等：以上关键文件齐全时跳过下载（支持续传/跳过已有，--force 未启用）
#
# 实测说明（modelscope 1.39.0 / modelscope_hub 0.2.0，2026-08-03）：
#   仓库 OpenBMB/MiniCPM-o-4_5-gguf 共 35 文件 / 20 个 GGUF，
#   【不存在 mmproj-*.gguf】，视觉投影实际为 vision/MiniCPM-o-4_5-vision-F16.gguf。
#   CLI：--include/--exclude 均为 nargs='+' 多值、可重复；--local-dir 直下到目录；
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
        try {
            if ($c.label -eq "py311") {
                & py -3.11 -c "import modelscope" 2>$null
                if ($LASTEXITCODE -eq 0) { return "py -3.11 -m modelscope.cli.cli" }
            } else {
                & $c.cmd -c "import modelscope" 2>$null
                if ($LASTEXITCODE -eq 0) { return "& `"$($c.cmd)`" -m modelscope.cli.cli" }
            }
        } catch {
            # 该候选不可用（原生 stderr 在 ErrorActionPreference=Stop 下会抛 NativeCommandError），尝试下一个
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

# ---- 2. 幂等判断：关键文件齐全则跳过 ----
if (-not (Test-Path $target)) { New-Item -ItemType Directory -Force -Path $target | Out-Null }

$groups = @(
    @{ name = "主模型 Q4_K_M";     pattern = "MiniCPM-o-4_5-Q4_K_M.gguf";       rel = "MiniCPM-o-4_5-Q4_K_M.gguf" },
    @{ name = "视觉投影 vision";   pattern = "vision/*.gguf";                  rel = "vision" },
    @{ name = "语音输入 audio";    pattern = "audio/*.gguf";                   rel = "audio" },
    @{ name = "语音输出 tts";      pattern = "tts/*.gguf";                     rel = "tts" },
    @{ name = "语音合成 token2wav"; pattern = "token2wav-gguf/*.gguf";         rel = "token2wav-gguf" }
)

$allPresent = $true
foreach ($g in $groups) {
    if ($g.rel -like "*.gguf") {
        $hit = Test-Path (Join-Path $target $g.rel)
    } else {
        $hit = (Get-ChildItem -Path (Join-Path $target $g.rel) -Filter "*.gguf" -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0
    }
    if (-not $hit) { $allPresent = $false }
}

if ($allPresent) {
    Write-Host "==> 关键文件已齐全，跳过下载（幂等）"
    foreach ($g in $groups) {
        if ($g.rel -like "*.gguf") {
            $f = Join-Path $target $g.rel
            Write-Host "  $($g.name): $([math]::Round((Get-Item $f).Length / 1GB, 2)) GB"
        } else {
            $fs = Get-ChildItem -Path (Join-Path $target $g.rel) -Filter "*.gguf" -ErrorAction SilentlyContinue
            $gb = ($fs | Measure-Object -Property Length -Sum).Sum / 1GB
            Write-Host "  $($g.name): $($fs.Count) 个文件, $([math]::Round($gb, 2)) GB"
        }
    }
    Write-Host "==> 完成"
    exit 0
}

$partial = Get-ChildItem -Path $target -Filter "*.incomplete" -ErrorAction SilentlyContinue
if ($partial) {
    Write-Host "==> 检测到 $($partial.Count) 个未完成文件（*.incomplete），不在 include 清单内的不受影响"
}

# ---- 3. 下载关键文件 ----
Write-Host "==> 下载关键文件（约 8.3GB：Q4_K_M + vision + audio + tts + token2wav，请保持网络稳定）"
Write-Host "    仅拉取关键文件，跳过整仓 (~80GB)"
Invoke-Expression "$msEntry download OpenBMB/MiniCPM-o-4_5-gguf --local-dir `"$target`" --include `"MiniCPM-o-4_5-Q4_K_M.gguf`" `"vision/*.gguf`" `"audio/*.gguf`" `"tts/*.gguf`" `"token2wav-gguf/*.gguf`""
if ($LASTEXITCODE -ne 0) { throw "modelscope download 失败" }

# ---- 4. 验证 ----
Write-Host "==> 验证关键文件"
foreach ($g in $groups) {
    if ($g.rel -like "*.gguf") {
        if (Test-Path (Join-Path $target $g.rel)) {
            Write-Host "OK: $($g.name) = $([math]::Round((Get-Item (Join-Path $target $g.rel)).Length / 1GB, 2)) GB"
        } else {
            Write-Host "[警告] $($g.name) 未找到"
        }
    } else {
        $fs = Get-ChildItem -Path (Join-Path $target $g.rel) -Filter "*.gguf" -ErrorAction SilentlyContinue
        if ($fs) { Write-Host "OK: $($g.name) = $($fs.Count) 个文件" } else { Write-Host "[警告] $($g.name) 未找到" }
    }
}
Write-Host "==> 完成"
