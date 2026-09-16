"""契约：无线 adb 的配对工具必须**不把真机地址/配对码写死**。

为什么钉这条
------------
`scripts/acceptance/adb-pair-connect.py` 是为真机验收准备的操作入口。真机的
Tailscale 地址、（每次重开都会变的）connect/pair 端口与 6 位配对码都属**一次性数据**：
写进仓库既会误导后来者（照抄必然失败），也会把设备地址固化进版本历史。
所以它们**只能来自命令行参数**。

本契约用 AST 解析真实默认值，而不是扫源码文本 —— 文本扫描会被注释里的说明误伤
（本仓今天刚在别处踩过：守卫对已修好的代码报红）。
"""
from __future__ import annotations

import ast
import ipaddress
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "acceptance" / "adb-pair-connect.py"

CODE_RE = re.compile(r"^\d{6}$")


def test_script_is_tracked() -> None:
    assert SCRIPT.is_file(), f"缺少 {SCRIPT.relative_to(ROOT)}"
    proc = subprocess.run(["git", "ls-files", "--error-unmatch",
                           "scripts/acceptance/adb-pair-connect.py"],
                          cwd=str(ROOT), capture_output=True, text=True)
    assert proc.returncode == 0, "配对工具未被 git 跟踪"


def _defaults() -> dict[str, object]:
    """AST 取 argparse 各参数的**默认值**（只看代码，不看注释）。"""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    out: dict[str, object] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument"):
            continue
        name = None
        default = None
        for kw in node.keywords:
            if kw.arg == "default":
                try:
                    default = ast.literal_eval(kw.value)
                except (ValueError, SyntaxError):
                    # 非字面量默认值（常量引用、表达式等）——标记为不可判定，
                    # **不要**因此判失败：本契约只关心"有没有把设备地址/配对码写死"，
                    # 写死必然表现为字面量。
                    default = "<non-literal>"
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                name = arg.value.lstrip("-").replace("-", "_")
        if name:
            out[name] = default
    return out


def test_no_hardcoded_device_address_or_code() -> None:
    d = _defaults()
    # --ip 必填：不该有默认值（有默认值就意味着把某个设备地址写死了）
    assert d.get("ip") is None, f"--ip 不得有默认值，实测 {d.get('ip')!r}"

    for key in ("connect_port", "pair_port"):
        v = d.get(key)
        if isinstance(v, int) and v:
            raise AssertionError(f"--{key} 不得有非零默认端口，实测 {v!r}")

    assert not (isinstance(d.get("code"), str) and CODE_RE.match(d["code"] or "")), (
        f"--code 不得有默认配对码，实测 {d.get('code')!r}"
    )


def test_docstring_does_not_carry_a_device_address() -> None:
    """用法示例里也只允许占位/文档保留地址（rfc5737 / 文档网段），不得出现真实 LAN/设备地址。

    过宽的扫描会造成假红，所以这里**只查 IP 字面量**，并显式放行文档用途地址：
    192.0.2.0/24、198.51.100.0/24、203.0.113.0/24（RFC 5737）与 100.64.0.0/10 里的示例除外
    —— 后者是运营商级 NAT 段，Tailscale 也用它，**无法仅凭网段判断真假**，
    故本契约只禁止**私有 LAN 段**（10/8、172.16/12、192.168/16）出现在文件里。
    """
    text = SCRIPT.read_text(encoding="utf-8")
    private = []
    for m in re.finditer(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", text):
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        if ip.is_private and not ip.is_loopback:
            private.append(m.group(1))
    assert not private, (
        f"文件里出现了私有 LAN 地址 {sorted(set(private))} —— 真机地址属一次性数据，"
        "只能用命令行参数传入，不要写进仓库。"
    )
