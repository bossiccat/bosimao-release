# Jax Voice M1 依赖下载脚本（Windows PowerShell）
# 用法：在 mobile-app/ 目录执行  powershell -ExecutionPolicy Bypass -File scripts/fetch-deps.ps1
# 作用：
#   1) sherpa-onnx Android AAR（含 Java/Kotlin API + JNI） -> app/libs/
#   2) KWS 唤醒词模型（wenetspeech-3.3M） -> app/src/main/assets/
#   3)（可选，若 AAR 未带 jni）解压 android 包 jniLibs -> app/src/main/jniLibs/
# 版本锚定：sherpa-onnx 1.13.4（与 API 代码核对过）

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$aarUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.4/sherpa-onnx-1.13.4.aar"
$modelUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2"
$downloadTimeoutSec = 300
$maxDownloadAttempts = 3
$heartbeatIntervalSec = 30
$expectedAarSha256 = "03f9c4df965f21c71269365a7951a7f23b5696fddd093fa318c80d65550ab780"
$expectedModelSha256 = "b2f7c89690dc8ce4c6ed6afeab7cd800c36ad1421fb6b6302b4a4b194cf7f35f"

function Get-RequiredFileHash {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-ExpectedHash {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $hash = Get-RequiredFileHash -Path $Path
    if ($hash -ne $ExpectedSha256) {
        throw "[$Label] SHA256 mismatch expected=$ExpectedSha256 actual=$hash"
    }
    return $hash
}

function Get-RemoteAsset {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    $partial = "$Destination.partial"
    Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue

    for ($attempt = 1; $attempt -le $maxDownloadAttempts; $attempt++) {
        Write-Host "[$Label] download attempt $attempt of $maxDownloadAttempts (timeout=${downloadTimeoutSec}s)"
        $job = Start-Job -ScriptBlock {
            param($DownloadUri, $DownloadPath, $TimeoutSec)
            $ProgressPreference = "SilentlyContinue"
            Invoke-WebRequest -Uri $DownloadUri -OutFile $DownloadPath -TimeoutSec $TimeoutSec -UseBasicParsing
        } -ArgumentList $Uri, $partial, $downloadTimeoutSec

        $deadline = (Get-Date).AddSeconds($downloadTimeoutSec)
        $nextHeartbeat = (Get-Date).AddSeconds($heartbeatIntervalSec)
        while ($job.State -eq "Running" -and (Get-Date) -lt $deadline) {
            if ((Get-Date) -ge $nextHeartbeat) {
                $bytes = 0
                if (Test-Path -LiteralPath $partial) {
                    $bytes = (Get-Item -LiteralPath $partial).Length
                }
                Write-Host "[$Label] heartbeat attempt=$attempt bytes=$bytes deadline=$($deadline.ToString('o'))"
                $nextHeartbeat = (Get-Date).AddSeconds($heartbeatIntervalSec)
            }
            Start-Sleep -Seconds 2
        }

        if ($job.State -eq "Running") {
            Stop-Job -Job $job -ErrorAction SilentlyContinue
            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            Write-Warning "[$Label] attempt $attempt timed out after ${downloadTimeoutSec}s"
            continue
        }

        $jobOutput = Receive-Job -Job $job -ErrorVariable downloadError 2>&1 | Out-String
        $jobFailed = $job.State -eq "Failed" -or $downloadError.Count -gt 0
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        if ($jobFailed -or -not (Test-Path -LiteralPath $partial -PathType Leaf)) {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            Write-Warning "[$Label] attempt $attempt failed: $jobOutput"
            continue
        }

        try {
            $hash = Assert-ExpectedHash -Path $partial -ExpectedSha256 $ExpectedSha256 -Label $Label
        } catch {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            throw
        }
        Move-Item -LiteralPath $partial -Destination $Destination -Force
        $bytes = (Get-Item -LiteralPath $Destination).Length
        Write-Host "[$Label] SHA256=$hash bytes=$bytes"
        return
    }

    throw "[$Label] failed after $maxDownloadAttempts attempts; no dependency file was accepted"
}

New-Item -ItemType Directory -Force -Path "$root\app\libs" | Out-Null
New-Item -ItemType Directory -Force -Path "$root\app\src\main\assets" | Out-Null

Write-Host "[1/3] 下载 sherpa-onnx AAR -> app/libs ..."
$aarPath = "$root\app\libs\sherpa-onnx-1.13.4.aar"
if (-not (Test-Path -LiteralPath $aarPath -PathType Leaf)) {
    Get-RemoteAsset -Uri $aarUrl -Destination $aarPath -Label "sherpa-aar" -ExpectedSha256 $expectedAarSha256
} else {
    $hash = Assert-ExpectedHash -Path $aarPath -ExpectedSha256 $expectedAarSha256 -Label "sherpa-aar"
    Write-Host "[sherpa-aar] cache hit SHA256=$hash"
}

Write-Host "[2/3] 下载 KWS 模型 -> app/src/main/assets ..."
$modelTmp = Join-Path $env:TEMP "sherpa-kws.tar.bz2"
$modelTokens = "$root\app\src\main\assets\sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01\tokens.txt"
if (-not (Test-Path -LiteralPath $modelTokens -PathType Leaf)) {
    Get-RemoteAsset -Uri $modelUrl -Destination $modelTmp -Label "sherpa-kws-model" -ExpectedSha256 $expectedModelSha256
    try {
        tar -xf $modelTmp -C "$root\app\src\main\assets"
        if ($LASTEXITCODE -ne 0) { throw "tar extraction failed with exit code $LASTEXITCODE" }
    } finally {
        Remove-Item -LiteralPath $modelTmp -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "[sherpa-kws-model] cache hit tokens SHA256=$(Get-RequiredFileHash -Path $modelTokens)"
}

Write-Host "[3/3] 检查 jniLibs（AAR 通常已含 .so；缺则解压 android 包）"
$jniRoot = "$root\app\src\main\jniLibs"
if (Test-Path -LiteralPath $jniRoot) {
    Write-Host "    jniLibs 已存在"
}
if (-not (Test-Path -LiteralPath $jniRoot)) {
    Write-Host "    jniLibs 不在本地；仅当构建报告缺少 libsherpa-onnx-jni.so 时才需要补充。"
}

$assetFileCount = (Get-ChildItem -LiteralPath "$root\app\src\main\assets" -Recurse -File | Measure-Object).Count
Write-Host "[fetch-deps] completed asset_file_count=$assetFileCount"
