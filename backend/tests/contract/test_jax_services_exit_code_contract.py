r"""契约：jax-services.ps1 主流程 fail-closed 退出码（exit-code advisory 修复）。

背景（2026-09-03 exit-code advisory）：
    主流程 `Invoke-SvcStart $s | Out-Null` / `Stop-ServiceByName $s | Out-Null`
    把服务操作结果全部丢弃——任何服务启动/停止失败，脚本仍以隐式 exit 0 结束。
    调用方（运维脚本 / 未来自动化 / 计划任务链）无法感知失败，fail-closed 缺位。

修复语义（锁定）：
    - start / stop / restart：任一服务操作返回 $false → exit 1；全部成功 → exit 0
    - status：永远 exit 0（信息性命令，服务 DOWN 不算脚本失败）
    - 并发互斥忙 → exit 2（既有行为，不变）
    - 服务内逐服务的幂等/防风暴语义不变——只加"结果必须体现到进程退出码"这一层

为什么 watchdog 不受影响（调用方安全声明）：
    jax-watchdog.ps1 以子作用域调用（`& $SvcScript start $svc`），exit n 只终止
    被调脚本文件，watchdog 随后用自己的健康复查（Test-Health/Test-BackendHealth
    等）判定成败，不消费退出码。

测试三腿（沿用 test_loadenv_utf8_no_mojibake_contract 的 AST 抽取实测模式）：
    腿 1（静态 AST）：主流程 start/stop/restart 分支不得再用 `| Out-Null` 吞结果，
        必须把 $false 结果记入失败旗标；脚本必须定义 Invoke-ExitCode 并在主流程
        末尾调用。
    腿 2（真机实测）：AST 从真实脚本抽出 Invoke-ExitCode 函数，在独立 powershell
        进程中对 (hadFailure × action) 矩阵实测进程退出码——exit n 在 -EncodedCommand
        子进程里就是进程退出码，骗不了人。
    腿 3（真机端到端，只读）：真实运行 `jax-services.ps1 status`（纯只读：health
        探测 + 进程枚举，无 spawn/无杀进程）→ exit 0 且输出含状态表头。验证 epilogue
        挂进主流程后正常路径完好。

    失败路径的全脚本真机实测（人为弄挂某服务再 start）会触碰生产服务，明确不做了
    ——失败退出码语义由腿 2 在真实 PS 进程中逐组合实测，这正是腿 2 存在的意义。
"""
from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "jax-services.ps1"

_EXIT_FN_NAME = "Invoke-ExitCode"
# (had_failure, action) -> expected process exit code
_EXIT_MATRIX = [
    (True, "start"),
    (False, "start"),
    (True, "stop"),
    (False, "stop"),
    (True, "restart"),
    (False, "restart"),
    (True, "status"),  # status 永远 0，即使 hadFailure
    (False, "status"),
]
_EXPECTED = {("status",): 0}  # 见 _expect()


def _expect(had: bool, action: str) -> int:
    if action == "status":
        return 0
    return 1 if had else 0


_PS_EXTRACT_AND_CALL_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{script}', [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) {{ Write-Error "parse errors: $($parseErrors.Count)"; exit 4 }}
$found = @($ast.FindAll({{
    param($a)
    $a -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq '{fn}'
}}, $true))
if ($found.Count -ne 1) {{
    Write-Error "expected exactly 1 function '{fn}' in jax-services.ps1, got $($found.Count)"
    exit 5
}}
Invoke-Expression $found[0].Extent.Text
& {fn} __HAD__ '__ACTION__'
""".format(script=str(_SCRIPT), fn=_EXIT_FN_NAME)
# 注意：format 只允许消费一次——若串联第二次 .format()，首次还原出的单花括号
# 会被当成新替换字段（KeyError: ' Write-Error "parse errors'，2026-09-03 实测）。
# 动态参数改用 __HAD__/__ACTION__ 占位符 + replace。


def _run_exit_fn(had: bool, action: str) -> subprocess.CompletedProcess:
    # PS 5.1 参数绑定拒绝 string→[bool]（实测 ParameterArgumentTransformationError，
    # 报错文本明示「请改用 $True、$False、1 或 0」）——诊断脚本 _diag_ps_bool_bind.py。
    # int→bool 绑定合法，故传 1/0 而非 true/false。
    ps = _PS_EXTRACT_AND_CALL_TEMPLATE.replace("__HAD__", "1" if had else "0").replace(
        "__ACTION__", action
    )
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


# ---------------- 腿 1：静态 AST / 源文本 ----------------

def test_main_flow_defines_and_calls_exit_function() -> None:
    """脚本必须定义 Invoke-ExitCode 函数，且主流程末尾真实调用（函数定义存在但
    不调用 = 没有接线 = 修复无效）。"""
    source = _SCRIPT.read_text(encoding="utf-8-sig")
    assert re.search(rf"^function\s+{_EXIT_FN_NAME}\b", source, re.MULTILINE), (
        f"jax-services.ps1 必须定义 function {_EXIT_FN_NAME}（fail-closed 退出码 epilogue）"
    )
    # 主流程末尾调用（& 形式或直接名字调用均可，排除定义行本身）
    calls = [
        ln.strip()
        for ln in source.splitlines()
        if _EXIT_FN_NAME in ln and not ln.strip().startswith(("function", "#"))
    ]
    assert any(
        ln.startswith(_EXIT_FN_NAME) or ln.startswith(f"& {_EXIT_FN_NAME}")
        for ln in calls
    ), f"主流程末尾必须调用 {_EXIT_FN_NAME}（仅有函数定义不算接线）: {calls!r}"


def test_main_flow_does_not_discard_service_results() -> None:
    """start/stop/restart 分支不得把服务操作结果 `| Out-Null` 吞掉。

    事故形态：`Invoke-SvcStart $s | Out-Null` → 失败被静默 → 脚本 exit 0。
    修复后形态：`if (-not (Invoke-SvcStart $s)) { ... $HadFailure = $true }`。
    """
    source = _SCRIPT.read_text(encoding="utf-8-sig")
    # 主流程区域：从 "主流程" 注释到脚本结尾
    m = re.search(r"(?ms)^# -+ 主流程 -+\s*$.*$", source)
    assert m, "找不到主流程区域"
    main = m.group(0)
    discarded = [
        ln.strip()
        for ln in main.splitlines()
        if re.search(r"(Invoke-SvcStart|Stop-ServiceByName)\b.*\|\s*Out-Null", ln)
    ]
    assert not discarded, (
        f"主流程仍在丢弃服务操作结果（fail-closed 缺位）: {discarded!r} — "
        "必须 if (-not (...)) { $HadFailure = $true } 记入失败旗标"
    )
    assert "$HadFailure" in main, (
        "主流程必须有 $HadFailure 失败旗标累积各服务操作结果"
    )


# ---------------- 腿 2：真机实测退出码矩阵 ----------------

@pytest.mark.parametrize("had,action", _EXIT_MATRIX, ids=[f"{a}-fail={h}" for h, a in _EXIT_MATRIX])
def test_exit_function_process_exit_code(had: bool, action: str) -> None:
    """AST 抽出真实 Invoke-ExitCode，在真实 powershell 进程中执行并断言进程退出码。"""
    proc = _run_exit_fn(had, action)
    want = _expect(had, action)
    assert proc.returncode == want, (
        f"Invoke-ExitCode(hadFailure={had}, action='{action}') 实测退出码 "
        f"{proc.returncode}，期望 {want}；stderr={proc.stderr[:300]!r}"
    )


# ---------------- 腿 3：真机端到端（只读 status） ----------------

def test_status_action_real_run_exits_zero() -> None:
    """真实运行 status（只读）→ exit 0。验证 epilogue 接线后正常路径完好。"""
    proc = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(_SCRIPT), "status",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"status 实测退出码 {proc.returncode}（期望 0）；stderr={proc.stderr[:300]!r}"
    )
    # 注意：PS 5.1 的 stdout 按系统 ANSI(GBK) 编码，中文表头经 UTF-8 解码必 mojibake
    # （2026-09-03 实测：'贾克斯' → 乱码）。断言只用 ASCII 稳定标记，不匹配中文。
    assert "[model]" in proc.stdout and "[backend]" in proc.stdout and "====" in proc.stdout, (
        f"status 输出缺状态表：{proc.stdout[:200]!r}"
    )
