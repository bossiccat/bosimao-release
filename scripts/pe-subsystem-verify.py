#!/usr/bin/env python3
"""PE 子系统验证（Windows 弹窗 / 命令窗防护门禁）。

背景
----
子系统枚举（Microsoft IMAGE_SUBSYSTEM）：
    0 UNKNOWN
    2 WINDOWS_GUI   <- GUI 子系统，启动时不分配控制台（业务二进制应为该值）
    3 WINDOWS_CUI   <- 控制台子系统，启动时分配控制台（cmd 类工具才是该值）

在 Windows 上：GUI 父进程拉起的 **GUI 子进程**不会弹命令窗；GUI 父进程拉起的
**CUI 子进程**会弹一个可见控制台窗，除非 spawn 时显式带 CREATE_NO_WINDOW。
所以「不弹窗」这条属性既依赖子系统，也依赖每个 spawn 点。本脚本只负责子系统那一半。

这个脚本是 `governance/claims/windows-popup-free.json` 的承重件：它是唯一能
在打包前量测「随包下发的二进制里有没有 CUI」的工具。改它之前先读 `--help` 与
下面的口径说明，别把它改回假门禁（见「历史」）。

模式（必须显式二选一，没有默认）
--------------------------------
    --expect-gui    门禁模式：任何非 GUI 目标 => FAIL（退出码 1）；
                    输入不完整/不合法 => INVALID_INPUT（退出码 2，fail-closed）。
    --report-only   只报告模式：只清点不判红，退出码 0。标记为 REPORT_ONLY，
                    刻意与 PASS 不同名，避免被误当成门禁通过。

扫描口径（**这是本脚本最容易出错的地方**）
------------------------------------------
`--dir` 是**非递归**语义：只清点该目录的直接子级 *.exe。这是「构建顶层产物」
口径，不是「随包集合」口径。要下钻请显式加 `--recursive`——语义不静默改变。

为什么不能天真递归：本仓 `target/release` 下递归共 111 个 .exe，其中 98 个是
CUI，全部来自 cargo 的 build-script / 测试 / 示例产物（`build/**`、`deps/**`、
`examples/**`）。它们**从不随包发布**。要剪掉它们必须**显式**加
`--exclude-cargo-artifacts`，而且只剪**扫描根的直接子级**（cargo 只在那里放
`build/`、`deps/` 等）。

这个「显式 + 只剪根直接子级」的设计是被两次实测假绿逼出来的，不是洁癖：
`build` 这个目录名在 cargo 之外同样是随包 resources 的一部分——
`trtc-electron-sdk/build/Release/liteav_media_server.exe` 就是。第一版
无条件按名字剪，`--installed` 把 3 个**真正随包发货**的 CUI 当构建产物剪掉，
安装树报出 NON_GUI=0；第二版「根在 target 下就按名字剪」，又把这 3 个
`target/release/*/resources/**/trtc-electron-sdk/build/Release/` 下的 CUI 剪掉。
换句话说：**排除清单的失效方向必须是「多报」而不是「漏报」。**

真正的「随包集合」口径是 `--installed`：对真实 per-user 安装目录
（%LOCALAPPDATA%\\贾克斯·星核）递归清点，**不剪任何目录**（落进 $INSTDIR 的
一切都在发货）。实测同时段：`--installed` 9 个 exe / 2 CUI；
`--dir target/release --recursive` 3 CUI（liteav）+ 98 cargo 产物。

注意这两个口径含 **trtc-electron-sdk 自带的 `liteav_media_server.exe`（CUI）**。
它是潜伏项而非在飞缺陷（sidecar 应用代码对媒体混流 / 推流 / 截屏家族零引用，
构建期另有 preflight 把"日后用到"变成红）。

产品决策已于 2026-09-20 落地：**从随包 resources 里剔除该二进制** —— 名字 + 理由 +
移除日期 + 实测量记录在 `scripts/lib/sidecar-trust.js` 的 `INTENTIONALLY_ABSENT_NATIVE`，
构建期由 `scripts/lib/sidecar-package-build.js` 的 `pruneIntentionallyAbsentNatives`
（两向 fail-closed）与 `SIDECAR_MEDIA_MIXING_REQUIRES_PRUNED_NATIVE` preflight 共同保证。

本脚本刻意**不提供 allowlist**：把「已知 CUI」静默放行正是本 claim 要消灭的那类假绿。
所以"变绿"只能靠**真的不发货那个二进制**，不是靠改这个脚本，也不是靠在某处加一行豁免。

⚠️ 剪除只让**新构建的世代**干净。客户机（含本机）上已经落地的旧世代每个都含一份该
二进制，必须另行回收，否则 `--installed --expect-gui` 仍然 FAIL（本机 2026-09-20 实测：
13 个 exe / 4 个 CUI，4 份都来自盘上残留的 4 个世代）。

debug 构建口径
--------------
`src/main.rs` 用的是 `#![cfg_attr(not(debug_assertions), windows_subsystem =
"windows")]`，所以 `target/debug/jax-pet.exe` **本来就是 CUI，而且是正确的**。
门禁模式下扫到 `target/debug/` 下的目标一律判 INVALID_INPUT 并明确说明原因
（而不是报 FAIL）；只报告模式下允许，但逐行标注为非发布口径。
绝不对一个正确的 debug 配置假红。

退出码
------
    0 = PASS（门禁模式全 GUI）/ REPORT_ONLY
    1 = FAIL（门禁模式发现非 GUI）
    2 = INVALID_INPUT（模式没给、目录不存在、目标缺失等输入问题；fail-closed）

机器判据（稳定 ASCII，供 pytest / CI 消费；中文输出保留给人看）
------------------------------------------------------------
    PE_SUBSYSTEM_SCANNED=<n>
    PE_SUBSYSTEM_NON_GUI=<n>
    PE_SUBSYSTEM=<PASS|FAIL|REPORT_ONLY|INVALID_INPUT>      <- 末行

历史（别改回去）
----------------
初版的 `--expect-gui` 是 `action="store_true", default=True` 且 main() 从未读过
`args.expect_gui` —— 一个传不传都一样的空操作，而 `--dir`/`--installed` 都只
`os.listdir` 顶层。两者叠加让「顶层 5 个 exe 全 GUI」被当成「产品无 CUI」，
而随包下发的 CUI 后代完全在视野之外。`--installed` 还会在目录不存在时抛
未捕获 FileNotFoundError。门禁必须被证明能变红：守护测试见
backend/tests/contract/test_pe_subsystem_gate_contract.py。
"""
from __future__ import annotations

import argparse
import os
import re
import struct
import sys

SUBSYSTEM_ENUM = {
    0: "UNKNOWN",
    1: "NATIVE",
    2: "WINDOWS_GUI",      # 业务 GUI 二进制应为该值
    3: "WINDOWS_CUI",      # 控制台（会弹命令窗）
    9: "WINDOWS_CE_GUI",
    14: "EFI_APPLICATION",
}

GUI_SUBSYSTEM = 2

# cargo 构建产物目录：`build/**` 里是 build-script（build_script_build-*.exe），
# `deps/**`、`examples/**` 里是测试/示例二进制，`incremental/`、`.fingerprint/`
# 是中间态。它们全是 CUI，但**从不随包发布**。实测：target/release 递归
# 111 个 .exe 中 98 个属于这一类。
#
# !! 只在 --exclude-cargo-artifacts 显式开启、且只剪**扫描根的直接子级** !!
# 两条教训（都是实测踩出来的，改这段前请先看）：
#  1) 第一版无条件按名字剪 → `--installed` 把 trtc-electron-sdk/build/Release/
#     liteav_media_server.exe（**真正随包发货**的 CUI）当构建产物剪掉，安装树
#     报出 NON_GUI=0 假绿。`build` 这个目录名在 cargo 之外同样存在。
#  2) 第二版"根在 target 下就按名字剪" → 仍然漏：cargo 的 build/ 只出现在
#     `target/<profile>/` 的直接子级，而 `target/release/jax-rtc-sidecar-runtime/
#     resources/**/trtc-electron-sdk/build/Release/` 是**随包 resources**，
#     实测 3 个 liteav CUI 被当成构建产物剪掉，又一个假绿。
# 所以：剪的范围必须精确到「扫描根直接子级」，深度不限的按名字剪永远不安全。
# **排除清单的失效方向必须是「多报」而不是「漏报」。**
CARGO_ARTIFACT_DIRS = frozenset({"build", "incremental", ".fingerprint", "deps", "examples"})

CARGO_TARGET_ROOT_RE = re.compile(r"(?:^|[\\/])target(?:[\\/]|$)", re.IGNORECASE)

# `target/debug/` 下的目标不是发布口径（cfg_attr(not(debug_assertions)) 生效）。
DEBUG_TARGET_RE = re.compile(r"(?:^|[\\/])target[\\/]debug[\\/]", re.IGNORECASE)

MARKER_PREFIX = "PE_SUBSYSTEM="
SCANNED_MARKER = "PE_SUBSYSTEM_SCANNED="
NON_GUI_MARKER = "PE_SUBSYSTEM_NON_GUI="

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INVALID = 2

# 每个目标在报告里的归宿
TARGET_OK = "ok"
TARGET_NON_GUI = "non-gui"
TARGET_MISSING = "missing"
TARGET_UNPARSEABLE = "unparseable"


def pe_subsystem(path: str) -> tuple[int | None, int | None]:
    """返回 (subsystem, optional_header_magic)。解析失败返回 (None, None)。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None, None
    if data[:2] != b"MZ":
        return None, None
    if len(data) < 0x40:
        return None, None
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\x00\x00":
        return None, None
    magic = struct.unpack_from("<H", data, pe + 24)[0]
    # Subsystem 位于 OptionalHeader 偏移 68（PE32 与 PE32+ 相同）
    subsystem = struct.unpack_from("<H", data, pe + 24 + 68)[0]
    return subsystem, magic


def is_cargo_target_root(root: str) -> bool:
    """扫描根是否落在 cargo target 目录内（决定要不要剪构建产物目录）。"""
    norm = os.path.normpath(os.path.abspath(root))
    return CARGO_TARGET_ROOT_RE.search(norm) is not None


def collect_dir(
    root: str, recursive: bool, prune_root_children: bool
) -> tuple[list[str], list[str]]:
    """返回 (目标列表, 被剪掉的目录)。

    非递归 = 只清点直接子级 *.exe（构建顶层产物口径）。
    递归   = 下钻；prune_root_children 为真时，只剪**扫描根的直接子级**里
             名字落在 CARGO_ARTIFACT_DIRS 的目录（理由见 CARGO_ARTIFACT_DIRS 注释：
             深度不限的按名字剪会把随包 resources 里的第三方 build/ 一起剪掉）。
    """
    targets: list[str] = []
    pruned: list[str] = []
    if not recursive:
        for name in sorted(os.listdir(root)):
            if name.lower().endswith(".exe"):
                targets.append(os.path.join(root, name))
        return targets, pruned
    root_key = os.path.normcase(os.path.abspath(root))
    for dirpath, dirnames, filenames in os.walk(root):
        at_root = os.path.normcase(os.path.abspath(dirpath)) == root_key
        keep = []
        for d in dirnames:
            if prune_root_children and at_root and d in CARGO_ARTIFACT_DIRS:
                pruned.append(os.path.join(dirpath, d))
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            if name.lower().endswith(".exe"):
                targets.append(os.path.join(dirpath, name))
    return targets, pruned


def installed_root() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "贾克斯·星核")


def classify(path: str) -> tuple[str, int | None, int | None]:
    if not os.path.exists(path):
        return TARGET_MISSING, None, None
    sub, magic = pe_subsystem(path)
    if sub is None:
        return TARGET_UNPARSEABLE, None, magic
    return (TARGET_OK if sub == GUI_SUBSYSTEM else TARGET_NON_GUI), sub, magic


def _invalid(message: str) -> int:
    print(f"INVALID_INPUT: {message}")
    print(f"{MARKER_PREFIX}INVALID_INPUT")
    return EXIT_INVALID


def main() -> int:
    ap = argparse.ArgumentParser(
        description="PE 子系统验证（Windows 弹窗 / 命令窗防护门禁）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("paths", nargs="*", help="exe 路径（显式清单）")
    ap.add_argument(
        "--dir",
        help="清点目录下直接子级 *.exe（非递归；下钻请显式加 --recursive）",
    )
    ap.add_argument(
        "--recursive",
        action="store_true",
        help="配合 --dir 下钻扫描（默认不剪任何目录；语义不静默改变）",
    )
    ap.add_argument(
        "--exclude-cargo-artifacts",
        dest="exclude_cargo_artifacts",
        action="store_true",
        help=(
            "递归时剪掉扫描根直接子级里的 cargo 构建产物目录 "
            + "/".join(sorted(CARGO_ARTIFACT_DIRS))
            + "（仅限 cargo target 根，且只剪直接子级；"
            "这些 CUI 产物从不随包发布，不应参与门禁）"
        ),
    )
    ap.add_argument(
        "--installed",
        action="store_true",
        help="清点已安装目录（%LOCALAPPDATA%\\贾克斯·星核，递归；随包集合口径）",
    )
    ap.add_argument(
        "--expect-gui",
        dest="expect_gui",
        action="store_true",
        help="门禁模式：任何非 GUI 目标判 FAIL（退出码 1）",
    )
    ap.add_argument(
        "--report-only",
        dest="report_only",
        action="store_true",
        help="只报告模式：只清点不判红（退出码 0，标记 REPORT_ONLY）",
    )
    args = ap.parse_args()

    # 模式必须显式二选一。刻意不设默认：默认放行 = 假绿温床。
    if args.expect_gui == args.report_only:
        return _invalid("必须且只能给一个模式：--expect-gui 或 --report-only")

    gate = args.expect_gui

    targets: list[str] = list(args.paths)
    pruned: list[str] = []
    prune_note = ""
    if args.dir:
        if not os.path.isdir(args.dir):
            return _invalid(f"--dir 目录不存在: {args.dir}")
        if args.exclude_cargo_artifacts and not args.recursive:
            return _invalid("--exclude-cargo-artifacts 只对递归扫描有意义（非递归不剪）")
        prune_here = args.exclude_cargo_artifacts
        if prune_here and not is_cargo_target_root(args.dir):
            # fail-closed：拒绝在非 cargo 根上按名字剪，避免又造出假绿。
            return _invalid(
                "--exclude-cargo-artifacts 只允许用在 cargo target 目录下，"
                f"当前扫描根不在 target 下: {args.dir}"
            )
        found, pruned = collect_dir(args.dir, args.recursive, prune_here)
        if args.recursive:
            prune_note = (
                f"递归扫描 {args.dir}；已按 --exclude-cargo-artifacts 剪掉扫描根直接子级"
                "里的 cargo 构建产物目录"
                if prune_here
                else f"递归扫描 {args.dir}；未剪任何目录（要剪 cargo 产物请显式加 "
                "--exclude-cargo-artifacts）"
            )
        else:
            prune_note = f"非递归：只清点 {args.dir} 的直接子级 *.exe"
        targets += found

    if args.installed:
        root = installed_root()
        # fail-closed：目录不存在是输入问题，不是「没有 CUI」，绝不能静默返回 0。
        if not os.path.isdir(root):
            return _invalid(f"安装目录不存在: {root}")
        # 随包集合口径：落进 $INSTDIR 的一切都在发货，**不剪任何目录**。
        found, pruned_here = collect_dir(root, True, False)
        pruned += pruned_here
        prune_note = f"安装目录随包集合口径（{root}）：递归且不剪任何目录"
        targets += found

    if not targets:
        return _invalid("没有清点到任何 .exe（检查 --dir / --installed / 路径清单）")

    # 去重，保持稳定顺序
    seen: set[str] = set()
    ordered: list[str] = []
    for t in targets:
        key = os.path.normcase(os.path.abspath(t))
        if key not in seen:
            seen.add(key)
            ordered.append(t)

    debug_targets = [t for t in ordered if DEBUG_TARGET_RE.search(t)]
    if gate and debug_targets:
        return _invalid(
            "目标落在 target/debug/ 下：debug 构建的 windows_subsystem 受 "
            "cfg_attr(not(debug_assertions)) 影响，jax-pet.exe 本来就是 CUI——"
            "这是正确配置，不能当 FAIL。请改用 target/release 或加 --report-only。"
            f"（命中 {len(debug_targets)} 项，例如 {debug_targets[0]}）"
        )

    print("PE 子系统检查（2=WINDOWS_GUI 正确，3=WINDOWS_CUI 会弹命令窗）：")
    if prune_note:
        print(f"  ({prune_note})")
    if pruned:
        shown = ", ".join(os.path.basename(p) or p for p in pruned[:6])
        print(
            f"  (已剪掉 {len(pruned)} 个 cargo 构建产物目录: {shown}"
            f"{' …' if len(pruned) > 6 else ''}；这些目录下的 CUI 产物从不随包发布)"
        )

    non_gui = 0
    missing = 0
    unparseable = 0
    for p in ordered:
        kind, sub, magic = classify(p)
        label = SUBSYSTEM_ENUM.get(sub, f"UNKNOWN({sub})") if sub is not None else "n/a"
        debug_note = (
            "  [debug 构建：非发布口径]" if not gate and DEBUG_TARGET_RE.search(p) else ""
        )
        if kind == TARGET_OK:
            status = "OK "
        elif kind == TARGET_NON_GUI:
            status = "!! "
            non_gui += 1
        elif kind == TARGET_MISSING:
            status = "[MISSING] "
            missing += 1
        else:
            status = "[UNPARSEABLE] "
            unparseable += 1
        magic_txt = f"0x{magic:x}" if magic is not None else "n/a"
        sub_txt = f"{sub:>2}" if sub is not None else " ?"
        print(f"  {status}subsystem={sub_txt} {label:16} magic={magic_txt}  "
              f"{os.path.basename(p)}{debug_note}")

    print("-" * 60)
    print(f"{SCANNED_MARKER}{len(ordered)}")
    print(f"{NON_GUI_MARKER}{non_gui}")

    # 输入不完整同样 fail-closed：否则「把 CUI 文件删掉」会把 FAIL 偷换成 PASS。
    if missing or unparseable:
        print(
            f"输入不完整：{missing} 个缺失、{unparseable} 个无法解析 —— "
            "不能在残缺集合上下门禁结论"
        )
        print(f"{MARKER_PREFIX}INVALID_INPUT")
        return EXIT_INVALID

    if not gate:
        print(f"RESULT: 只报告模式，{non_gui} 个非 GUI 子系统（不判红）")
        print(f"{MARKER_PREFIX}REPORT_ONLY")
        return EXIT_PASS

    if non_gui == 0:
        print("RESULT: ALL_GUI = True  (全部为 GUI 子系统，不会弹出命令窗)")
        print(f"{MARKER_PREFIX}PASS")
        return EXIT_PASS

    print(f"RESULT: 存在 {non_gui} 个非 GUI 子系统二进制（可能弹命令窗）")
    print(f"{MARKER_PREFIX}FAIL")
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
