# Run this only in an interactive Windows customer or acceptance session.
# It performs read-only evidence capture for exactly three historical tasks.
# Do not redirect this script's output to a public or shared location.
#
# 变更记录（2026-09-19，claim `windows-popup-free`）
# ---------------------------------------------------------------------------
# 上一版有一个**真缺陷**，它让这个"官方取证脚本"分不清两件完全不同的事：
#
#   :32  `catch [Microsoft.Management.Infrastructure.CimException]`
#
#   `Get-ScheduledTask -TaskName <不存在的名字>` 实际抛的是
#   `Microsoft.PowerShell.Cmdletization.Cim.CimJobException`，而它与
#   `CimException` **没有派生关系**：
#       [Microsoft.Management.Infrastructure.CimException]::
#           IsAssignableFrom([Microsoft.PowerShell.Cmdletization.Cim.CimJobException])  ->  False
#   于是那个类型化 catch **永不匹配**，"任务不存在"全部掉进最后的裸 catch，
#   被记成 `query_error` —— 与"查询失败/权限不足/提供程序故障"**不可区分**。
#   实测（修复前，本机）：三只 legacy 任务全部 `status = "query_error"`，
#   `not_found` 分支是死代码。
#
# 后果：本脚本会同时犯两个方向的错 ——
#   · 把"任务不存在"报成"查询失败" ⇒ 永远无法得出 ABSENT，retirement 被无谓阻塞；
#   · 而一旦有人"修"成只看异常类型，就换成另一种脆弱：提供程序换文案/换异常类型时
#     又会静默退化。
#
# 本版因此**不靠单一信号**，而要求**两条互相独立的信号同向**，并强制带阴阳对照：
#
#   信号 1（另一条查询路径）：`Get-ScheduledTask -TaskPath '\'` 的全量枚举里没有该名字，
#                             且枚举非空**且**包含阳性对照（证明这条路径真的通）。
#   信号 2（阴阳对照）：一个**确定不存在**的编造名字走**同一段代码**，
#                       与 legacy 名字在 (异常类型, HResult, not-found 文案) 上**不可区分**。
#   信号 3（显式文案，可得时必须一致）：提供程序是否给出 not-found 文案。
#                       若提供程序已不再给这种文案（连编造名字也不给），
#                       则记为 `not_available`，此时判据退化为 1∧2，且记录里如实写明。
#
# 判定闸门（全部成立才允许 ABSENT，否则一律 INACCESSIBLE）：
#   阳性对照必须 EXISTS  ⇒ 这条查询通道真的通
#   阴性对照必须 ABSENT  ⇒ "不存在"这件事真的能被表达出来
#   三只 legacy 必须同时具备 信号1 ∧ 信号2（∧ 信号3 若可得）
# 这与 docs/OPS-002 的纪律一致：EXISTS / ABSENT / INACCESSIBLE 三态，
# 其余一律 INACCESSIBLE，**不许**把模糊当结论。
#
# 只读：不注册、不删除、不修改、不启用/禁用任何任务。
# 不调用 `schtasks.exe`（本机程序黑名单会拒；且它按代码页输出中文）。
#
# `-Provider` / `-Enumerator` 是**给测试用的接缝**：默认走真实 cmdlet，
# 测试可注入假提供程序来验证"文案变了 / 异常类型变了"时判据仍不退化。
# 生产调用不需要传它们。

[CmdletBinding()]
param(
    [string]$OutFile = $null,
    [scriptblock]$Provider = $null,
    [scriptblock]$Enumerator = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# stdout 必须是 UTF-8：提供程序的错误原文是中文，重定向后按 ANSI 输出会被消费方读成乱码。
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
$OutputEncoding = New-Object System.Text.UTF8Encoding $false

$legacyNames = @(
    'Jax-Watchdog-AtStartup',
    'Jax-Watchdog-Every5Min',
    'jax-watchdog'
)
# 阴性对照：一个肯定不存在的名字。它必须与 legacy 名字走**同一段代码**。
$absentControlName = 'Jax-Definitely-Absent-Control-9f3a2b'
# 判据是"提供程序自己给出的 not-found 文案"，不是 HRESULT
# （实测 HRESULT 是 0x80131501，泛型 CLR 异常，不含 not-found 语义）。
$notFoundPattern = '找不到任何匹配|No matching MSFT_ScheduledTask'

if (-not $Provider) {
    $Provider = {
        param([string]$Name)
        Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop
    }
}
if (-not $Enumerator) {
    $Enumerator = {
        @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop)
    }
}

function Get-ErrorHResult($err) {
    try {
        if ($err.Exception -and $err.Exception.HResult) {
            return ('0x{0:X8}' -f $err.Exception.HResult)
        }
        if ($err.Exception -and $err.Exception.InnerException -and $err.Exception.InnerException.HResult) {
            return ('0x{0:X8}' -f $err.Exception.InnerException.HResult)
        }
    } catch {
        # 读不到就如实返回 null，不猜
    }
    return $null
}

function New-Record([string]$name) {
    return [ordered]@{
        task_name                  = $name
        task_path                  = '\'
        status                     = 'inaccessible'
        exact_query_ok             = $false
        error_type                 = $null
        error_hresult              = $null
        error_message              = $null
        not_found_marker           = $false
        in_root_enumeration        = $false
        signal_1_not_in_enum       = $false
        signal_2_matches_absent_ct = $false
        signals_used               = @()
        state                      = $null
        principal_logon_type       = $null
        action_execute             = $null
        action_arguments_present   = $false
        last_task_result           = $null
        last_run_time_utc          = $null
        next_run_time_utc          = $null
    }
}

# ---------------------------------------------------------------------------
# 枚举一次根任务（信号 1 的基准）。失败 ⇒ 整份结论只能 INACCESSIBLE。
# ---------------------------------------------------------------------------
$enumerationOk = $false
$rootNames = @()
try {
    $rootTasks = & $Enumerator
    $rootNames = @($rootTasks | ForEach-Object { $_.TaskName })
    $enumerationOk = $true
} catch {
    $enumerationOk = $false
    $rootNames = @()
}

$enumerationUsable = [bool]($enumerationOk -and $rootNames.Count -gt 0)

# 阳性对照：枚举里确实存在的第一个根任务。没有它，"查不到"和"通道坏了"分不开。
$positiveName = if ($enumerationUsable) { [string]$rootNames[0] } else { $null }

function Invoke-Probe([string]$name) {
    $rec = New-Record $name
    $rec.in_root_enumeration = [bool]($rootNames -contains $name)
    try {
        $t = & $Provider $name
        $rec.exact_query_ok = $true
        $rec.status = 'exists'
        $rec.state = [string]$t.State
        $rec.principal_logon_type = [string]$t.Principal.LogonType
        $rec.action_execute = @($t.Actions | ForEach-Object { $_.Execute }) -join ';'
        $rec.action_arguments_present = [bool](
            @($t.Actions | Where-Object { -not [string]::IsNullOrWhiteSpace($_.Arguments) }).Count -gt 0
        )
        try {
            $info = Get-ScheduledTaskInfo -TaskName $name -TaskPath '\' -ErrorAction Stop
            $rec.last_task_result = $info.LastTaskResult
            $rec.last_run_time_utc = if ($info.LastRunTime) { $info.LastRunTime.ToUniversalTime().ToString('o') } else { $null }
            $rec.next_run_time_utc = if ($info.NextRunTime) { $info.NextRunTime.ToUniversalTime().ToString('o') } else { $null }
        } catch {
            # 任务拿到了但 info 拿不到：只影响这几个统计字段，不改存在性判定
            $rec.last_task_result = $null
        }
    } catch {
        $ex = $_.Exception
        $msg = ($ex.Message -replace '\s+', ' ')
        $rec.error_type = $ex.GetType().FullName
        $rec.error_hresult = Get-ErrorHResult $_
        $rec.error_message = $msg.Substring(0, [Math]::Min(300, $msg.Length))
        $rec.not_found_marker = [bool]($msg -match $notFoundPattern)
        # 此处**不**下结论：结论要等阴性对照回来以后由闸门统一给。
        $rec.status = 'threw'
    }
    return $rec
}

# legacy 先查，阴性对照后查（同一段代码路径）
$records = @()
foreach ($n in $legacyNames) { $records += ,(Invoke-Probe $n) }
$recAbsentControl = Invoke-Probe $absentControlName
$recPositive = if ($positiveName) { Invoke-Probe $positiveName } else { $null }

# ⚠️ 分类必须覆盖**全部**记录，包括两条对照自身。
#    漏掉对照的后果不是"少一行"，而是闸门永远拿不到 negative_control_absent
#    ⇒ 结论恒为 INACCESSIBLE。fail-closed 是对的，但那就等于这个脚本永远说不出 ABSENT。
$allRecords = @(@($records) + @($recAbsentControl) + @($recPositive) | Where-Object { $_ })

# ---------------------------------------------------------------------------
# 信号 2：与"确定不存在的编造名字"是否**不可区分**
#   （比较 异常类型 / HResult / not-found 文案 三者；三者全同才叫不可区分）
#   阴性对照与自己比必然全同 ⇒ 它自己的 signal_2 同向。
# ---------------------------------------------------------------------------
foreach ($r in $allRecords) {
    if ($r.status -ne 'threw') { continue }
    $same = (
        $r.error_type -eq $recAbsentControl.error_type -and
        $r.error_hresult -eq $recAbsentControl.error_hresult -and
        $r.not_found_marker -eq $recAbsentControl.not_found_marker
    )
    $r.signal_2_matches_absent_ct = [bool]($same -and $recAbsentControl.status -eq 'threw')
}

# 信号 1：不在枚举里（且枚举这条路真的通）
foreach ($r in $allRecords) {
    $r.signal_1_not_in_enum = [bool]($enumerationUsable -and -not $r.in_root_enumeration)
}

# 信号 3 是否可得：连编造名字都拿不到 not-found 文案 ⇒ 提供程序换过文案，信号 3 不可用
$signal3Available = [bool]($recAbsentControl.status -eq 'threw' -and $recAbsentControl.not_found_marker)

# ---------------------------------------------------------------------------
# 判定：逐个名字 + 总闸门
# ---------------------------------------------------------------------------
foreach ($r in $allRecords) {
    $signals = @()
    if ($r.signal_1_not_in_enum) { $signals += 'not_in_root_enumeration' }
    if ($r.signal_2_matches_absent_ct) { $signals += 'indistinguishable_from_absent_control' }
    if ($signal3Available -and $r.not_found_marker) { $signals += 'provider_not_found_marker' }
    $r.signals_used = $signals

    $enoughSignals = [bool](
        $r.signal_1_not_in_enum -and
        $r.signal_2_matches_absent_ct -and
        ($r.not_found_marker -or -not $signal3Available)
    )
    $r.status = if ($r.exact_query_ok) { 'exists' } elseif ($enoughSignals) { 'not_found' } else { 'inaccessible' }
}

$positiveExists = [bool]($recPositive -and $recPositive.status -eq 'exists')
$absentControlAbsent = [bool]($recAbsentControl.status -eq 'not_found')
$legacyRecords = @($records | Where-Object { $legacyNames -contains $_.task_name })
$legacyNotInEnum = [bool](-not ($legacyRecords | Where-Object { $_.in_root_enumeration }))
$legacyAllAbsent = [bool](
    @($legacyRecords | Where-Object { $_.status -eq 'not_found' }).Count -eq $legacyNames.Count
)
$legacyAllIndistinguishable = [bool](
    @($legacyRecords | Where-Object { $_.signal_2_matches_absent_ct }).Count -eq $legacyNames.Count
)
# "真的找到了一个 legacy 任务" 必须是一个**可区分的**结论。
# 否则它会掉进 INACCESSIBLE，与"查询失败"混在一起 —— 那正是本文件要消灭的模糊，
# 而且 next-steps 第 2 步（发现任务就跑 retirement 脚本）会因此永远不触发。
$legacyAnyPresent = [bool](
    @($legacyRecords | Where-Object { $_.status -eq 'exists' }).Count -gt 0
)

$gate = [ordered]@{
    enumeration_usable          = $enumerationUsable
    root_task_count             = $rootNames.Count
    signal3_message_available   = $signal3Available
    positive_control_exists     = $positiveExists
    negative_control_absent     = $absentControlAbsent
    legacy_all_absent           = $legacyAllAbsent
    legacy_all_indistinguishable_from_negative_control = $legacyAllIndistinguishable
    legacy_none_in_root_enumeration = $legacyNotInEnum
    legacy_any_present          = $legacyAnyPresent
}
$gate['verdict'] = if ($legacyAnyPresent) {
    # 有 legacy 任务真的还在 ⇒ 这是一个**可区分**的结论，不是一个"查询异常"。
    # next-steps 第 2 步据此触发 retirement，然后再跑一次本脚本取 after 记录。
    'LEGACY_TASKS_PRESENT'
} elseif (
    $gate.enumeration_usable -and $gate.positive_control_exists -and
    $gate.negative_control_absent -and $gate.legacy_all_absent -and
    $gate.legacy_all_indistinguishable_from_negative_control -and
    $gate.legacy_none_in_root_enumeration
) { 'LEGACY_TASKS_ABSENT' } else { 'INACCESSIBLE' }

$obj = [ordered]@{
    probe_utc        = (Get-Date).ToUniversalTime().ToString('o')
    script           = 'windows-popup-field-evidence.ps1'
    legacy_task_names = $legacyNames
    positive_control = $positiveName
    negative_control = $absentControlName
    gate             = $gate
    records          = $allRecords
}

$json = $obj | ConvertTo-Json -Depth 5

if ($OutFile) {
    # 显式 UTF-8：路径可能含非 ASCII（如 贾克斯·星核），不靠代码页
    $json | Out-File -FilePath $OutFile -Encoding utf8
}

Write-Output $json
# 机器可消费标记：始终是最后一行。只有这一行的值才是结论。
Write-Output ("TASK_ABSENCE_EVIDENCE=" + $gate['verdict'])
