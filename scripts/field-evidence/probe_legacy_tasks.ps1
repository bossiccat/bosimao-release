# 只读：判定三只 legacy 计划任务是否存在。
#
# 为什么要重写而不是直接用 windows-popup-field-evidence.ps1：
#   那个脚本对"任务不存在"只 catch 了 `Microsoft.Management.Infrastructure.CimException`，
#   而 `Get-ScheduledTask -TaskName <不存在的名字>` 实际抛的是
#   `Microsoft.PowerShell.Cmdletization.Cim.CimJobException`（子类没被 catch 到），
#   于是**"不存在"被错报成 query_error**。按 docs 的纪律：
#     EXISTS = 成功拿到 task 对象；ABSENT = 明确的 not-found 结果；其余 = INACCESSIBLE。
#   query_error 既不是 EXISTS 也不是 ABSENT，不能拿来下结论。
#
# 本脚本用**名字级对照**把这个歧义消掉：
#   · 阳性对照：一个**确实存在**的根任务（Get-ScheduledTask 枚举里取第一个）
#   · 阴性对照：一个**肯定不存在**的编造名字
#   只有当"编造名字"和"三只 legacy 名字"给出**同一种** not-found 结果、
#   且阳性对照给出**不同**的成功结果时，才允许判 ABSENT。
#
# 只读：不注册、不删除、不修改任何任务。

param(
    [string]$OutFile = "$env:LOCALAPPDATA\Temp\jax-pe\legacy_tasks_probe.json"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$legacyNames = @('Jax-Watchdog-AtStartup', 'Jax-Watchdog-Every5Min', 'jax-watchdog')
$fakeName    = 'Jax-Definitely-Absent-Control-9f3a2b'

function Get-HResult($err) {
    try {
        if ($err.Exception -and $err.Exception.HResult) { return ('0x{0:X8}' -f $err.Exception.HResult) }
        if ($err.Exception -and $err.Exception.InnerException -and $err.Exception.InnerException.HResult) {
            return ('0x{0:X8}' -f $err.Exception.InnerException.HResult)
        }
    } catch { }
    return $null
}

function Probe-One([string]$name, [string[]]$allRootNames) {
    $rec = [ordered]@{
        task_name          = $name
        exact_query_ok     = $false
        error_type         = $null
        error_hresult      = $null
        error_message      = $null
        enumeration_hit    = ($allRootNames -contains $name)
        not_found_marker   = $false
        classification     = 'INACCESSIBLE'
    }
    try {
        $t = Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction Stop
        $rec.exact_query_ok = $true
        $rec.classification = 'EXISTS'
        $rec | Add-Member -NotePropertyName state -NotePropertyValue ([string]$t.State) -Force
        $rec | Add-Member -NotePropertyName action_count -NotePropertyValue (@($t.Actions).Count) -Force
    } catch {
        $msg = ($_.Exception.Message -replace '\s+', ' ')
        $rec.error_type    = $_.Exception.GetType().FullName
        $rec.error_hresult = Get-HResult $_
        $rec.error_message = $msg.Substring(0, [Math]::Min(200, $msg.Length))
        # 判据不是 HRESULT（这里是 0x80131501，泛型 CLR 异常，不含 not-found 语义），
        # 而是**提供程序自己给出的 not-found 文案** + **与阴性对照不可区分**。
        # 两者都成立，且阳性对照成功，才允许判 ABSENT。
        if ($msg -match '找不到任何匹配|No matching MSFT_ScheduledTask') {
            $rec.not_found_marker = $true
            $rec.classification   = 'ABSENT'
        } else {
            $rec.classification   = 'INACCESSIBLE'
        }
    }
    return $rec
}

$rootTasks = @()
try {
    $all = @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop)
    $rootTasks = @($all | ForEach-Object { $_.TaskName })
} catch {
    $rootTasks = @()
}

# 阳性对照：枚举里确实存在的第一个根任务
$positiveName = if ($rootTasks.Count -gt 0) { $rootTasks[0] } else { $null }

$records = @()
foreach ($n in $legacyNames) { $records += (Probe-One $n $rootTasks) }
$recFake = Probe-One $fakeName $rootTasks
$records += $recFake
$recPos = if ($positiveName) { Probe-One $positiveName $rootTasks } else { $null }
if ($recPos) { $records += $recPos }

# 枚举侧交叉：三只 legacy 名字是否出现在根任务枚举里
$legacyEnum = [ordered]@{}
foreach ($n in $legacyNames) { $legacyEnum[$n] = ($rootTasks -contains $n) }

# ── 判定闸门：必须三个条件同时成立才允许说 ABSENT ──
#   (1) 阳性对照 EXISTS（证明这条查询通道真的通）
#   (2) 三只 legacy 全部带 not_found_marker（提供程序明确说"找不到任何匹配"）
#   (3) 三只 legacy 与阴性对照在 (error_type, classification, not_found_marker) 上**不可区分**
$gate = [ordered]@{
    positive_control_exists = [bool]($recPos -and $recPos.classification -eq 'EXISTS')
    negative_control_absent = [bool]($recFake.classification -eq 'ABSENT')
    legacy_all_absent       = [bool](@($records | Where-Object { $legacyNames -contains $_.task_name -and $_.classification -eq 'ABSENT' }).Count -eq $legacyNames.Count)
    legacy_indistinguishable_from_fake = $true
    legacy_none_in_enumeration = [bool](-not ($legacyEnum.Values -contains $true))
}
foreach ($r in @($records | Where-Object { $legacyNames -contains $_.task_name })) {
    if ($r.error_type -ne $recFake.error_type -or $r.classification -ne $recFake.classification -or $r.not_found_marker -ne $recFake.not_found_marker) {
        $gate.legacy_indistinguishable_from_fake = $false
    }
}
$gate['verdict'] = if (
    $gate.positive_control_exists -and $gate.negative_control_absent -and
    $gate.legacy_all_absent -and $gate.legacy_indistinguishable_from_fake -and
    $gate.legacy_none_in_enumeration
) { 'LEGACY_TASKS_ABSENT' } else { 'INACCESSIBLE' }

$obj = [ordered]@{
    probe_utc            = (Get-Date).ToUniversalTime().ToString('o')
    schedule_service     = (Get-Service Schedule | Select-Object -ExpandProperty Status).ToString()
    root_task_count      = $rootTasks.Count
    root_task_names      = $rootTasks
    positive_control     = $positiveName
    negative_control     = $fakeName
    legacy_enumeration   = $legacyEnum
    gate                 = $gate
    records              = $records
}
$obj | ConvertTo-Json -Depth 5 | Out-File -FilePath $OutFile -Encoding utf8
Write-Output ("WROTE " + $OutFile)
