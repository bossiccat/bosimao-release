# 模型服务常驻启动脚本（llama-server :19080, engine=comni）
# 用法: powershell -ExecutionPolicy Bypass -File scripts/start-model.ps1
# 说明: Start-Process 独立窗口启动，不随本 PowerShell 会话退出而终止；
#       若需开机自启，可在任务计划程序中注册本脚本。
$ErrorActionPreference = "Stop"

$ServerBin = "C:\Users\Administrator\AppData\Local\Comni\_internal\resources\build\bin\Release\llama-server.exe"
$Model = "D:\models\MiniCPM-o-4_5-gguf\MiniCPM-o-4_5-Q4_K_M.gguf"
$Port = 19080
$Ctx = 4096   # B1 结论：8192 爆 12G 显存，必须 4096
$Ngl = 99
$LogDir = Join-Path $PSScriptRoot "..\logs"
$Log = Join-Path $LogDir "llama-server-$Port.log"

if (-not (Test-Path $ServerBin)) { Write-Error "引擎不存在: $ServerBin"; exit 1 }
if (-not (Test-Path $Model))     { Write-Error "模型不存在: $Model";     exit 1 }
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

# 已在跑则不重复启动
try {
    $health = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 3
    if ($health.StatusCode -eq 200) {
        Write-Host "[ok] 模型服务已在运行: http://127.0.0.1:$Port/health"
        exit 0
    }
} catch { }

$args = @(
    "--host", "127.0.0.1",
    "--port", "$Port",
    "--model", $Model,
    "-ngl", "$Ngl",
    "--ctx-size", "$Ctx"
)
Write-Host "==> 启动: $ServerBin $($args -join ' ')"
# 2026-08-13 无窗口修复：llama-server 是 console 子系统，-WindowStyle Hidden 不可靠，
# 改用 .NET ProcessStartInfo.CreateNoWindow（等价 CREATE_NO_WINDOW）真正无窗口启动。
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $ServerBin
$psi.Arguments = ($args -join ' ')
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$p = [System.Diagnostics.Process]::Start($psi)
# 后台异步写出 stdout/stderr 到日志，避免管道缓冲阻塞模型加载
Start-Job -ScriptBlock { param($r, $path) while ($null -ne ($line = $r.ReadLine())) { Add-Content -Path $path -Value $line } } -ArgumentList $p.StandardOutput, $Log | Out-Null
Start-Job -ScriptBlock { param($r, $path) while ($null -ne ($line = $r.ReadLine())) { Add-Content -Path $path -Value $line } } -ArgumentList $p.StandardError, "$Log.err" | Out-Null
Write-Host "PID=$($p.Id) 日志=$Log"
Write-Host "==> 等待 /health (模型加载约 20s-5min) ..."
$deadline = (Get-Date).AddMinutes(5)
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) {
            Write-Host "[ok] 模型服务就绪: $($r.Content)"
            exit 0
        }
    } catch { Start-Sleep -Seconds 3 }
}
Write-Error "[err] 5 分钟内未就绪，查看日志: $Log"
exit 1
