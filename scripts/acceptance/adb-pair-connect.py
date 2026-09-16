"""无线 adb 的「配对 → 连接 → 验机」**一条命令**（本机定型版）。

为什么必须做成单个脚本
----------------------
本会话每次工具调用结束后 `adb` daemon 会被收割，`pair` 与 `connect` 拆成两次调用
中间会断 ⇒ 表现为"配对成功但立刻掉线"。所以在**同一个进程内**依次做完。

Android 无线调试的三个值（**每次重开都会变**，换 Wi-Fi/Tailscale 后也必须重读）
------------------------------------------------------------------------------
| 值 | 在手机上哪看 | 用途 |
|---|---|---|
| `IP:connect端口` | 「无线调试」主页那条 **IP 地址和端口** | `adb connect` |
| `IP:pair端口` | 点「**使用配对码配对设备**」后**弹出的对话框**里那条 | `adb pair`（**与上面不是同一个端口**） |
| 6 位配对码 | 同一个对话框 | `adb pair` 的第二个参数；**对话框关掉就失效** |

用法：
    ./.venv/Scripts/python.exe scripts/acceptance/adb-pair-connect.py \
        --ip 100.75.48.99 --connect-port 40715 \
        --pair-port 37123 --code 330194
只给 `--ip`/`--connect-port` 时跳过配对，直接尝试连接（用于已配对过的设备）。
"""
from __future__ import annotations

import argparse
import pathlib
import socket
import subprocess
import sys
import time

ADB_DEFAULT = r"tmp\task6-tools\platform-tools\adb.exe"
ROOT = pathlib.Path(__file__).resolve().parents[2]


def run(argv: list[str], timeout: int = 45) -> tuple[int | None, str]:
    """跑一条 adb 命令并合并输出。**用 Python 超时**——
    Windows 下 shell 的 `timeout` 是 timeout.exe（不是 coreutils），会直接报"无效语法"、
    命令根本不会执行（实测踩过）。"""
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return None, "<超时>"


def tcp_ok(host: str, port: int, timeout: float = 6.0) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adb", default=ADB_DEFAULT)
    ap.add_argument("--ip", required=True)
    ap.add_argument("--connect-port", type=int, required=True)
    ap.add_argument("--pair-port", type=int, default=0)
    ap.add_argument("--code", default="")
    args = ap.parse_args()

    adb = str((ROOT / args.adb) if not pathlib.Path(args.adb).is_absolute() else args.adb)
    if not pathlib.Path(adb).is_file():
        print(f"❌ 找不到 adb：{adb}", file=sys.stderr)
        return 2

    target = f"{args.ip}:{args.connect_port}"
    print(f"adb     : {adb}")
    print(f"目标    : {target}")

    # 0) 先判可达性。**超时 vs 拒绝**含义不同：超时=无路由/被丢包；拒绝=主机可达但端口关着。
    if tcp_ok(args.ip, args.connect_port):
        print(f"TCP     : {target} 可连")
    else:
        print(f"TCP     : {target} 不可达（超时或拒绝）—— 先确认真机在线、且端口号是最新的")

    # 1) 配对（可选）
    if args.pair_port and args.code:
        pt = f"{args.ip}:{args.pair_port}"
        print(f"\n[1/3] 配对 {pt}")
        rc, out = run([adb, "pair", pt, args.code])
        print("     ", out.replace("\n", "\n      ") or f"rc={rc}")
        if rc != 0 and "Successfully paired" not in out:
            print("     ⚠️ 配对未成功：常见原因是 pair 端口写成了 connect 端口，或配对码已过期")
            print("        （配对码与 pair 端口只在「使用配对码配对设备」对话框打开期间有效）")
    else:
        print("\n[1/3] 跳过配对（未给 --pair-port/--code）")

    # 2) 连接（daemon 起来后重试一次；首次 connect 常因 daemon 刚起而失败）
    print(f"\n[2/3] 连接 {target}")
    rc, out = run([adb, "connect", target])
    print("     ", out.replace("\n", "\n      ") or f"rc={rc}")
    if "connected" not in out:
        time.sleep(2)
        rc, out = run([adb, "connect", target])
        print("      重试:", out.replace("\n", "\n      ") or f"rc={rc}")

    # 3) 验机
    print("\n[3/3] 设备列表")
    rc, out = run([adb, "devices", "-l"])
    print("     ", out.replace("\n", "\n      "))

    online = any(target in l and "device" in l and "offline" not in l
                 for l in out.splitlines())
    print()
    if online:
        print("✅ 就绪：设备在线（可开始真机验收）")
        return 0
    print("❌ 未就绪：设备不在线。`offline` = TCP 通了但配对/授权未完成 ⇒ 需要配对；"
          "列表为空 = 端口已轮换 ⇒ 重读手机上的三个值")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
