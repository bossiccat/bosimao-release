# ============================================================
# install-scheduled-tasks.ps1 — 注册贾克斯 watchdog 计划任务（幂等）
# 任务 1: Jax-Watchdog-AtStartup   开机自启（AtStartup）
# 任务 2: Jax-Watchdog-Every5Min   每 5 分钟自愈（RepetitionInterval）
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts/install-scheduled-tasks.ps1            # 注册
#   powershell -ExecutionPolicy Bypass -File scripts/install-scheduled-tasks.ps1 -Uninstall  # 卸载
# 说明：用 Register-ScheduledTask cmdlet（不用 schtasks）；注册前先删同名任务（幂等）
#       本文件必须保持 UTF-8 with BOM（PS 5.1 中文解析依赖 BOM）
# ============================================================
param(
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$Watchdog = Join-Path $PSScriptRoot "jax-watchdog.ps1"
$ActionArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$Watchdog`""
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $ActionArgs
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

function Remove-JaxTasks {
    $existing = Get-ScheduledTask -TaskName "Jax-*" -ErrorAction SilentlyContinue
    foreach ($t in $existing) {
        Write-Host ("[unreg] " + $t.TaskName)
        Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false
    }
}

if ($Uninstall) {
    Write-Host "==> 卸载 Jax-* 计划任务"
    Remove-JaxTasks
    $left = @(Get-ScheduledTask -TaskName "Jax-*" -ErrorAction SilentlyContinue)
    if ($left.Count -eq 0) { Write-Host "[ok] 已全部卸载" } else { Write-Host "[!] 仍有残留任务" }
    exit 0
}

Write-Host "==> 注册 Jax-* 计划任务（幂等：先删同名再注册）"
Remove-JaxTasks

# ---- 任务 1：开机自启 ----
$trigStartup = New-ScheduledTaskTrigger -AtStartup
$task1 = New-ScheduledTask -Action $Action -Trigger $trigStartup -Principal $Principal `
    -Description "贾克斯 watchdog：开机即自愈（模型/后端/relay_client 掉线自动拉起）"
Register-ScheduledTask -TaskName "Jax-Watchdog-AtStartup" -InputObject $task1 -Force | Out-Null
Write-Host "[ok] Jax-Watchdog-AtStartup 已注册"

# ---- 任务 2：每 5 分钟 ----
# 注意：[TimeSpan]::MaxValue 会生成 P99999999DT23H59M59S 非法 XML 导致注册失败；
#        Task Scheduler 的合法上限为 P3650D（≈10 年），用 3650 天实现"长期重复"
$trigRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$task2 = New-ScheduledTask -Action $Action -Trigger $trigRepeat -Principal $Principal `
    -Description "贾克斯 watchdog：每 5 分钟检查三件套健康并自愈（含中继假死检测）"
Register-ScheduledTask -TaskName "Jax-Watchdog-Every5Min" -InputObject $task2 -Force | Out-Null
Write-Host "[ok] Jax-Watchdog-Every5Min 已注册（每 5 分钟，长期重复）"

# ---- 输出注册结果 ----
Write-Host ""
Write-Host "=========== Jax-* 计划任务状态 ==========="
$jaxTasks = @(Get-ScheduledTask -TaskName "Jax-*" -ErrorAction SilentlyContinue)
foreach ($t in $jaxTasks) {
    $next = if ($t.NextRunTime -and $t.NextRunTime -gt (Get-Date 1900,1,1)) { $t.NextRunTime.ToString("yyyy-MM-dd HH:mm") } else { "N/A" }
    Write-Host ("{0,-28} {1,-8} next={2}" -f $t.TaskName, $t.State, $next)
}
Write-Host "========================================="
