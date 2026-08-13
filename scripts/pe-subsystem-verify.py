#!/usr/bin/env python3
"""
PE 子系统验证脚本（Windows 弹窗 / 命令窗防护验证）

子系统的正确枚举（Microsoft IMAGE_SUBSYSTEM）：
    0 UNKNOWN
    1 NATIVE
    2 WINDOWS_GUI   <- GUI 子系统，启动时不分配黑色命令窗（业务二进制应为该值）
    3 WINDOWS_CUI   <- 控制台子系统，启动时分配命令窗（cmd/控制台工具才是该值）

用法：
    python pe-subsystem-verify.py <exe...>
或：
    python pe-subsystem-verify.py --dir <dir>        # 检查目录下所有 .exe
    python pe-subsystem-verify.py --installed         # 检查已安装目录

退出码：0 = 全部 GUI，1 = 存在非 GUI 子系统二进制
"""
import argparse
import os
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


def pe_subsystem(path: str) -> tuple[int, int | None]:
    """返回 (subsystem, optional_header_magic)。解析失败返回 (None, None)。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None, None
    if data[:2] != b"MZ":
        return None, None
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\x00\x00":
        return None, None
    magic = struct.unpack_from("<H", data, pe + 24)[0]
    # Subsystem 位于 OptionalHeader 偏移 68（PE32 与 PE32+ 相同）
    subsystem = struct.unpack_from("<H", data, pe + 24 + 68)[0]
    return subsystem, magic


def main() -> int:
    ap = argparse.ArgumentParser(description="PE 子系统验证")
    ap.add_argument("paths", nargs="*", help="exe 路径")
    ap.add_argument("--dir", help="检查目录下所有 .exe")
    ap.add_argument(
        "--installed",
        action="store_true",
        help="检查已安装目录（%LOCALAPPDATA%\\贾克斯·星核）",
    )
    ap.add_argument("--expect-gui", action="store_true", default=True,
                    help="期望全部为 GUI 子系统（默认开启）")
    args = ap.parse_args()

    targets: list[str] = list(args.paths)
    if args.dir:
        targets += [
            os.path.join(args.dir, fn)
            for fn in sorted(os.listdir(args.dir))
            if fn.lower().endswith(".exe")
        ]
    if args.installed:
        d = os.environ.get("LOCALAPPDATA", "") + "/贾克斯·星核"
        targets += [
            os.path.join(d, fn)
            for fn in sorted(os.listdir(d))
            if fn.lower().endswith(".exe")
        ]
    if not targets:
        ap.error("未指定任何 exe，请传入路径、--dir 或 --installed")

    all_gui = True
    print("PE 子系统检查（2=WINDOWS_GUI 正确，3=WINDOWS_CUI 会弹命令窗）：")
    for p in targets:
        if not os.path.exists(p):
            print(f"  [MISSING] {p}")
            all_gui = False
            continue
        sub, magic = pe_subsystem(p)
        label = SUBSYSTEM_ENUM.get(sub, f"UNKNOWN({sub})")
        ok = sub == 2
        if not ok:
            all_gui = False
        status = "OK " if ok else "!! "
        print(f"  {status} subsystem={sub:>2} {label:16} magic=0x{magic or 0:x}  {os.path.basename(p)}")

    print("-" * 60)
    if all_gui:
        print("RESULT: ALL_GUI = True  (全部为 GUI 子系统，不会弹出命令窗)")
        return 0
    print("RESULT: 存在非 GUI 子系统二进制（可能弹命令窗）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
