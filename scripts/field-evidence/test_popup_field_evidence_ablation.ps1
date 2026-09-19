# 消融测试：windows-popup-field-evidence.ps1 的存在性判据**不得静默退化**。
#
# 为什么需要这个文件
# ------------------
# 旧版脚本的判据是"catch 住特定异常类型"。它的失败方式不是报错，而是**沉默**：
# `CimJobException` 不派生自 `CimException`，类型化 catch 永不匹配，
# 于是"任务不存在"被记成 `query_error` —— 分不清"没有任务"和"查不了"。
# 实测（修复前）：三只 legacy 全部 `status = "query_error"`，`not_found` 是死代码。
#
# 所以"修好"不能只看今天跑出 ABSENT。必须证明：**当提供程序换了文案、
# 换了异常类型、或者整条通道坏掉时，结论不会悄悄变成错误的 ABSENT。**
#
# 做法：脚本留了两个接缝 `-Provider` / `-Enumerator`（生产调用不传），
# 本文件往里面注入假的提供程序，逐个场景断言**期望结论**。
# 其中最关键的两个场景是"必须仍然 ABSENT"（不退化）与
# "必须不许 ABSENT"（fail-closed）。
#
# 只读：本文件不注册/删除/修改任何计划任务；真实场景只读本机任务表。
#
# 用法（在 Windows PowerShell 里）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\field-evidence\test_popup_field_evidence_ablation.ps1

[CmdletBinding()]
param(
    [string]$ScriptPath = $null,
    [string]$OutFile = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# stdout 必须是 UTF-8：否则中文（脚本路径里的"监视app"、SKIP 说明）在重定向到文件/管道后
# 会按 ANSI 输出，被消费方按 UTF-8 读就成了乱码 —— 本文件存在的意义就是让输出可信。
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
$OutputEncoding = New-Object System.Text.UTF8Encoding $false

# $PSScriptRoot 在 param 默认值里不总可靠 ⇒ 在函数体里解析（本文件在 scripts/field-evidence/ 下）
if (-not $ScriptPath) {
    $repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $ScriptPath = Join-Path $repoRoot 'windows-popup-field-evidence.ps1'
}

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    throw "找不到被测脚本: $ScriptPath"
}
$ScriptPath = (Resolve-Path -LiteralPath $ScriptPath).Path

$positiveName = 'Fake-Positive-Control-Task'
$results = @()

# 假任务对象**内联**在每个 scriptblock 里（不跨作用域调函数）：
# 传进另一个脚本文件的 scriptblock 不保证看得见本文件定义的函数。

# 假枚举：固定两条根任务，阳性对照必然是第一条。不依赖本机任务表。
$fakeEnumerator = {
    @(
        [pscustomobject]@{ TaskName = 'Fake-Positive-Control-Task' },
        [pscustomobject]@{ TaskName = 'Fake-Other-Task' }
    )
}

function Invoke-Scenario {
    param(
        [string]$Name,
        [string]$Expect,
        [scriptblock]$Provider,
        [scriptblock]$Enumerator
    )
    $out = & $ScriptPath -Provider $Provider -Enumerator $Enumerator 2>&1
    $marker = @($out | Where-Object { "$_" -like 'TASK_ABSENCE_EVIDENCE=*' })
    $actual = if ($marker.Count -gt 0) { "$($marker[-1])".Split('=')[1] } else { '<NO_MARKER>' }
    $pass = ($actual -eq $Expect)
    $script:results += [pscustomobject]@{ scenario = $Name; expect = $Expect; actual = $actual; pass = $pass }
    Write-Output ("ABLATION {0,-34} expect={1,-22} actual={2,-22} => {3}" -f $Name, $Expect, $actual, $(if ($pass) { 'PASS' } else { 'FAIL' }))
}

# 机器可消费的行必须**字节层面**纯 ASCII：本仓工作区路径含中文（…\监视app\…），
# 原样写出在 GBK 控制台上就是乱码，"哪一份脚本被测"这条关键信息直接不可读
# （实测踩过：SCRIPT_UNDER_TEST=…\鐩戣app\…）。这里把非 ASCII 字符无损转义成 \uXXXX，
# 由 test_windows_popup_field_evidence_contract.py 解回来比对。
$ScriptUnderTestEscaped = [regex]::Replace($ScriptPath, '[^\x20-\x7E]', {
    param($m) '\u' + ('{0:x4}' -f [int][char]$m.Value)
})
Write-Output ("SCRIPT_UNDER_TEST=" + $ScriptUnderTestEscaped)
Write-Output ("SCRIPT_UNDER_TEST_SHA256=" + (Get-FileHash -LiteralPath $ScriptPath -Algorithm SHA256).Hash.ToLower())
Write-Output ""

# ---------------------------------------------------------------------------
# 基线：真实 cmdlet。本机三只 legacy 已由安装器清理 ⇒ 期望 ABSENT。
#   ScheduledTasks 模块只在 Windows 上有；非 Windows（如 ubuntu CI 上的 pwsh）
#   跳过这一条，其余 7 条照跑 —— 判据逻辑本身与平台无关。
#   SKIP 必须**显式打出来**，不能安静地少一行。
# ---------------------------------------------------------------------------
$hasScheduledTasks = [bool](Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)
if ($hasScheduledTasks) {
    Invoke-Scenario -Name 'baseline_real_provider' -Expect 'LEGACY_TASKS_ABSENT' `
        -Provider { param($Name) Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop } `
        -Enumerator { @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop) }
} else {
    Write-Output "ABLATION baseline_real_provider             expect=LEGACY_TASKS_ABSENT    actual=SKIP                    => SKIP (no ScheduledTasks module; Windows-only)"
}

# ---------------------------------------------------------------------------
# 场景 A：提供程序**换了文案**（不再给 not-found 字样），但缺啥抛啥的行为不变。
#   期望：仍然 ABSENT（靠信号1 枚举 + 信号2 与阴性对照不可区分），且
#         gate.signal3_message_available 应为 false —— 判据要如实降级而不是假装。
# ---------------------------------------------------------------------------
Invoke-Scenario -Name 'provider_message_changed' -Expect 'LEGACY_TASKS_ABSENT' `
    -Provider {
        param($Name)
        if ($Name -eq 'Fake-Positive-Control-Task') { return [pscustomobject]@{ TaskName = $Name; TaskPath = '\'; State = 'Ready'; Principal = [pscustomobject]@{ LogonType = 'Interactive' }; Actions = @([pscustomobject]@{ Execute = 'C:\fake.exe'; Arguments = '' }) } }
        throw [System.InvalidOperationException]::new("provider vNext: item '$Name' not retrievable")
    } -Enumerator $fakeEnumerator

# ---------------------------------------------------------------------------
# 场景 B：提供程序**换了异常类型**（不再是 CimJobException），文案也换了。
#   期望：仍然 ABSENT。这正是"只把 catch 类型换一下"修不好、且会再次退化的那条。
# ---------------------------------------------------------------------------
Invoke-Scenario -Name 'provider_exception_type_changed' -Expect 'LEGACY_TASKS_ABSENT' `
    -Provider {
        param($Name)
        if ($Name -eq 'Fake-Positive-Control-Task') { return [pscustomobject]@{ TaskName = $Name; TaskPath = '\'; State = 'Ready'; Principal = [pscustomobject]@{ LogonType = 'Interactive' }; Actions = @([pscustomobject]@{ Execute = 'C:\fake.exe'; Arguments = '' }) } }
        throw [System.TimeoutException]::new("upstream query timeout for '$Name'")
    } -Enumerator $fakeEnumerator

# ---------------------------------------------------------------------------
# 场景 C（fail-closed 核心）：提供程序对**所有**名字都失败，包括阳性对照。
#   期望：INACCESSIBLE。若这里报 ABSENT，那就是"通道坏了却宣布任务不存在"。
# ---------------------------------------------------------------------------
Invoke-Scenario -Name 'provider_uniform_failure' -Expect 'INACCESSIBLE' `
    -Provider { param($Name) throw [System.InvalidOperationException]::new("everything is broken: '$Name'") } `
    -Enumerator $fakeEnumerator

# ---------------------------------------------------------------------------
# 场景 D：枚举这条路坏了（阳性对照拿不到）。
#   期望：INACCESSIBLE —— 信号1 的基准没了，不许用剩下的半个判据下 ABSENT。
# ---------------------------------------------------------------------------
Invoke-Scenario -Name 'enumeration_broken' -Expect 'INACCESSIBLE' `
    -Provider {
        param($Name)
        if ($Name -eq 'Fake-Positive-Control-Task') { return [pscustomobject]@{ TaskName = $Name; TaskPath = '\'; State = 'Ready'; Principal = [pscustomobject]@{ LogonType = 'Interactive' }; Actions = @([pscustomobject]@{ Execute = 'C:\fake.exe'; Arguments = '' }) } }
        throw [System.InvalidOperationException]::new("no matching item '$Name'")
    } -Enumerator { throw [System.InvalidOperationException]::new('enumerator unavailable') }

# ---------------------------------------------------------------------------
# 场景 E：枚举**空**（0 条根任务）。空表不能当"没有 legacy 任务"的证据。
#   期望：INACCESSIBLE。
# ---------------------------------------------------------------------------
Invoke-Scenario -Name 'enumeration_empty' -Expect 'INACCESSIBLE' `
    -Provider {
        param($Name)
        if ($Name -eq 'Fake-Positive-Control-Task') { return [pscustomobject]@{ TaskName = $Name; TaskPath = '\'; State = 'Ready'; Principal = [pscustomobject]@{ LogonType = 'Interactive' }; Actions = @([pscustomobject]@{ Execute = 'C:\fake.exe'; Arguments = '' }) } }
        throw [System.InvalidOperationException]::new("no matching item '$Name'")
    } -Enumerator { @() }

# ---------------------------------------------------------------------------
# 场景 F：**真的有 legacy 任务还在**（回归场景）。
#   期望：LEGACY_TASKS_PRESENT —— 可区分，不许混进 INACCESSIBLE，
#         否则 next-steps 第 2 步（发现即 retirement）永远不触发。
# ---------------------------------------------------------------------------
Invoke-Scenario -Name 'legacy_task_present' -Expect 'LEGACY_TASKS_PRESENT' `
    -Provider {
        param($Name)
        if ($Name -eq 'Fake-Positive-Control-Task' -or $Name -eq 'Jax-Watchdog-Every5Min') {
            return [pscustomobject]@{ TaskName = $Name; TaskPath = '\'; State = 'Ready'; Principal = [pscustomobject]@{ LogonType = 'Interactive' }; Actions = @([pscustomobject]@{ Execute = 'C:\fake.exe'; Arguments = '' }) }
        }
        throw [System.InvalidOperationException]::new("no matching item '$Name'")
    } -Enumerator $fakeEnumerator

# ---------------------------------------------------------------------------
# 场景 G：提供程序对不存在的名字**不抛异常**而是返回空对象（"静默给空"型提供程序）。
#   期望：绝不是 ABSENT（此时 exact_query_ok=true，会被记成 exists ⇒ PRESENT）。
#         把"空"当"不存在"是我们这一天一直在消灭的那类错。
# ---------------------------------------------------------------------------
Invoke-Scenario -Name 'provider_returns_empty_object' -Expect 'LEGACY_TASKS_PRESENT' `
    -Provider {
        param($Name)
        if ($Name -eq 'Fake-Positive-Control-Task') { return [pscustomobject]@{ TaskName = $Name; TaskPath = '\'; State = 'Ready'; Principal = [pscustomobject]@{ LogonType = 'Interactive' }; Actions = @([pscustomobject]@{ Execute = 'C:\fake.exe'; Arguments = '' }) } }
        return [pscustomobject]@{ TaskName = $Name; TaskPath = '\'; State = ''; Principal = $null; Actions = @() }
    } -Enumerator $fakeEnumerator

$failed = @($results | Where-Object { -not $_.pass })
$summary = if ($failed.Count -eq 0) { 'ALL_PASS' } else { 'HAS_FAILURE' }

Write-Output ""
Write-Output ("ABLATION_SUMMARY={0} total={1} passed={2} failed={3}" -f $summary, $results.Count, ($results.Count - $failed.Count), $failed.Count)
foreach ($f in $failed) {
    Write-Output ("ABLATION_FAILED {0} expect={1} actual={2}" -f $f.scenario, $f.expect, $f.actual)
}

if ($OutFile) {
    ($results | ConvertTo-Json -Depth 3) | Out-File -FilePath $OutFile -Encoding utf8
}

if ($failed.Count -gt 0) { exit 1 } else { exit 0 }
