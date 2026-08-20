# Jax Voice M1 依赖下载脚本（Windows PowerShell）
# 用法：在 mobile-app/ 目录执行  powershell -ExecutionPolicy Bypass -File scripts/fetch-deps.ps1
# 作用：
#   1) sherpa-onnx Android AAR（含 Java/Kotlin API + JNI） → app/libs/
#   2) KWS 唤醒词模型（wenetspeech-3.3M） → app/src/main/assets/
#   3)（可选，若 AAR 未带 jni）解压 android 包 jniLibs → app/src/main/jniLibs/
# 版本锚定：sherpa-onnx 1.13.4（与 API 代码核对过）
#
# 完整性：所有下载均按 scripts/deps-checksums.txt 校验 SHA-256，
#         不匹配的文件立即删除并以非 0 退出码失败（绝不静默使用被篡改的输入）。
#         已存在且哈希正确的文件跳过下载；已存在但哈希错误 → 删除重下。

$ErrorActionPreference = "Stop"
# 关闭进度条：PS 5.1 的 Invoke-WebRequest 进度条渲染会把大文件下载拖慢 10 倍以上
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot

# tar 解压器：必须用 Windows 原生 bsdtar。GitHub windows-2022 runner 的 PATH 前置了
# Strawberry Perl 的 MSYS tar，在 PS 5.1 管道下解压 bz2 会无限挂死（Run#7 卡 31min 实锤）。
$tarExe = "C:\Windows\System32\tar.exe"
if (-not (Test-Path $tarExe)) { $tarExe = "tar" } # 非 Windows 兜底

$checksumFile = Join-Path $PSScriptRoot "deps-checksums.txt"
if (-not (Test-Path $checksumFile)) {
    Write-Error "缺少哈希清单 $checksumFile —— 拒绝无校验下载"
    exit 1
}

# 解析清单：@( @{sha=".."; path=".."} ... )，tag 为 archive:/file:（校验时机不同，哈希口径相同）
$manifest = @()
foreach ($line in Get-Content $checksumFile -Encoding UTF8) {
    $t = $line.Trim()
    if ($t -eq "" -or $t.StartsWith("#")) { continue }
    if ($t -match '^(archive|file):\s*([0-9a-fA-F]{64})\s+(.+)$') {
        $manifest += [pscustomobject]@{ Tag = $Matches[1]; Sha = $Matches[2].ToLower(); RelPath = $Matches[3].Trim() -replace '/', '\' }
    } else {
        Write-Error "deps-checksums.txt 存在无法解析的行: $line"
        exit 1
    }
}
if ($manifest.Count -eq 0) {
    Write-Error "deps-checksums.txt 为空 —— 拒绝无校验下载"
    exit 1
}

function Get-Sha256([string]$file) {
    return (Get-FileHash -Algorithm SHA256 -Path $file).Hash.ToLower()
}

# 校验单个文件；返回 "ok" / "mismatch" / "absent"
function Test-File([pscustomobject]$entry) {
    $p = Join-Path $root $entry.RelPath
    if (-not (Test-Path $p)) { return "absent" }
    if ((Get-Sha256 $p) -eq $entry.Sha) { return "ok" } else { return "mismatch" }
}

function Remove-BadFile([pscustomobject]$entry) {
    $p = Join-Path $root $entry.RelPath
    if (Test-Path $p) {
        Write-Host "    哈希不匹配，删除已损坏文件: $($entry.RelPath)"
        Remove-Item $p -Force
    }
}

$aarUrl   = "https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.4/sherpa-onnx-1.13.4.aar"
$modelUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2"

# 归档级清单条目（下载目标即最终文件的用 file: 口径，tar.bz2 中间产物用 archive: 口径）
$aarEntry       = $manifest | Where-Object { $_.RelPath -eq 'app\libs\sherpa-onnx-1.13.4.aar' -and $_.Tag -eq 'file' }
$modelArchive   = $manifest | Where-Object { $_.Tag -eq 'archive' -and $_.RelPath -like '*wenetspeech*tar.bz2' }
$modelFileEntry = $manifest | Where-Object { $_.Tag -eq 'file' -and $_.RelPath -like '*\tokens.txt' }

if (-not $aarEntry -or -not $modelArchive -or -not $modelFileEntry) {
    Write-Error "deps-checksums.txt 缺少 AAR 或模型条目 —— 拒绝无校验下载"
    exit 1
}

New-Item -ItemType Directory -Force -Path "$root\app\libs" | Out-Null
New-Item -ItemType Directory -Force -Path "$root\app\src\main\assets" | Out-Null

Write-Host "[1/3] 下载 sherpa-onnx AAR -> app/libs ..."
switch (Test-File $aarEntry) {
    "ok"       { Write-Host "    已存在且哈希校验通过，跳过" }
    "mismatch" { Remove-BadFile $aarEntry; Invoke-WebRequest -Uri $aarUrl -OutFile (Join-Path $root $aarEntry.RelPath) }
    "absent"   { Invoke-WebRequest -Uri $aarUrl -OutFile (Join-Path $root $aarEntry.RelPath) }
}
if ((Test-File $aarEntry) -ne "ok") {
    Remove-BadFile $aarEntry
    Write-Error "AAR SHA-256 校验失败（期望 $($aarEntry.Sha)）—— 已删除，中止"
    exit 1
}
Write-Host "    SHA-256 OK: $($aarEntry.Sha)"

Write-Host "[2/3] 下载 KWS 模型 -> app/src/main/assets ..."
$modelDir = Join-Path $root "app\src\main\assets\sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
$modelTmp = Join-Path $env:TEMP "sherpa-kws.tar.bz2"
if ((Test-File $modelFileEntry) -eq "ok") {
    Write-Host "    模型已存在且 tokens.txt 哈希校验通过，跳过"
} else {
    # 已存在但不完整/被改 → 整目录重建，防残留半解压状态
    if (Test-Path $modelDir) {
        Write-Host "    检测到已存在但校验失败的模型目录，删除重建"
        Remove-Item $modelDir -Recurse -Force
    }
    Invoke-WebRequest -Uri $modelUrl -OutFile $modelTmp
    $tmpSha = Get-Sha256 $modelTmp
    if ($tmpSha -ne $modelArchive.Sha) {
        Remove-Item $modelTmp -Force
        Write-Error "模型归档 SHA-256 校验失败（期望 $($modelArchive.Sha)，实际 $tmpSha）—— 已删除，中止"
        exit 1
    }
    Write-Host "    归档 SHA-256 OK: $($modelArchive.Sha)"
    Write-Host "    [$(Get-Date -Format HH:mm:ss)] 解压中（python tarfile，无外部 tar 进程）..."
    # 不用 tar.exe：GitHub runner 无终端环境下 bsdtar 的 stderr 进度控制序列会永久阻塞
    # （Run#7 MSYS tar / Run#8 System32 tar 直调 / Run#9 Start-Process 三种方式同位置挂死实锤）。
    # Python tarfile 纯缓冲 stdout，且 runner 与本机均预装 Python，行为一致。
    $pyScript = Join-Path $env:TEMP "extract_kws.py"
@'
import sys, tarfile
src, dest = sys.argv[1], sys.argv[2]
with tarfile.open(src, "r:bz2") as t:
    try:
        t.extractall(dest, filter="data")
    except TypeError:
        t.extractall(dest)
    print("extracted entries:", len(t.getmembers()))
'@ | Set-Content -Path $pyScript -Encoding ASCII
    $pyExe = @(Get-Command python, py -ErrorAction SilentlyContinue | Select-Object -First 1)[0].Source
    if (-not $pyExe) { Write-Error "python/py 均不可用 —— 无法解压 bz2"; exit 1 }
    & $pyExe $pyScript $modelTmp "$root\app\src\main\assets"
    if ($LASTEXITCODE -ne 0) { Write-Error "python 解压失败（exit $LASTEXITCODE）—— 中止"; exit 1 }
    Remove-Item $pyScript -Force -ErrorAction SilentlyContinue
    Write-Host "    [$(Get-Date -Format HH:mm:ss)] 解压完成"
    Remove-Item $modelTmp
    # 解压后逐文件校验运行时闭集（file: 条目中位于模型目录下的全部文件）。
    # 注意：keywords_jax.txt 是自建文件、不在归档内，此时必缺——由下方恢复段补齐后统一复验，
    # 此处只校验归档应含的文件（排除 keywords_jax.txt）。
    $failed = @()
    foreach ($e in ($manifest | Where-Object { $_.Tag -eq 'file' -and $_.RelPath -like 'app\src\main\assets\sherpa-onnx-*' -and $_.RelPath -notlike '*keywords_jax.txt' })) {
        if ((Test-File $e) -ne "ok") { $failed += $e.RelPath }
    }
    if ($failed.Count -gt 0) {
        Write-Error "解压后文件级校验失败: $($failed -join ', ') —— 中止"
        exit 1
    }
    Write-Host "    解压后文件级哈希全部通过"
}

# keywords_jax.txt 是本项目自建文件（上游不含），由本清单哈希管控；
# 全新 clone 后缺失则从代码库内模板恢复（git 跟踪的模板 = 唯一真源）。
$kwEntry = $manifest | Where-Object { $_.RelPath -like '*keywords_jax.txt' }
$kwTemplate = Join-Path $root "scripts\keywords_jax.txt.template"
if ((Test-File $kwEntry) -ne "ok") {
    if (Test-Path $kwTemplate) {
        Write-Host "    恢复 keywords_jax.txt（自建关键词文件，来自仓库模板）..."
        New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
        # 不能用 Copy-Item：git checkout 在 Windows 上可能把模板 LF 转成 CRLF（core.autocrlf），
        # 模板哈希按 LF 记录，CRLF 会让 27B 变 29B 导致校验必败。读入后统一按 LF 写出。
        $kwContent = [IO.File]::ReadAllText($kwTemplate) -replace "`r`n", "`n"
        [IO.File]::WriteAllText((Join-Path $root $kwEntry.RelPath), $kwContent, (New-Object System.Text.UTF8Encoding($false)))
        if ((Test-File $kwEntry) -ne "ok") {
            Write-Error "keywords_jax.txt 恢复后哈希仍不符（期望 $($kwEntry.Sha)）—— 中止"
            exit 1
        }
    } else {
        Write-Error "缺少 $kwTemplate 且模型目录内无有效 keywords_jax.txt —— 中止"
        exit 1
    }
}

Write-Host "[3/3] 检查 jniLibs（AAR 通常已含 .so；缺则解压 android 包）"
$jniRoot = "$root\app\src\main\jniLibs"
if (-not (Test-Path $jniRoot)) {
    Write-Host "    需要 jniLibs？如构建报错 'library libsherpa-onnx-jni.so not found'，手动执行："
    Write-Host "      wget https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.4/sherpa-onnx-v1.13.4-android.tar.bz2"
    Write-Host "      解压后把 jniLibs/ 目录复制到 app/src/main/jniLibs"
} else {
    Write-Host "    jniLibs 已存在"
}

Write-Host ""
Write-Host "完成（全部输入已通过 SHA-256 校验）。assets 内容："
Get-ChildItem "$root\app\src\main\assets" -Recurse -File | ForEach-Object { Write-Host "  $($_.FullName)" }
