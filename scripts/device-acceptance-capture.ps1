<#
.SYNOPSIS
    jax-pet (com.jax.voice) on-device acceptance capture pipeline.

.DESCRIPTION
    One-shot, idempotent evidence capture over wireless ADB:
      connect -> reverse tunnel -> clock sync -> silent baseline
      -> session establish -> N-second capture -> gain recompute -> export logs

    Manual equivalent: docs/reports/device-capture-sop-2026-09-07.md

    Windows PowerShell 5.1 compatible:
      - no '??' operator, no ternary '?:'  (both are PS7+ only)
      - all conditionals use explicit if/else

.PARAMETER Device
    Target as <ip>:<port>. Wireless debugging port rotates on every enable;
    scan with tmp/adb_scan_full.py or read it off the phone screen.

.PARAMETER SampleSeconds
    Capture window length in seconds. Default 30.

.PARAMETER OutDir
    Output root. A per-run subfolder <OutDir>/run-yyyyMMdd-HHmmss is created,
    plus a 'latest' copy marker. Default 'out' under the repo root.

.EXAMPLE
    .\scripts\device-acceptance-capture.ps1 -Device "100.75.48.99:46813" -SampleSeconds 30 -OutDir "out"
#>
[CmdletBinding()]
param(
    [string]$Device        = "100.75.48.99:46813",
    [int]   $SampleSeconds = 30,
    [string]$OutDir        = "out"
)

$ErrorActionPreference = "Continue"
# Keep redirected native output readable (logcat may contain non-ASCII).
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ---------------------------------------------------------------- constants
$BaselineSeconds = 10
$RepoRoot        = Split-Path -Parent $PSScriptRoot
$AdbCandidates   = @(
    (Join-Path $RepoRoot "tmp/task6-tools/platform-tools/adb.exe"),
    "C:/Users/Administrator/Downloads/jax-build/android-sdk/platform-tools/adb.exe"
)
# Class name != logcat TAG. Using class names here yields zero matches.
$LogcatTags = @(
    "VoiceService:*",      # VoiceForegroundService
    "VoiceSessionCoord:*", # VoiceSessionCoordinator
    "BargeInCtrl:*",       # BargeInController
    "CaptureGain:*",
    "RtcCustomAudio:*"
)

# ------------------------------------------------------------------- state
$script:AdbPath  = $null
$script:RunDir   = $null
$script:LogcatProc = $null
$script:Aborted  = $false

# ----------------------------------------------------------------- helpers
function Write-Step {
    param([string]$No, [string]$Text)
    Write-Host ""
    Write-Host ("=== [STEP {0}] {1} ===" -f $No, $Text) -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Text)
    Write-Host ("  [OK]   {0}" -f $Text) -ForegroundColor Green
}

function Write-Info {
    param([string]$Text)
    Write-Host ("  [INFO] {0}" -f $Text) -ForegroundColor Gray
}

function Write-Warn {
    param([string]$Text)
    Write-Host ("  [WARN] {0}" -f $Text) -ForegroundColor Yellow
}

function Fail {
    param([string]$Text, [string[]]$Advice)
    Write-Host ("  [FAIL] {0}" -f $Text) -ForegroundColor Red
    if ($Advice -and $Advice.Count -gt 0) {
        Write-Host "         处理建议：" -ForegroundColor Yellow
        foreach ($a in $Advice) { Write-Host ("           - {0}" -f $a) -ForegroundColor Yellow }
    }
}

function Invoke-Adb {
    param([Parameter(Mandatory = $true)][string[]]$AdbArgs)
    $global:LASTEXITCODE = 0
    $raw = & $script:AdbPath $AdbArgs 2>&1
    $code = $LASTEXITCODE
    $text = ""
    if ($raw) { $text = ($raw | Out-String).Trim() }
    return [PSCustomObject]@{ ExitCode = $code; Output = $text }
}

function Stop-Logcat {
    if ($script:LogcatProc -ne $null) {
        if (-not $script:LogcatProc.HasExited) {
            Write-Info "停止后台 logcat 采集 (pid $($script:LogcatProc.Id))"
            Stop-Process -Id $script:LogcatProc.Id -Force -ErrorAction SilentlyContinue
        }
        $script:LogcatProc = $null
    }
}

function Write-ClockSample {
    param([string]$Path)
    $pc = [DateTimeOffset]::UtcNow
    $r  = Invoke-Adb @("-s", $Device, "shell", "date", "-u", "+%s")
    $devEpoch = 0
    if ($r.ExitCode -eq 0 -and $r.Output -match '^\d+$') { $devEpoch = [int64]$r.Output }
    $offsetMs = 0
    if ($devEpoch -gt 0) { $offsetMs = ($pc.ToUnixTimeMilliseconds() - ($devEpoch * 1000)) }
    $lines = @(
        "device=$Device",
        "device_epoch=$devEpoch",
        "pc_epoch=$($pc.ToUnixTimeSeconds())",
        "offset_ms=$offsetMs   # pc - device; logcat(-v time) uses device clock, bridge/backend use PC clock"
    )
    Set-Content -Path $Path -Value $lines -Encoding UTF8
    return $offsetMs
}

# ------------------------------------------------------- STEP 0  preflight
Write-Step "0" "预检（adb 可执行文件 / 参数 / 输出目录）"

if ($Device -notmatch '^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5}$') {
    Fail "Device 参数格式非法：$Device（应为 <ip>:<port>）" @(
        "正确示例：-Device ""100.75.48.99:46813""",
        "端口每次开启无线调试都会变，不要沿用上一次的值"
    )
    exit 2
}
Write-Ok ("目标设备 {0}" -f $Device)

foreach ($c in $AdbCandidates) {
    if (Test-Path $c) { $script:AdbPath = $c; break }
}
if ($script:AdbPath -eq $null) {
    $cmd = Get-Command adb -ErrorAction SilentlyContinue
    if ($cmd -ne $null) { $script:AdbPath = $cmd.Source }
}
if ($script:AdbPath -eq $null) {
    Fail "找不到 adb 可执行文件" @(
        "确认仓库内 tmp/task6-tools/platform-tools/adb.exe 存在，",
        "或把 Android SDK platform-tools 加进 PATH",
        "注意：报错「系统找不到指定的文件」是路径错；「连接被拒绝」是路径对但设备不通，处置不同"
    )
    exit 2
}
Write-Ok ("adb = {0}" -f $script:AdbPath)

if ([System.IO.Path]::IsPathRooted($OutDir)) {
    $absOut = $OutDir
} else {
    $absOut = Join-Path $RepoRoot $OutDir
}
$stamp       = Get-Date -Format "yyyyMMdd-HHmmss"
$script:RunDir = Join-Path $absOut ("run-" + $stamp)
try {
    New-Item -ItemType Directory -Path $script:RunDir -Force -ErrorAction Stop | Out-Null
} catch {
    Fail "无法创建输出目录：$script:RunDir（$($_.Exception.Message)）" @(
        "相对路径会相对仓库根解析：仓库根 = $RepoRoot",
        "绝对路径请写全，例如 -OutDir ""C:/temp/jax-capture""",
        "确认磁盘可写且路径不含非法字符"
    )
    exit 2
}
Write-Ok ("输出目录 {0}" -f $script:RunDir)
Write-Info "本脚本幂等：重复执行只新增 run-* 子目录，不覆盖历史证据"

# ------------------------------------------------------ STEP 1  adb connect
Write-Step "1" "连接设备（无线 ADB）"

$dev  = Invoke-Adb @("devices")
$already = $false
if ($dev.Output -match [regex]::Escape($Device)) {
    if ($dev.Output -match [regex]::Escape($Device) + "\s+device") { $already = $true }
}
if ($already) {
    Write-Ok ("已连接 {0}（跳过 connect，保持幂等）" -f $Device)
} else {
    $c = Invoke-Adb @("connect", $Device)
    Start-Sleep -Seconds 2
    $dev2 = Invoke-Adb @("devices")
    if ($dev2.Output -match ([regex]::Escape($Device) + "\s+device")) {
        Write-Ok ("connect 成功：{0}" -f $Device)
    } else {
        $detail = $c.Output
        if ([string]::IsNullOrWhiteSpace($detail)) { $detail = "(adb 无输出；可能是 adb 版本异常或被安全软件拦截)" }
        $advice = @(
            "adb 未连接：请在手机「开发者选项 → 无线调试」重新开启无线调试，并用配对码配对取得新端口",
            "10061（目标计算机积极拒绝）= 手机在线但该端口无监听，即无线调试未开；不是网络问题、不是 adb 缺失",
            "10060/超时 = Tailscale 不通，检查手机与 PC 是否在同一 tailnet",
            "unauthorized/offline = 手机上点「允许 USB 调试」；仍失败则 adb kill-server 后重连",
            "端口每次重开都变：可用 python tmp/adb_scan_full.py --host <ip> 并发扫描"
        )
        if ($detail -match "offline") {
            $advice = @("设备状态 offline：在手机上确认授权弹窗，或 adb kill-server 后重连") + $advice
        }
        Fail ("无法连接 {0}。adb 输出：{1}" -f $Device, $detail) $advice
        exit 3
    }
}

# ------------------------------------------- STEP 2  reverse tunnel (8443)
Write-Step "2" "建立反向隧道 tcp:8443 -> tcp:8000"

# The reverse binding lives on the transport and is dropped on every reconnect,
# so it must be re-created after EVERY connect, including a reused one.
$rv = Invoke-Adb @("-s", $Device, "reverse", "tcp:8443", "tcp:8000")
$rl = Invoke-Adb @("-s", $Device, "reverse", "--list")
if ($rl.Output -match "8443") {
    Write-Ok "reverse tcp:8443 tcp:8000 已生效"
} else {
    Fail "reverse 隧道未建立" @(
        "每次 adb connect 后都必须重建 reverse，它绑定在 transport 上",
        "隧道丢失的表现是手机端报 Failed to connect to localhost/127.0.0.1:8443",
        "若反复失败：确认本机 8000 端口有服务在听（backend），再重跑本脚本"
    )
    exit 3
}

# ---------------------------------------------------- STEP 3  clock sync
Write-Step "3" "对时基准（设备时钟 vs PC 时钟）"

$offsetMs = Write-ClockSample (Join-Path $script:RunDir "clocksync.txt")
Write-Ok ("对时偏移 offset_ms = {0}（已写入 clocksync.txt）" -f $offsetMs)
if ([Math]::Abs($offsetMs) -gt 1000) {
    Write-Warn "偏移超过 1000ms：跨设备时序判读不可信，建议先在手机开启「自动网络时间」再重跑"
} else {
    Write-Info "logcat(-v time) 用设备时钟；rtc_bridge/backend 日志用 PC 时钟；跨进程只能靠本偏移对齐"
}

# --------------------------------------------------- STEP 4  app pid check
Write-Step "4" "确认 App 进程与后端链路"

$pidR = Invoke-Adb @("-s", $Device, "shell", "pidof", "com.jax.voice")
$appPid = ""
if ($pidR.ExitCode -eq 0 -and $pidR.Output -match '\d+') { $appPid = $Matches[0] }
if ($appPid -eq "") {
    Write-Warn "com.jax.voice 当前未运行（采样期会先拉起，若拉不起则本轮无证据）"
} else {
    Write-Ok ("App pid = {0}" -f $appPid)
}

$threadFile = Join-Path $script:RunDir "proc-threads-before.txt"
Write-Info "线程普查仅在 App 已运行时有意义（泄漏类问题一眼可见）"
if ($appPid -eq "") {
    Set-Content -Path $threadFile -Value "# App not running before sampling - no thread census" -Encoding UTF8
} else {
    $censusCmd = "cat /proc/" + $appPid + "/task/*/comm"
    $thr = Invoke-Adb @("-s", $Device, "shell", $censusCmd)
    Set-Content -Path $threadFile -Value ("# captured before sampling`n" + $thr.Output) -Encoding UTF8
}

# ----------------------------------------------- STEP 5  silent baseline
$baselineTitle = ("静默基线采样（{0} 秒，请勿对手机说话）" -f $BaselineSeconds)
Write-Step "5" $baselineTitle

$baseLog = Join-Path $script:RunDir "logcat-baseline.txt"
$lcArgs  = @("-s", $Device, "logcat", "-v", "time") + $LogcatTags
$script:LogcatProc = Start-Process -FilePath $script:AdbPath -ArgumentList $lcArgs `
    -RedirectStandardOutput $baseLog -RedirectStandardError (Join-Path $script:RunDir "logcat-baseline.err.txt") `
    -PassThru -WindowStyle Hidden
Start-Sleep -Seconds $BaselineSeconds
Stop-Logcat

$baseCount = 0
if (Test-Path $baseLog) { $baseCount = (Get-Content $baseLog -ErrorAction SilentlyContinue | Measure-Object -Line).Lines }
if ($baseCount -eq 0) {
    Fail "静默基线 logcat 为空" @(
        "检查 App 是否已启动（STEP 4 提示未运行则先手动打开一次）",
        "确认 TAG 常量：VoiceForegroundService 的真 TAG 是 VoiceService，用类名 grep 一条都匹配不到",
        "确认 reverse 隧道在（STEP 2），否则 App 侧会一直报 8443 连不上",
        "设备 logcat 缓冲只留约 40~80 秒，事后补抓会全空；必须流式落盘"
    )
    $script:Aborted = $true
} else {
    Write-Ok ("静默基线采集完成：{0} 行 -> {1}" -f $baseCount, (Split-Path $baseLog -Leaf))
    Write-Info "判读：静默段 gate=true 占比应接近 0（历史故障值 98.8%）"
}

# --------------------------------------------- STEP 6  session + capture
if (-not $script:Aborted) {
    $captureTitle = ("建立会话并采集 {0} 秒（采样窗口内请对着手机说话 2~3 句）" -f $SampleSeconds)
    Write-Step "6" $captureTitle

    $null = Invoke-Adb @("-s", $Device, "shell", "am", "start", "-n", "com.jax.voice/.MainActivity")
    Start-Sleep -Seconds 3

    # uiautomator must dump to /sdcard; /dev/tty never returns the XML over adb.
    $null = Invoke-Adb @("-s", $Device, "shell", "uiautomator", "dump", "/sdcard/jax-window.xml")
    $xml = Invoke-Adb @("-s", $Device, "shell", "cat", "/sdcard/jax-window.xml")

    $tapped = $false
    if ($xml.Output -match 'resource-id="[^"]*btnTalk"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"') {
        $x1 = [int]$Matches[1]; $y1 = [int]$Matches[2]; $x2 = [int]$Matches[3]; $y2 = [int]$Matches[4]
        $cx = [int](($x1 + $x2) / 2); $cy = [int](($y1 + $y2) / 2)
        Write-Info ("btnTalk bounds=[{0},{1}][{2},{3}] -> tap ({4},{5})" -f $x1, $y1, $x2, $y2, $cx, $cy)
        $null = Invoke-Adb @("-s", $Device, "shell", "input", "tap", "$cx", "$cy")
        $tapped = $true
    } else {
        # Fallback: attribute order varies, retry with a looser pattern.
        if ($xml.Output -match 'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="[^"]*btnTalk"') {
            $x1 = [int]$Matches[1]; $y1 = [int]$Matches[2]; $x2 = [int]$Matches[3]; $y2 = [int]$Matches[4]
            $cx = [int](($x1 + $x2) / 2); $cy = [int](($y1 + $y2) / 2)
            Write-Info ("btnTalk(宽松匹配) -> tap ({0},{1})" -f $cx, $cy)
            $null = Invoke-Adb @("-s", $Device, "shell", "input", "tap", "$cx", "$cy")
            $tapped = $true
        }
    }

    if (-not $tapped) {
        Fail "未能在界面上定位 btnTalk（立即对话）" @(
            "确认已安装目标 APK 且是前台（STEP 2 的 8443 隧道不通时界面会停在错误态）",
            "btnTalk 才建会话并启动 RealCustomAudioSource；btnToggleListen 只启停 KWS，采集增益只发生在会话内",
            "不要发 keyevent 4（MainActivity 是根界面，BACK 会直接回桌面）",
            "按钮 bounds 会随 tvPhase 文案位移，dump 后要立刻 tap",
            "可手工点「立即对话」后重跑本脚本（-SkipTap 未实现，直接重跑即可）"
        )
        $script:Aborted = $true
    } else {
        Write-Ok "已触发 btnTalk，开始采集"
    }
}

if (-not $script:Aborted) {
    $capLog = Join-Path $script:RunDir "logcat-capture.txt"
    $script:LogcatProc = Start-Process -FilePath $script:AdbPath -ArgumentList $lcArgs `
        -RedirectStandardOutput $capLog -RedirectStandardError (Join-Path $script:RunDir "logcat-capture.err.txt") `
        -PassThru -WindowStyle Hidden

    Write-Host ""
    Write-Host ("  >>> 采集进行中，请对着手机说话（剩余 {0}s）..." -f $SampleSeconds) -ForegroundColor Magenta
    for ($i = $SampleSeconds; $i -gt 0; $i--) {
        Start-Sleep -Seconds 1
        if (($i % 10) -eq 0 -or $i -eq 1) { Write-Host ("      剩余 {0}s" -f $i) -ForegroundColor DarkGray }
    }
    Stop-Logcat

    $capCount = 0
    if (Test-Path $capLog) { $capCount = (Get-Content $capLog -ErrorAction SilentlyContinue | Measure-Object -Line).Lines }
    if ($capCount -eq 0) {
        Fail "会话采样 logcat 为空" @(
            "App 可能在采样期重启：后台 logcat --pid 会因 pid 变化而全空，本脚本已改用 TAG 过滤，若仍为空则是 App 崩了",
            "确认 btnTalk 真的建起了会话（logcat 里应有 VoiceSessionCoord 的 start accepted）"
        )
        $script:Aborted = $true
    } else {
        Write-Ok ("会话采集完成：{0} 行 -> {1}" -f $capCount, (Split-Path $capLog -Leaf))
    }
}

# ------------------------------------------------- STEP 7  gain recompute
if (-not $script:Aborted) {
    Write-Step "7" "复算采集增益（CaptureGainStage）"

    $all = Get-Content (Join-Path $script:RunDir "logcat-capture.txt") -ErrorAction SilentlyContinue
    # Historical trap: the deploy helper prints logs twice -> dedupe before counting.
    $lvl = $all | Where-Object { $_ -match "lvl raw=" } | Sort-Object -Unique
    $lvl | Set-Content -Path (Join-Path $script:RunDir "lvl.txt") -Encoding UTF8

    if ($lvl.Count -eq 0) {
        Fail "未采集到任何电平样本（lvl raw= ...）" @(
            "电平日志周期 500ms；样本为 0 说明采集不在会话内（btnTalk 没点成功）或 TAG 过滤写错",
            "期望条数参考：30s 采样 ≈ 60 条 lvl 行"
        )
        $script:Aborted = $true
    } else {
        $gateOpen = ($lvl | Where-Object { $_ -match "gate=true" }).Count
        $rate = 0
        if ($lvl.Count -gt 0) { $rate = [Math]::Round(100.0 * $gateOpen / $lvl.Count, 1) }

        $outs = @()
        foreach ($line in $lvl) {
            if ($line -match "out=(\d+)") { $outs += [int]$Matches[1] }
        }
        $outMax = 0; $outMed = 0
        if ($outs.Count -gt 0) {
            $sorted = $outs | Sort-Object
            $outMax = $sorted[-1]
            $outMed = $sorted[[int][Math]::Floor($sorted.Count / 2)]
        }

        $summary = @(
            "lvl_samples=$($lvl.Count)",
            "gate_open=$gateOpen",
            "gate_open_rate_pct=$rate",
            "out_median=$outMed",
            "out_max=$outMax",
            "# target out RMS window: 2000~5000",
            "# historical faults: v1 out_max=9702 (clipping) / silent out=177 (gate stuck open)"
        )
        $summary | Set-Content -Path (Join-Path $script:RunDir "gain-summary.txt") -Encoding UTF8

        Write-Ok ("电平样本 {0} 条，gate 开放率 {1}%，out 中位 {2} / 最大 {3}" -f $lvl.Count, $rate, $outMed, $outMax)
        if ($outMax -gt 5000) { Write-Warn "out 超过 5000（上界）：存在削波，增益仍需下调" }
        if ($rate -gt 50)     { Write-Warn "gate 开放率 > 50%：疑噪声门常开（历史故障值 98.8%），检查静默基线" }
        Write-Info "详细判据见 docs/reports/device-capture-sop-2026-09-07.md §6.4"
    }
}

# ------------------------------------------------------ STEP 8  export logs
Write-Step "8" "导出日志与进程快照"

$procFile = Join-Path $script:RunDir "proc.txt"
$ps = Invoke-Adb @("-s", $Device, "shell", "ps -A | grep com.jax.voice")
$pidR2 = Invoke-Adb @("-s", $Device, "shell", "pidof", "com.jax.voice")
$pidNow = ""
if ($pidR2.Output -match '\d+') { $pidNow = $Matches[0] }
$thr2 = ""
if ($pidNow -ne "") {
    $thr2 = (Invoke-Adb @("-s", $Device, "shell", "cat /proc/$pidNow/task/*/comm | sort | uniq -c")).Output
}
Set-Content -Path $procFile -Value @("# ps", $ps.Output, "# thread census (jax-rtc-capture should be 1)", $thr2) -Encoding UTF8
Write-Ok "进程/线程快照 -> proc.txt"

$diag = Invoke-Adb @("-s", $Device, "shell", "run-as com.jax.voice cat files/diag_log.txt")
$diagFile = Join-Path $script:RunDir "diag_log.txt"
Set-Content -Path $diagFile -Value $diag.Output -Encoding UTF8
if ($diag.Output -match "run-as|not debuggable|Permission denied") {
    Write-Warn "diag_log 取不到（release 包 run-as 不可用）：改用 MainActivity 长按连接状态区弹窗导出"
    Write-Info "DiagLog 从不输出 logcat，是 BargeIn 事件唯一可靠来源"
} else {
    $nInter = ([regex]::Matches($diag.Output, "interrupt source=user_voice")).Count
    $nIgn   = ([regex]::Matches($diag.Output, "voice ignored")).Count
    Write-Ok ("diag_log.txt 已导出：interrupt(user_voice)={0}，voice ignored={1}" -f $nInter, $nIgn)
    Write-Info "修复前基线：interrupt=32 / 播放段 1.0~1.8s 碎片；修复后应 interrupt=0 且播放段完整（约 2112ms）"
}

# --------------------------------------------------------------- wrap-up
Write-Host ""
Write-Host "=================== 取证结束 ===================" -ForegroundColor Cyan
Write-Host ("输出目录：{0}" -f $script:RunDir) -ForegroundColor White

$latest = Join-Path $absOut "latest-run.txt"
Set-Content -Path $latest -Value $script:RunDir -Encoding UTF8
Write-Host ("latest 指针：{0}" -f $latest) -ForegroundColor Gray

$missing = @()
foreach ($f in @("clocksync.txt", "logcat-capture.txt", "proc.txt", "diag_log.txt", "lvl.txt", "gain-summary.txt")) {
    if (-not (Test-Path (Join-Path $script:RunDir $f))) { $missing += $f }
}
if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host "缺失项（对应判据必须标「未实测」，禁止用推测值填空）：" -ForegroundColor Yellow
    foreach ($m in $missing) { Write-Host ("  - {0}" -f $m) -ForegroundColor Yellow }
}

if ($script:Aborted) {
    Write-Host ""
    Write-Host "本轮取证未完整完成，请按上方 [FAIL] 的处理建议修复后重跑（脚本幂等，可直接重跑）。" -ForegroundColor Red
    exit 1
}
Write-Host ""
Write-Host "证据包完整。按 docs/reports/device-capture-sop-2026-09-07.md §8 做交付前自检。" -ForegroundColor Green
exit 0
