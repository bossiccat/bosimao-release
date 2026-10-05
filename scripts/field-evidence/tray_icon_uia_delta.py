# -*- coding: utf-8 -*-
"""
tray_icon_uia_delta.py — Windows 11 XAML 任务栏下的托盘图标数取证（UIA 前后差分法）

背景（2026-10-04 实证定型）：
- Win11（22H2+）任务栏已 XAML 化：Shell_TrayWnd/TrayNotifyWnd 存在，但
  SysPager/ToolbarWindow32 已不存在 ⇒ 经典 TB_BUTTONCOUNT + ReadProcessMemory
  的托盘枚举技术在本机完全失效。溢出区宿主 = TopLevelWindowForOverflowXamlIsland
  （NotifyIconOverflowWindow 已不存在），且飞出窗收起时按钮不物化（UIA 读到 0 个）。
- 因此用「进程生死差分」法：app 运行时与退出后各做一次 UIA 快照，
  多重集差 = 本进程注册的托盘图标数。与图标名字无关，抗改名漂移。

已知假阳性陷阱：时钟按钮 name 含当前时间（"时钟 17:41"），快照跨分钟会
把「改名」误判成「出现」。判读时凡 aid=SystemTrayIcon 的时间类按钮一律人工排除；
本 app 图标特征：aid='NotifyItemIcon' 且 name='贾克斯 · 星核'
（commit 3ae70f7 起 TrayIconBuilder 设了 tooltip；此前无 tooltip 时代 name=''）。
⚠️ 溢出物化陷阱（2026-10-05 实证）：本 app 图标默认进 Win11 溢出区（隐藏图标），
飞出窗收起时按钮不物化（UIA 读 0，差分漏检）——快照前必须先 Invoke 任务栏
chevron（name 含 '隐藏的图标'）展开溢出窗，且 A/B 两态都要展开，口径才一致。

用法（依赖 comtypes，本机装在受管 venv）：
  python tray_icon_uia_delta.py snapshot <label>   # 落盘 tmp/tray_snap_<label>.json
  python tray_icon_uia_delta.py delta <labelA> <labelB>
                                                   # A - B 正差（A 有 B 无 = 随 app 出现）
  # 典型编排：snapshot B_dead（杀进程后）→ 启动 app → snapshot A_alive → delta A_alive B_dead

只读探针：不改任何产品状态，杀/启由编排方控制。
"""
import collections
import ctypes
import json
import sys

import comtypes.client

u32 = ctypes.WinDLL("user32")
CORE = None


def core():
    global CORE
    if CORE is None:
        CORE = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            interface=comtypes.client.GetModule("UIAutomationCore.dll").IUIAutomation,
        )
    return CORE


def snapshot(label):
    c = core()
    items = collections.Counter()
    hosts_seen = []
    for cls in ("Shell_TrayWnd", "TopLevelWindowForOverflowXamlIsland"):
        h = u32.FindWindowW(cls, None)
        hosts_seen.append({"host": cls, "hwnd": h, "found": bool(h)})
        if not h:
            continue
        el = c.ElementFromHandle(ctypes.c_void_p(h))
        found = el.FindAll(4, c.CreateTrueCondition())  # TreeScope_Descendants
        for i in range(found.Length):
            e2 = found.GetElement(i)
            ct = e2.CurrentControlType
            if ct in (50000, 50007):  # Button / ListItem
                items[(ct, e2.CurrentName or "", e2.CurrentAutomationId or "")] += 1
    out = {
        "label": label,
        "hosts": hosts_seen,
        "total": sum(items.values()),
        "items": [
            {"ct": ct, "name": name, "aid": aid, "n": n}
            for (ct, name, aid), n in sorted(items.items())
        ],
    }
    path = f"tmp/tray_snap_{label}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[{label}] total={out['total']} -> {path}")


def delta(label_a, label_b):
    A = json.load(open(f"tmp/tray_snap_{label_a}.json", encoding="utf-8"))
    B = json.load(open(f"tmp/tray_snap_{label_b}.json", encoding="utf-8"))
    ca = collections.Counter({(i["ct"], i["name"], i["aid"]): i["n"] for i in A["items"]})
    cb = collections.Counter({(i["ct"], i["name"], i["aid"]): i["n"] for i in B["items"]})
    diff = ca - cb  # A 有 B 无 = 随 app 出现的按钮
    CT = {50000: "Button", 50007: "ListItem"}
    print(f"A({label_a})={A['total']}  B({label_b})={B['total']}")
    print("--- 随 app 出现的按钮 ---")
    for (ct, name, aid), n in diff.items():
        print(f"  x{n} [{CT.get(ct, ct)}] name={name!r} automationId={aid!r}")
    print(f"结论（原值）: {sum(diff.values())}")
    print("判读提示: 时钟按钮（aid=SystemTrayIcon 且 name 含'时钟'）跨分钟改名会进入差分，须人工扣除；")
    print("          本 app 图标特征 = aid='NotifyItemIcon' 且 name='贾克斯 · 星核'（3ae70f7 起设 tooltip）。")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "snapshot" and len(sys.argv) == 3:
        snapshot(sys.argv[2])
    elif cmd == "delta" and len(sys.argv) == 4:
        delta(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(2)
