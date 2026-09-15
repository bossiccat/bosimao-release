"""契约：**文档里写的命令必须真的能跑**（脚本存在 + 参数在真实 argparse 里存在）。

为什么需要它（2026-09-15 实测教训）
-----------------------------------
交接文档与 `docs/STATUS.md` 第 8 节都把 GO 路径写成裸命令
`scripts/release-preflight.py verify`，而该子命令实际有 **6 个 required 参数**
（`--policy --claims --command-lock --repo-root --artifact-path --evidence-root`）。
接手者照抄必然失败 —— 而且更糟的是 `scripts/check-release-blockers.py` **自己打印的「下一步」**
也是那条错命令。一份把人带向失败步骤的权威文档，比没有文档更坏。

静态扫源码抓不到这类问题（脚本名写对、语法也对，只是**缺了必填项**），
所以这里**调用真实 CLI 的 `--help`** 来核对：文档里出现的每个 `--flag`
都必须真的存在于该脚本（或该子命令）的参数表里。

范围：`docs/STATUS.md` 与 `docs/release/*handover*.md`。要扩到别的文档就往 `DOCS` 里加。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

DOCS = [
    ROOT / "docs" / "STATUS.md",
    *sorted((ROOT / "docs" / "release").glob("*handover*.md")),
]

# 只匹配「脚本 + 可选子命令」；**参数另行从该命令自身的尾部取**。
# ⚠️ 不要把参数并进这个正则：`(?:--flag|\S+)*` 里的 `\S+` 会把整个代码块吞掉，
# 于是每个代码块只识别出第一条命令（实测踩到：`release-preflight.py verify` 根本没被提取，
# 测试因此"没报错"却什么都没核对到）。
INVOCATION = re.compile(
    r"(scripts/[A-Za-z0-9_./-]+\.py)"      # 脚本路径
    r"(?:\s+(?P<sub>[a-z][a-z0-9-]*))?"    # 可选子命令（只认全小写，避免吃掉 flag）
)
FLAG = re.compile(r"--[a-z0-9-]+")

_HELP_CACHE: dict[tuple[str, str], tuple[int, str]] = {}


def _code_fences(text: str) -> list[str]:
    """只取 ``` 围栏内的内容 —— 散文里提到脚本名不应被当成可执行命令。"""
    return re.findall(r"```(?:bash|sh)?\n(.*?)```", text, re.S)


def _help_flags(script: Path, sub: str | None) -> tuple[int, str]:
    """带缓存：同一 (script, sub) 只跑一次 —— 有些脚本导入较重，重复调用会拖垮测试。"""
    key = (str(script), sub or "")
    if key in _HELP_CACHE:
        return _HELP_CACHE[key]
    argv = [sys.executable, str(script)] + ([sub] if sub else []) + ["--help"]
    try:
        proc = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        result = (proc.returncode, (proc.stdout or "") + (proc.stderr or ""))
    except subprocess.TimeoutExpired:
        result = (-1, "<--help 超时：无法核对参数>")
    _HELP_CACHE[key] = result
    return result


def _documented_invocations() -> list[tuple[Path, str, str | None, list[str]]]:
    """逐条命令解析：先把 bash 续行拼回一行，再按行与 `&&` 切开，每条命令只取其自身尾部。"""
    found: list[tuple[Path, str, str | None, list[str]]] = []
    for doc in DOCS:
        for fence in _code_fences(doc.read_text(encoding="utf-8")):
            joined = fence.replace("\\\n", " ")          # 拼回续行
            for raw_line in joined.splitlines():
                for command in raw_line.split("&&"):
                    command = command.strip()
                    m = INVOCATION.search(command)
                    if not m:
                        continue
                    tail = command[m.end():]             # 只取本命令剩余部分
                    flags = sorted(set(FLAG.findall(tail)))
                    found.append((doc, m.group(1), m.group("sub"), flags))
    return found


def test_docs_list_at_least_one_invocation() -> None:
    """若提取到 0 条，说明解析坏了 —— 不能把「测不到」当成「没问题」。"""
    found = _documented_invocations()
    assert found, f"未能从 {[d.name for d in DOCS]} 的代码块里提取到任何命令，检查 INVOCATION 正则"


def test_documented_scripts_exist() -> None:
    missing = []
    for doc, rel, _sub, _flags in _documented_invocations():
        if not (ROOT / rel).is_file():
            missing.append(f"{doc.name}: {rel}")
    assert not missing, f"文档引用了不存在的脚本：{missing}"


def test_documented_flags_exist_in_the_real_cli() -> None:
    """核心断言：文档里的每个 --flag 必须在真实 argparse 里存在。"""
    problems = []
    for doc, rel, sub, flags in _documented_invocations():
        script = ROOT / rel
        if not script.is_file() or not flags:
            continue
        rc, out = _help_flags(script, sub)
        if rc != 0:
            problems.append(f"{doc.name}: {rel} {sub or ''} --help 退出码 {rc}，无法核对参数")
            continue
        for flag in flags:
            if flag not in out:
                problems.append(
                    f"{doc.name}: `{rel} {sub or ''}` 里写了 {flag}，"
                    f"但该子命令的 argparse 里没有它（照抄会失败）"
                )
    assert not problems, "文档命令与真实 CLI 不符：\n  - " + "\n  - ".join(problems)


def test_handover_verify_command_is_complete() -> None:
    """专项：`release-preflight.py verify` 的必填参数一个都不能少。

    这条是本次事故的直接固化 —— 裸写 `verify` 会因缺 6 个必填参数而失败。
    """
    required = {"--policy", "--claims", "--command-lock", "--repo-root",
                "--artifact-path", "--evidence-root"}
    checked = 0
    for doc, rel, sub, flags in _documented_invocations():
        if not rel.endswith("release-preflight.py") or sub != "verify":
            continue
        checked += 1
        missing = required - set(flags)
        assert not missing, f"{doc.name}: release-preflight.py verify 缺必填参数 {sorted(missing)}"
    assert checked, "交接/状态文档里应至少有一条 release-preflight.py verify 的完整写法"


def test_check_release_blockers_does_not_print_a_bare_verify_command() -> None:
    """告警脚本自己打印的「下一步」也不能是那条错命令。"""
    src = (ROOT / "scripts" / "check-release-blockers.py").read_text(encoding="utf-8")
    assert "release-preflight.py verify" in src
    assert "--artifact-path" in src and "--evidence-root" in src, (
        "check-release-blockers.py 打印的下一步必须带上必填参数（或指向 release-harness.md）"
    )
