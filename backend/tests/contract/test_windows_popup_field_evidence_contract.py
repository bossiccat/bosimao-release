r"""契约：`windows-popup-field-evidence.ps1` 必须能分清"任务不存在"与"查询失败"。

背景（2026-09-19，claim `windows-popup-free`）
--------------------------------------------
这个脚本是该 claim 的**官方现场取证脚本**，而它有一个真缺陷：

    :32  `catch [Microsoft.Management.Infrastructure.CimException]`

`Get-ScheduledTask -TaskName <不存在的名字>` 实际抛的是
`Microsoft.PowerShell.Cmdletization.Cim.CimJobException`，**不派生**自 `CimException`：

    [Microsoft.Management.Infrastructure.CimException]::
        IsAssignableFrom([Microsoft.PowerShell.Cmdletization.Cim.CimJobException])  ->  False

于是那个类型化 catch **永不匹配**，"任务不存在"全部掉进最后的裸 catch 记成
`query_error` —— 与"查询失败/权限不足/提供程序故障"**不可区分**。
实测（修复前，本机）：三只 legacy 任务全部 `status = "query_error"`，
`not_found` 分支是死代码。

危害是双向的：
    · 把"任务不存在"报成"查询失败" ⇒ 永远得不出 ABSENT，retirement 被无谓阻塞；
    · 而若"修"成只认异常类型，就换成另一种脆弱：提供程序换文案/换异常类型时又静默退化。

本文件的立场
------------
所以断言不能是"今天跑出了 ABSENT"。必须同时守两件事：

    1) **判据是两条互相独立的信号同向**，且强制带阴阳对照（单信号一律不许下 ABSENT）；
    2) **换文案 / 换异常类型 / 通道坏掉时，结论不会静默变成错误的 ABSENT**。

第 (2) 条由 `scripts/field-evidence/test_popup_field_evidence_ablation.ps1` 用注入的
假提供程序逐场景验证（含一个"全部失败却必须判 INACCESSIBLE"的 fail-closed 场景）。
本文件负责：
    · **跨平台**地（含 ubuntu CI）钉住脚本的结构不变式 —— 守据、对照、非 ASCII 安全、
      禁用 schtasks、机器可消费标记；
    · 有 PowerShell 时，真跑那套消融并**要求全绿**（Windows 上 8/8；
      非 Windows 上 ScheduledTasks 模块不存在，基线那条显式 SKIP，其余 7 条照跑）；
    · 做**变异验证**：把判据改回"只看一种信号"，消融必须变红。

没有 PowerShell 的环境会**显式 skip 行为用例**（`-rsk` 可见），但结构用例始终执行 ——
因为一个"跑不了就静默通过"的契约测试，正是本仓库这一天一直在消灭的东西。

注意：结构断言只看**非注释行**。脚本头部注释里必须能写下旧缺陷的原文
（`catch [CimException]`、`schtasks.exe`），否则下一个读它的人会重新踩进去。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "windows-popup-field-evidence.ps1"
ABLATION = REPO_ROOT / "scripts" / "field-evidence" / "test_popup_field_evidence_ablation.ps1"

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


def _code() -> str:
    """只取非注释行：注释里可以（也应当）引用旧缺陷原文。"""
    return "\n".join(
        line for line in _source().splitlines() if not line.lstrip().startswith("#")
    )


def _run_ablation(script: Path | None = None,
                  ablation: Path = ABLATION) -> subprocess.CompletedProcess[str]:
    argv = [POWERSHELL or "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(ablation)]
    if script is not None:
        argv += ["-ScriptPath", str(script)]
    return subprocess.run(
        argv, cwd=str(REPO_ROOT), capture_output=True,
        encoding="utf-8", errors="replace", timeout=300,
    )


# ---------------------------------------------------------------------------
# 机器可消费行的口径
#
# 这些行会被 CI / 别的 agent 按行读。要求是**字节层面**纯 ASCII：
# 本仓工作区路径含中文（`…\监视app\…`），原样写出来在 GBK 控制台上就是乱码，
# "哪一份脚本被测"这种关键信息直接不可读。所以路径在脚本里被转义成 `\uXXXX`，
# 断言的对象必须是**运行时真的发出来的行**，不是源码里的字面量 ——
# 只扫字面量的断言会在字面量恰好是 ASCII、而运行时值是中文时恒绿（这个洞已经踩过）。
# ---------------------------------------------------------------------------
_PS_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_MACHINE_PREFIXES = ("ABLATION ", "ABLATION_SUMMARY=", "ABLATION_FAILED ",
                     "SCRIPT_UNDER_TEST=")


def _unescape_ps(text: str) -> str:
    r"""把消融脚本写出的 `\uXXXX` 还原回原字符。"""
    return _PS_UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _machine_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()
            if line.strip().startswith(_MACHINE_PREFIXES)]


def _non_ascii_machine_lines(text: str) -> list[str]:
    return [line for line in _machine_lines(text) if not line.isascii()]


# ---------------------------------------------------------------------------
# 结构不变式（跨平台，始终执行）：守据本身
# ---------------------------------------------------------------------------
def test_absence_is_never_decided_by_exception_type() -> None:
    """不得用「catch 住某个异常类型」来判"任务不存在"。

    这正是原缺陷的形状：类型化 catch 与真实抛出的类型没有派生关系，于是判据是死代码，
    而失败方式是**沉默**（报成 query_error），不是报错。
    """
    code = _code()

    typed_catches = re.findall(r"catch\s*\[([^\]]+)\]", code)
    offenders = [c for c in typed_catches if "CimException" in c or "CimJobException" in c]
    assert not offenders, (
        f"存在按异常类型判定的 catch: {offenders}。"
        "提供程序换异常类型就会静默退化 —— 必须按两条独立信号判定。"
    )
    assert not typed_catches, f"代码里不应有任何类型化 catch，实际: {typed_catches}"


def test_absence_requires_two_independent_signals_and_controls() -> None:
    """判 ABSENT 必须同时用上：枚举信号 + 与阴性对照不可区分，并强制阴阳对照。"""
    code = _code()

    # 信号 1：另一条查询路径（全量枚举）里没有该名字
    assert "signal_1_not_in_enum" in code, "缺少枚举侧信号"
    # 信号 2：与"确定不存在的编造名字"走同一段代码且不可区分
    assert "signal_2_matches_absent_ct" in code, "缺少阴性对照不可区分性判据"

    m = re.search(r"\$enoughSignals\s*=\s*\[bool\]\((.*?)\n\s*\)", code, re.S)
    assert m, "找不到 enoughSignals 定义"
    body = m.group(1)
    assert "signal_1_not_in_enum" in body, body
    assert "signal_2_matches_absent_ct" in body, body
    assert len(re.findall(r"\s-and\s", body)) >= 2, (
        f"两条信号必须是合取（AND）且各自参与，实际: {body}"
    )

    # 阴阳对照必须内置且出现在输出里
    assert "Jax-Definitely-Absent-Control-9f3a2b" in code, "缺阴性对照"
    assert "positive_control" in code and "negative_control" in code, "对照未暴露到输出"
    assert "positive_control_exists" in code, "阳性对照未参与判定"
    assert "signal3_message_available" in code, (
        "缺信号 3 可得性标记：提供程序换文案时必须如实降级，而不是假装信号还在"
    )


def test_verdict_is_fail_closed_and_machine_consumable() -> None:
    """必须给出机器可消费的三态标记；缺对照 / 通道坏 ⇒ INACCESSIBLE，不是 ABSENT。"""
    code = _code()

    assert "TASK_ABSENCE_EVIDENCE=" in code, "缺机器可消费标记"

    idx = code.index("$gate['verdict']")
    block = code[idx: idx + 900]
    for required in ("positive_control_exists", "negative_control_absent",
                     "legacy_all_absent", "legacy_none_in_root_enumeration",
                     "enumeration_usable",
                     "legacy_all_indistinguishable_from_negative_control"):
        assert required in block, f"verdict 判定缺必要条件: {required}"
    assert "INACCESSIBLE" in block and "LEGACY_TASKS_ABSENT" in block, block
    # "真的找到了任务"必须是可区分的结论，不能混进 INACCESSIBLE
    assert "LEGACY_TASKS_PRESENT" in block, "缺 PRESENT 态：发现任务会掉进 INACCESSIBLE"


def test_script_is_read_only_and_avoids_schtasks() -> None:
    """只读且不得调用 schtasks.exe（本机程序黑名单会拒，且它按代码页输出）。"""
    code = _code()

    for forbidden in ("schtasks", "Register-ScheduledTask", "Unregister-ScheduledTask",
                      "Set-ScheduledTask", "Start-ScheduledTask", "Stop-ScheduledTask",
                      "Disable-ScheduledTask", "Enable-ScheduledTask", "Remove-Item",
                      "Stop-Process", "Start-Process"):
        assert forbidden not in code, f"代码里出现写操作/禁用工具: {forbidden}"


def test_non_ascii_safe_output_and_redacted() -> None:
    """非 ASCII 路径安全：显式 UTF-8 落盘；且不采集任何原始元数据（只读脱敏）。"""
    code = _code()

    assert "Out-File" in code and "utf8" in code, "落盘必须显式 UTF-8"
    # BOM 必须真的在字节里（以 utf-8-sig 读会把它吃掉，所以要读原始字节）
    assert SCRIPT.read_bytes()[:3] == b"\xef\xbb\xbf", (
        "缺 UTF-8 BOM：PS 5.1 会按 ANSI 读，中文注释变乱码并把解析搞坏"
    )
    # 只读脱敏：不采集窗口标题/用户名一类原始元数据
    for forbidden in ("GetWindowText", "whoami", "UserName", "net user"):
        assert forbidden not in code, f"出现原始元数据采集: {forbidden}"


def test_ablation_suite_declares_all_required_scenarios() -> None:
    """消融套件必须同时包含「必须仍 ABSENT」与「必须不许 ABSENT」两类场景。

    只有前者会漏掉 fail-closed；只有后者会漏掉静默退化。
    """
    src = ABLATION.read_text(encoding="utf-8-sig")

    required = {
        "provider_message_changed": "LEGACY_TASKS_ABSENT",              # 换文案不得退化
        "provider_exception_type_changed": "LEGACY_TASKS_ABSENT",       # 换异常类型不得退化
        "provider_uniform_failure": "INACCESSIBLE",                     # 通道坏必须 fail-closed
        "enumeration_broken": "INACCESSIBLE",
        "enumeration_empty": "INACCESSIBLE",
        "legacy_task_present": "LEGACY_TASKS_PRESENT",
        "provider_returns_empty_object": "LEGACY_TASKS_PRESENT",        # "空"不等于"不存在"
    }
    for name, expect in required.items():
        m = re.search(rf"-Name\s+'{re.escape(name)}'\s+-Expect\s+'([A-Z_]+)'", src)
        assert m, f"消融套件缺场景: {name}"
        assert m.group(1) == expect, f"{name} 期望值应为 {expect}，实际 {m.group(1)}"


def test_ablation_machine_consumable_lines_are_ascii_only() -> None:
    r"""机器可消费的行必须是纯 ASCII（源码字面量这一层）。

    这些行会被 CI / 别的 agent 按行读；一旦掺进中文，在 GBK 控制台上被解码
    就会变成乱码（实测踩过："被测脚本:" → "琚祴鑴氭湰"），
    于是"哪一份脚本被测"这种关键信息就不可读了。

    **注意这一条只扫字面量，不足以证明运行时输出安全**：路径是插值出来的，
    字面量 ASCII 而运行时值含中文时它照样恒绿 —— 那个洞已经踩过一次
    （`SCRIPT_UNDER_TEST=` + `$ScriptPath`，本仓工作区名含中文）。
    运行时的那一层由 `test_ablation_all_scenarios_pass_on_shipped_script` 与
    下面的变异用例守。
    """
    src = ABLATION.read_text(encoding="utf-8-sig")

    checked = 0
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Write-Output"):
            continue
        if not any(p in stripped for p in _MACHINE_PREFIXES):
            continue
        checked += 1
        assert stripped.isascii(), (
            f"机器可消费行含非 ASCII，代码页一变就乱码: {stripped}"
        )
    assert checked >= 3, (
        f"只扫到 {checked} 条机器可消费字面量 —— 前缀表大概和脚本脱节了，本用例已失去意义"
    )


def test_ascii_detector_is_not_vacuous() -> None:
    """检测器本身必须有牙齿：喂一条含中文的机器行，必须被判定为非 ASCII。

    没有这一条，`_non_ascii_machine_lines` 完全可以是个恒空的函数，
    而上面两条断言照样绿。
    """
    good = "SCRIPT_UNDER_TEST=C:\\repo\\windows-popup-field-evidence.ps1"
    bad = "SCRIPT_UNDER_TEST=C:\\repo\\\u76d1\u89c6app\\windows-popup-field-evidence.ps1"
    noise = "不是机器行，含中文也无所谓"

    assert _machine_lines(good) == [good]
    assert _machine_lines(bad) == [bad]
    assert _machine_lines(noise) == [], "非机器行不该被纳入判定"
    assert _non_ascii_machine_lines(good) == []
    assert _non_ascii_machine_lines(bad) == [bad]
    # 转义是**无损**的：解回来必须与原文一致
    assert _unescape_ps(_escape_like_ps(bad)) == bad


def _escape_like_ps(text: str) -> str:
    r"""和消融脚本里那段 `[regex]::Replace` 同语义（供上面的无损往返断言用）。"""
    return "".join(ch if " " <= ch <= "~" else "\\u%04x" % ord(ch) for ch in text)


# ---------------------------------------------------------------------------
# 行为验证：真的跑那套消融（需要 PowerShell）
# ---------------------------------------------------------------------------
@pytest.mark.skipif(POWERSHELL is None, reason="本机无 PowerShell，无法做行为验证")
def test_ablation_all_scenarios_pass_on_shipped_script() -> None:
    """随仓库发货的那一份必须全绿。非 Windows 上基线那条会显式 SKIP，其余照跑。"""
    r = _run_ablation()   # 不传 -ScriptPath ⇒ 消融脚本默认解析到仓库里那一份

    combined = (r.stdout or "") + (r.stderr or "")
    assert "ABLATION_SUMMARY=" in combined, combined
    # 用 ASCII 标记定位被测脚本：路径里的非 ASCII 字符已被脚本转义成 \uXXXX，这里解回来
    m = re.search(r"SCRIPT_UNDER_TEST=(\S+)", combined)
    assert m, "缺 SCRIPT_UNDER_TEST 标记\n" + combined
    assert Path(_unescape_ps(m.group(1))).name == SCRIPT.name, (
        "消融测的不是仓库里那一份 —— 默认解析路径错了\n" + combined
    )
    # 机器可消费的行必须是**运行时**纯 ASCII（不是源码字面量纯 ASCII）
    assert _machine_lines(combined), "一条机器可消费行都没有\n" + combined
    bad = _non_ascii_machine_lines(combined)
    assert not bad, f"机器可消费行含非 ASCII，代码页一变就乱码: {bad}"
    assert "ABLATION_SUMMARY=ALL_PASS" in combined, combined
    assert r.returncode == 0, combined
    # 非 Windows 上必须**显式**看到 SKIP，不许安静地少一行
    if not sys.platform.startswith("win"):
        assert "=> SKIP" in combined, combined


@pytest.mark.skipif(POWERSHELL is None, reason="本机无 PowerShell，无法做行为验证")
def test_ablation_can_turn_red_when_criterion_degrades(tmp_path: Path) -> None:
    """**变异验证**：把判据改回"只看一种信号"，消融必须变红。

    没有这一条，整个消融套件可能只是恒绿的装饰。
    """
    src = _source()
    target = "        ($r.not_found_marker -or -not $signal3Available)"
    assert src.count(target) == 1, (
        "变异目标不唯一/找不到 —— 变异未生效，本用例失去意义。"
    )
    mutated = tmp_path / "mutated_popup_field_evidence.ps1"
    mutated.write_bytes(src.replace(target, "        $r.not_found_marker").encode("utf-8-sig"))

    r = _run_ablation(mutated)
    combined = (r.stdout or "") + (r.stderr or "")

    assert "ABLATION_SUMMARY=HAS_FAILURE" in combined, (
        "判据退化后消融竟然还全绿 —— 这套消融没有牙齿\n" + combined
    )
    assert r.returncode != 0, combined
    # 退化的正是"提供程序换文案就说不清"这一格
    assert "ABLATION_FAILED provider_message_changed" in combined, combined


@pytest.mark.skipif(POWERSHELL is None, reason="本机无 PowerShell，无法做行为验证")
def test_ascii_check_can_turn_red_on_runtime_output(tmp_path: Path) -> None:
    """**变异验证**：往运行时输出里塞一个中文字符，ASCII 检查必须变红。

    这一条证明的是"运行时那一层检查"有牙齿。注入的哨兵是 `[char]0x76D1`（监），
    与仓库路径是否含中文无关 —— 所以它在 ASCII 路径的 CI runner 上同样有效。
    """
    src = ABLATION.read_text(encoding="utf-8-sig")
    target = 'Write-Output ("SCRIPT_UNDER_TEST=" + $ScriptUnderTestEscaped)'
    assert src.count(target) == 1, (
        "变异目标不唯一/找不到 —— 变异未生效，本用例失去意义。"
    )
    mutated = tmp_path / "ablation_with_nonascii_marker.ps1"
    mutated.write_bytes(
        src.replace(
            target,
            'Write-Output ("SCRIPT_UNDER_TEST=" + ([char]0x76D1) + $ScriptUnderTestEscaped)',
        ).encode("utf-8-sig")
    )

    # 消融脚本被搬到 tmp 之后，它默认按自身位置解析被测脚本 ⇒ 必须显式传 -ScriptPath
    r = _run_ablation(script=SCRIPT, ablation=mutated)
    combined = (r.stdout or "") + (r.stderr or "")
    assert "ABLATION_SUMMARY=" in combined, (
        "变异后的消融脚本没跑起来 —— 结论无意义\n" + combined
    )

    bad = _non_ascii_machine_lines(combined)
    assert bad, (
        "运行时输出里明明塞了中文，ASCII 检查却是空的 —— 这一层检查没有牙齿\n" + combined
    )
    assert "SCRIPT_UNDER_TEST=" in bad[0], bad
