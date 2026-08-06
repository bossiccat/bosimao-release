# Jax Voice M1 依赖下载脚本（Windows PowerShell）
# 用法：在 mobile-app/ 目录执行  powershell -ExecutionPolicy Bypass -File scripts/fetch-deps.ps1
# 作用：
#   1) sherpa-onnx Android AAR（含 Java/Kotlin API + JNI） → app/libs/
#   2) KWS 唤醒词模型（wenetspeech-3.3M） → app/src/main/assets/
#   3)（可选，若 AAR 未带 jni）解压 android 包 jniLibs → app/src/main/jniLibs/
# 版本锚定：sherpa-onnx 1.13.4（与 API 代码核对过）

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$aarUrl   = "https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.4/sherpa-onnx-1.13.4.aar"
$modelUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2"

New-Item -ItemType Directory -Force -Path "$root\app\libs" | Out-Null
New-Item -ItemType Directory -Force -Path "$root\app\src\main\assets" | Out-Null

Write-Host "[1/3] 下载 sherpa-onnx AAR -> app/libs ..."
if (-not (Test-Path "$root\app\libs\sherpa-onnx-1.13.4.aar")) {
    Invoke-WebRequest -Uri $aarUrl -OutFile "$root\app\libs\sherpa-onnx-1.13.4.aar"
} else {
    Write-Host "    已存在，跳过"
}

Write-Host "[2/3] 下载 KWS 模型 -> app/src/main/assets ..."
$modelTmp = Join-Path $env:TEMP "sherpa-kws.tar.bz2"
if (-not (Test-Path "$root\app\src\main\assets\sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01\tokens.txt")) {
    Invoke-WebRequest -Uri $modelUrl -OutFile $modelTmp
    tar -xf $modelTmp -C "$root\app\src\main\assets"
    Remove-Item $modelTmp
} else {
    Write-Host "    模型已存在，跳过"
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
Write-Host "完成。assets 内容："
Get-ChildItem "$root\app\src\main\assets" -Recurse -File | ForEach-Object { Write-Host "  $($_.FullName)" }
