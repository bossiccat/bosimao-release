r"""契约：PE 子系统门禁（`scripts/pe-subsystem-verify.py`）必须真的有牙齿。

背景（2026-09-19，claim `windows-popup-free`）
--------------------------------------------
「客户 Windows 桌面不出现产品后代命令窗」这条属性，在打包前**只**由
`scripts/pe-subsystem-verify.py` 量测。而初版那个脚本是**假门禁**：

    1) `--expect-gui` 是 `action="store_true", default=True`，且 `main()` 从未读过
       `args.expect_gui` —— 传不传都一样，等于引用了一个空操作；
    2) `--dir` / `--installed` 都是 `os.listdir` 非递归，只看得见顶层；
    3) 没有任何机器可消费的标记，只有中文人读输出；
    4) `--installed` 在目录不存在时抛未捕获 `FileNotFoundError`。

后果是实测过的：`--dir …/target/release --expect-gui` 报 `ALL_GUI = True`，
而同一棵树的随包 resources 里躺着 CUI 的
`trtc-electron-sdk/build/Release/liteav_media_server.exe`。

本文件的立场：**一个从未被观察到失败的 gate 不算 gate**。所以每个断言都
锚在「工具能不能变红」上，而不是锚在「工具输出了什么好看的字符串」上：

    - 阳性对照（全 GUI ⇒ PASS）与阴性（含 CUI ⇒ FAIL）成对存在，
      避免"恒 PASS"和"恒 FAIL"两种假象各自蒙对一半；
    - 反假绿用例刻意复刻两次真实事故：按目录名深度不限地剪构建产物，
      会把**真正随包发货**的 CUI 一起剪掉；
    - 「删掉违规文件」不得把 FAIL 偷换成 PASS；
    - 只报告模式刻意用不同名的标记，不能被当成通过。

所有用例都用**合成 PE 字节**，不依赖本机 target 目录或已安装实例，
因此在任何平台（含 ubuntu CI）都确定性可跑，且不 skip。
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "pe-subsystem-verify.py"

GUI = 2
CUI = 3


# --------------------------------------------------------------------------
# 合成最小 PE：只满足 pe_subsystem() 读取所需的偏移，不依赖任何本机二进制。
# 布局：MZ 头（0x40，其中 0x3C 放 e_lfanew）+ "PE\0\0" + COFF(20)
#       + OptionalHeader（magic 在 +0，subsystem 在 +68）
# --------------------------------------------------------------------------
def _pe_bytes(subsystem: int, magic: int = 0x20B) -> bytes:
    stub = bytearray(0x40)
    stub[0:2] = b"MZ"
    pe_off = 0x40
    struct.pack_into("<I", stub, 0x3C, pe_off)
    coff = bytes(20)
    optional = bytearray(0x70)
    struct.pack_into("<H", optional, 0, magic)
    struct.pack_into("<H", optional, 68, subsystem)
    return bytes(stub) + b"PE\x00\x00" + coff + bytes(optional)


def _write_pe(path: Path, subsystem: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_pe_bytes(subsystem))
    return path


def _run(*argv: str, local_app_data: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if local_app_data is not None:
        env["LOCALAPPDATA"] = str(local_app_data)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )


def _marker(stdout: str) -> str:
    """取最后一行 PE_SUBSYSTEM=<verdict>；显式拒绝拿 PE_SUBSYSTEM_SCANNED 凑数。"""
    verdicts = [
        line.strip()[len("PE_SUBSYSTEM="):]
        for line in stdout.splitlines()
        if line.strip().startswith("PE_SUBSYSTEM=")
    ]
    assert verdicts, f"缺少机器可消费标记 PE_SUBSYSTEM=…\nstdout:\n{stdout}"
    return verdicts[-1]


def _tail_marker_is_last_line(stdout: str) -> bool:
    lines = [l for l in stdout.splitlines() if l.strip()]
    return bool(lines) and lines[-1].strip().startswith("PE_SUBSYSTEM=")


def _count(stdout: str, name: str) -> int:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            return int(line.split("=", 1)[1])
    raise AssertionError(f"缺少计数标记 {name}=\nstdout:\n{stdout}")


# --------------------------------------------------------------------------
# 阳性对照 + 阴性：门禁必须两个方向都能走
# --------------------------------------------------------------------------
def test_pe_subsystem_gate_all_gui_passes(tmp_path: Path) -> None:
    """阳性对照：全 GUI ⇒ PASS / exit 0。没有它，"恒 FAIL"会被误当合格门禁。"""
    d = tmp_path / "release"
    _write_pe(d / "app.exe", GUI)
    _write_pe(d / "helper.exe", GUI)

    r = _run("--dir", str(d), "--expect-gui")

    assert r.returncode == 0, r.stdout + r.stderr
    assert _marker(r.stdout) == "PASS"
    assert _count(r.stdout, "PE_SUBSYSTEM_SCANNED") == 2
    assert _count(r.stdout, "PE_SUBSYSTEM_NON_GUI") == 0
    assert _tail_marker_is_last_line(r.stdout)


def test_pe_subsystem_gate_cui_target_fails(tmp_path: Path) -> None:
    """阴性：一个 CUI ⇒ FAIL / exit 1。这是门禁存在的理由本身。"""
    d = tmp_path / "release"
    _write_pe(d / "app.exe", GUI)
    _write_pe(d / "cmd-child.exe", CUI)

    r = _run("--dir", str(d), "--expect-gui")

    assert r.returncode == 1, r.stdout + r.stderr
    assert _marker(r.stdout) == "FAIL"
    assert _count(r.stdout, "PE_SUBSYSTEM_NON_GUI") == 1
    assert "cmd-child.exe" in r.stdout


def test_pe_subsystem_gate_reports_every_non_gui(tmp_path: Path) -> None:
    """多个 CUI 必须被全部计数，不能只报第一个。"""
    d = tmp_path / "release"
    for i in range(3):
        _write_pe(d / f"cui-{i}.exe", CUI)

    r = _run("--dir", str(d), "--expect-gui")

    assert r.returncode == 1, r.stdout + r.stderr
    assert _count(r.stdout, "PE_SUBSYSTEM_NON_GUI") == 3
    assert _count(r.stdout, "PE_SUBSYSTEM_SCANNED") == 3


# --------------------------------------------------------------------------
# 模式语义：不许留装饰性参数，report-only 不许冒充 PASS
# --------------------------------------------------------------------------
def test_pe_subsystem_gate_requires_explicit_mode(tmp_path: Path) -> None:
    """不给模式 ⇒ INVALID_INPUT / exit 2。刻意没有默认放行。"""
    d = tmp_path / "release"
    _write_pe(d / "app.exe", GUI)

    r = _run("--dir", str(d))

    assert r.returncode == 2, r.stdout + r.stderr
    assert _marker(r.stdout) == "INVALID_INPUT"


def test_pe_subsystem_gate_rejects_both_modes(tmp_path: Path) -> None:
    """两个模式一起给 ⇒ INVALID_INPUT，不许"取其一"含糊过关。"""
    d = tmp_path / "release"
    _write_pe(d / "app.exe", GUI)

    r = _run("--dir", str(d), "--expect-gui", "--report-only")

    assert r.returncode == 2, r.stdout + r.stderr
    assert _marker(r.stdout) == "INVALID_INPUT"


def test_pe_subsystem_gate_report_only_is_not_pass(tmp_path: Path) -> None:
    """只报告模式即使看到 CUI 也不判红，但**绝不能**输出 PASS 字样。"""
    d = tmp_path / "release"
    _write_pe(d / "cui-child.exe", CUI)

    r = _run("--dir", str(d), "--report-only")

    assert r.returncode == 0, r.stdout + r.stderr
    assert _marker(r.stdout) == "REPORT_ONLY"
    assert "PE_SUBSYSTEM=PASS" not in r.stdout
    assert _count(r.stdout, "PE_SUBSYSTEM_NON_GUI") == 1


# --------------------------------------------------------------------------
# fail-closed：输入不可用一律 exit 2，不许 crash，也不许静默 0
# --------------------------------------------------------------------------
def test_pe_subsystem_gate_missing_dir_is_fail_closed(tmp_path: Path) -> None:
    """目录不存在：exit 2 + 无 traceback（初版这里是未捕获 FileNotFoundError）。"""
    r = _run("--dir", str(tmp_path / "nope"), "--expect-gui")

    assert r.returncode == 2, r.stdout + r.stderr
    assert _marker(r.stdout) == "INVALID_INPUT"
    assert "Traceback" not in (r.stdout + r.stderr)


def test_pe_subsystem_gate_installed_missing_is_fail_closed(tmp_path: Path) -> None:
    """--installed 指向不存在的安装目录：exit 2，不许 crash、不许静默 0。"""
    empty = tmp_path / "empty-localappdata"
    empty.mkdir()

    r = _run("--installed", "--expect-gui", local_app_data=empty)

    assert r.returncode == 2, r.stdout + r.stderr
    assert _marker(r.stdout) == "INVALID_INPUT"
    assert "Traceback" not in (r.stdout + r.stderr)


def test_pe_subsystem_gate_deleting_offender_cannot_flip_to_pass(tmp_path: Path) -> None:
    """删掉违规文件不得把 FAIL 偷换成 PASS：显式清单里目标缺失 ⇒ INVALID_INPUT。"""
    d = tmp_path / "release"
    _write_pe(d / "app.exe", GUI)
    offender = _write_pe(d / "cui-child.exe", CUI)

    before = _run("--dir", str(d), "--expect-gui")
    assert before.returncode == 1 and _marker(before.stdout) == "FAIL"

    offender.unlink()

    after = _run(str(d / "app.exe"), str(offender), "--expect-gui")
    assert after.returncode == 2, after.stdout + after.stderr
    assert _marker(after.stdout) == "INVALID_INPUT"


# --------------------------------------------------------------------------
# 扫描口径：非递归语义不得静默改变；递归必须显式
# --------------------------------------------------------------------------
def test_pe_subsystem_gate_dir_is_non_recursive_by_default(tmp_path: Path) -> None:
    """`--dir` 非递归是**显式**语义：同上一个命令，加 --recursive 结论必须不同。"""
    d = tmp_path / "release"
    _write_pe(d / "app.exe", GUI)
    _write_pe(d / "nested" / "cui-child.exe", CUI)

    shallow = _run("--dir", str(d), "--expect-gui")
    assert shallow.returncode == 0, shallow.stdout + shallow.stderr
    assert _count(shallow.stdout, "PE_SUBSYSTEM_SCANNED") == 1

    deep = _run("--dir", str(d), "--recursive", "--expect-gui")
    assert deep.returncode == 1, deep.stdout + deep.stderr
    assert _count(deep.stdout, "PE_SUBSYSTEM_SCANNED") == 2
    assert _count(deep.stdout, "PE_SUBSYSTEM_NON_GUI") == 1


def test_pe_subsystem_gate_cargo_exclusion_is_root_level_only(tmp_path: Path) -> None:
    """**反假绿核心**：按名字剪构建产物只允许剪扫描根的直接子级。

    复刻两次真实事故：`trtc-electron-sdk/build/Release/liteav_media_server.exe`
    是**随包发货**的 CUI，深度不限地按 names 剪会把 `.../sdk/build` 一起剪掉，
    于是安装树/构建树报出 NON_GUI=0 的假绿。这里把该形状合成出来：
    剪掉 cargo 的 `build/`，但**必须**仍然发现随包 resources 里的 `build/`。
    """
    target = tmp_path / "target" / "release"
    _write_pe(target / "app.exe", GUI)
    # cargo 自己的 build-script 产物：扫描根直接子级，应被剪
    _write_pe(target / "build" / "pkg-hash" / "build-script-build.exe", CUI)
    # 随包 resources 里的第三方 SDK：也含一个名为 build 的目录，**必须被发现**
    leaked = _write_pe(
        target / "app-runtime" / "resources" / "node_modules" / "sdk"
        / "build" / "Release" / "liteav_media_server.exe",
        CUI,
    )

    r = _run(
        "--dir", str(target), "--recursive", "--exclude-cargo-artifacts", "--expect-gui"
    )

    assert r.returncode == 1, r.stdout + r.stderr
    assert _marker(r.stdout) == "FAIL"
    assert _count(r.stdout, "PE_SUBSYSTEM_NON_GUI") == 1, r.stdout
    assert leaked.name in r.stdout, "随包 resources 里的 CUI 被错误剪掉了（假绿）"
    assert "build-script-build.exe" not in r.stdout, "cargo 产物本应被剪掉"


def test_pe_subsystem_gate_exclusion_requires_recursive(tmp_path: Path) -> None:
    """非递归下给 --exclude-cargo-artifacts 是用法错误，不许静默忽略。"""
    target = tmp_path / "target" / "release"
    _write_pe(target / "app.exe", GUI)

    r = _run("--dir", str(target), "--exclude-cargo-artifacts", "--expect-gui")

    assert r.returncode == 2, r.stdout + r.stderr
    assert _marker(r.stdout) == "INVALID_INPUT"


def test_pe_subsystem_gate_exclusion_rejects_non_target_root(tmp_path: Path) -> None:
    """非 cargo 根上按名字剪 = 假绿制造机，必须直接拒绝。"""
    plain = tmp_path / "plain"
    _write_pe(plain / "build" / "leaked.exe", CUI)

    r = _run(
        "--dir", str(plain), "--recursive", "--exclude-cargo-artifacts", "--expect-gui"
    )

    assert r.returncode == 2, r.stdout + r.stderr
    assert _marker(r.stdout) == "INVALID_INPUT"


# --------------------------------------------------------------------------
# debug 构建：正确配置不得假红
# --------------------------------------------------------------------------
def test_pe_subsystem_gate_debug_target_is_invalid_not_fail(tmp_path: Path) -> None:
    """target/debug 下 jax-pet.exe 本来就是 CUI（cfg_attr(not(debug_assertions))）。

    门禁模式必须判 INVALID_INPUT（exit 2，输入口径不对），**不是** FAIL（exit 1）
    —— 否则会对一个正确配置假红。只报告模式允许，但要标出非发布口径。
    """
    debug = tmp_path / "target" / "debug"
    _write_pe(debug / "jax-pet.exe", CUI)

    gate = _run("--dir", str(debug), "--expect-gui")
    assert gate.returncode == 2, gate.stdout + gate.stderr
    assert _marker(gate.stdout) == "INVALID_INPUT"
    assert "debug" in gate.stdout

    report = _run("--dir", str(debug), "--report-only")
    assert report.returncode == 0, report.stdout + report.stderr
    assert _marker(report.stdout) == "REPORT_ONLY"
    assert "debug 构建" in report.stdout
