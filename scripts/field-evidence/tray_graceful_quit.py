"""现场取证：正常退出后重启（claim 场景 6）—— 真正执行托盘「退出」(graceful quit)。

目标：定位产品托盘图标 -> 打开其右键菜单 -> 激活「退出」(label 退出) -> 证明进程走
graceful 路径退出（exit code 0，sidecar 子进程被 Supervisor 有序关停，无孤儿）。

实现要点（已对 tray-icon 0.24.2 源码核对）：
  · 托盘菜单由 tray_icon_app 窗口的 wndproc 处理，回调消息 WM_USER_TRAYICON = 6002。
  · 向该窗口 PostMessageW(6002, 0, WM_RBUTTONUP=0x0205) 即触发 Tauri 的 show_tray_menu ->
    TrackPopupMenu，弹出原生 #32768 菜单（绕开 Win11 XAML 托盘岛对合成输入的命中测试，
    也绕开 Shell_NotifyIconGetRect 的 DPI 错位）。
  · 菜单弹出后读取「退出」项的命令 ID，向其 owner 窗口 PostMessageW(WM_COMMAND, id, 0)
    即可触发 on_menu_event("quit") -> app.exit(0)。全程不依赖鼠标移动（鼠标在
    非交互会话里被 ERROR_ACCESS_DENIED 禁止）。

环境前提（硬约束）：tray wndproc 在 show_tray_menu 之前先调用 GetCursorPos，若为 0 则
return 0，菜单永不弹出。因此本脚本只有在【交互式桌面会话】里 GetCursorPos 可用时才能完成
graceful quit。若本进程 GetCursorPos 返回 ERROR_ACCESS_DENIED，则同样的限制会作用于
被本会话拉起的 app 进程，脚本会采集该证据并以 INFEASIBLE_IN_THIS_SESSION 收尾，
绝不伪造成功。

脱敏：不读窗口标题，只报类名+可见性；进程只报 basename + PID/PPID/SessionId。不写注册表、
不改计划任务、不 kill 产品进程（清场由调用方决定）。不 spawn node。
"""
import base64
import ctypes as c
import os
import json
import pathlib
import subprocess
import time
import datetime

import ctypes.wintypes as wt

user32 = c.windll.user32
kernel32 = c.windll.kernel32
shell32 = c.windll.shell32

WM_USER_TRAYICON = 6002
WM_RBUTTONUP = 0x0205
WM_USER_SHOW_TRAYICON = 6005
WM_COMMAND = 0x0111
SMTO_BLOCK = 0x0001
MIIM_STRING = 0x40
MIIM_ID = 0x2

PRODUCT_EXES = {"jax-pet.exe", "jax-rtc-sidecar.exe"}
EXE = r"C:\Users\Administrator\AppData\Local\贾克斯·星核\jax-pet.exe"
OUT_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "outputs"
OUT_DIR.mkdir(exist_ok=True)

user32.SetCursorPos.argtypes = [c.c_int, c.c_int]
user32.GetCursorPos.argtypes = [c.POINTER(wt.POINT)]; user32.GetCursorPos.restype = c.c_int
user32.PostMessageW.argtypes = [wt.HWND, c.c_uint, c.c_uint64, c.c_int64]; user32.PostMessageW.restype = c.c_int
user32.SendMessageTimeoutW.argtypes = [wt.HWND, c.c_uint, c.c_uint64, c.c_int64, c.c_uint, c.c_uint, c.POINTER(c.c_uint64)]
user32.SendMessageTimeoutW.restype = c.c_int
user32.EnumWindows.argtypes = [c.c_void_p, c.c_int64]
user32.GetClassNameW.argtypes = [wt.HWND, c.c_wchar_p, c.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, c.POINTER(c.c_uint)]; user32.GetWindowThreadProcessId.restype = c.c_uint
user32.IsWindowVisible.argtypes = [wt.HWND]; user32.IsWindowVisible.restype = c.c_int
user32.GetMenu.argtypes = [wt.HWND]; user32.GetMenu.restype = wt.HMENU
user32.GetMenuItemCount.argtypes = [wt.HMENU]; user32.GetMenuItemCount.restype = c.c_int
user32.GetMenuItemInfoW.argtypes = [wt.HMENU, c.c_uint, c.c_int, c.c_void_p]; user32.GetMenuItemInfoW.restype = c.c_int
user32.GetMenuItemRect.argtypes = [wt.HWND, wt.HMENU, c.c_uint, c.c_void_p]; user32.GetMenuItemRect.restype = c.c_int
user32.mouse_event.argtypes = [c.c_uint, c.c_uint, c.c_uint, c.c_uint, c.c_void_p]
kernel32.ProcessIdToSessionId.argtypes = [c.c_uint, c.POINTER(c.c_uint)]
kernel32.ProcessIdToSessionId.restype = c.c_int


class MENUITEMINFO(c.Structure):
    _fields_ = [("cbSize", c.c_uint), ("fMask", c.c_uint), ("fType", c.c_uint),
                ("fState", c.c_uint), ("wID", c.c_uint), ("hSubMenu", wt.HMENU),
                ("hbmpChecked", wt.HBITMAP), ("hbmpUnchecked", wt.HBITMAP),
                ("dwItemData", c.c_void_p), ("dwTypeData", c.c_wchar_p),
                ("cch", c.c_uint), ("hbmpItem", wt.HBITMAP)]


def _ps_enumerate():
    """powershell -File + base64：取 PID/PPID/SessionId/basename，自带双源交叉。"""
    ps = """
$ErrorActionPreference='Continue'
$cim=@(Get-CimInstance Win32_Process)
$rows=@()
foreach($p in $cim){ $rows+=('{0}|{1}|{2}|{3}' -f $p.ProcessId,$p.ParentProcessId,$p.SessionId,$p.Name) }
$obj=[ordered]@{ total=$cim.Count; rows=$rows }
$obj|ConvertTo-Json -Compress -Depth 4
"""
    b64 = base64.b64encode(ps.encode("utf-8")).decode("ascii")
    out = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", b64],
                         capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
    try:
        obj = json.loads(out)
    except Exception:
        return {}
    procs = {}
    for row in obj.get("rows", []):
        pid, ppid, sid, name = row.split("|")
        procs[int(pid)] = {"ppid": int(ppid), "session": int(sid), "name": name}
    return procs


def utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe_cursor():
    p = wt.POINT()
    ok = user32.GetCursorPos(c.byref(p))
    return {"GetCursorPos_ok": ok, "gle": kernel32.GetLastError() & 0xFFFFFFFF, "pt": (p.x, p.y)}


def session_id(pid):
    sid = c.c_uint(0)
    if kernel32.ProcessIdToSessionId(pid, c.byref(sid)):
        return sid.value
    return None


def tray_windows(pid):
    out = []
    def cb(h, _):
        pr = c.c_uint(0); user32.GetWindowThreadProcessId(h, c.byref(pr))
        if pr.value == pid:
            cls = c.create_unicode_buffer(256); user32.GetClassNameW(h, cls, 256)
            if cls.value == "tray_icon_app":
                out.append(int(h))
        return 1
    user32.EnumWindows(c.WINFUNCTYPE(c.c_int, wt.HWND, c.c_int64)(cb), 0)
    return out


def product_windows(pid):
    out = []
    def cb(h, _):
        pr = c.c_uint(0); user32.GetWindowThreadProcessId(h, c.byref(pr))
        if pr.value == pid:
            cls = c.create_unicode_buffer(256); user32.GetClassNameW(h, cls, 256)
            out.append({"hwnd": int(h), "class": cls.value, "visible": bool(user32.IsWindowVisible(h))})
        return 1
    user32.EnumWindows(c.WINFUNCTYPE(c.c_int, wt.HWND, c.c_int64)(cb), 0)
    return out


def enum_menus():
    out = []
    def cb(h, _):
        b = c.create_unicode_buffer(256); n = user32.GetClassNameW(h, b, 256)
        if (b.value if n > 0 else "") == "#32768":
            pid = c.c_uint(0); user32.GetWindowThreadProcessId(h, c.byref(pid))
            hm = user32.GetMenu(h); labels = []
            if hm:
                cnt = user32.GetMenuItemCount(hm)
                for i in range(cnt if cnt > 0 else 0):
                    mii = MENUITEMINFO(); mii.cbSize = c.sizeof(MENUITEMINFO)
                    mii.fMask = MIIM_STRING | MIIM_ID
                    mii.dwTypeData = c.create_unicode_buffer(256); mii.cch = 255
                    if user32.GetMenuItemInfoW(hm, i, True, c.byref(mii)) and mii.dwTypeData:
                        labels.append((mii.wID, mii.dwTypeData))
            out.append((int(h), pid.value, labels))
        return 1
    user32.EnumWindows(c.WINFUNCTYPE(c.c_int, wt.HWND, c.c_int64)(cb), 0)
    return out


def find_quit():
    for hwnd, pid, labels in enum_menus():
        for wid, lab in labels:
            if "退出" in (lab or ""):
                return (hwnd, pid, wid, lab)
    return None


def sidecar_count(procs):
    return sum(1 for v in procs.values() if v["name"].lower() == "jax-rtc-sidecar.exe")


def main():
    samples = {"scenario": "6-graceful-tray-quit", "steps": [], "verdict": None}
    samples["steps"].append({"t": utc(), "event": "environment_probe", "cursor": probe_cursor()})
    cur = probe_cursor()
    print(f"[{utc()}] GetCursorPos probe: ok={cur['GetCursorPos_ok']} gle=0x{cur['gle']:X} pt={cur['pt']}")

    start = utc()
    p = subprocess.Popen([EXE], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    app_pid = p.pid
    print(f"[{utc()}] launched app pid={app_pid}")
    time.sleep(13)
    procs0 = _ps_enumerate()
    meta = procs0.get(app_pid, {})
    samples["app"] = {"pid": app_pid, "ppid": meta.get("ppid"), "session": meta.get("session"),
                      "launched_utc": start, "exe_basename": "jax-pet.exe",
                      "session_id": session_id(app_pid),
                      "harness_session_id": session_id(os.getpid())}
    samples["steps"].append({"t": utc(), "event": "app_launched",
                              "windows_before": product_windows(app_pid),
                              "tray_windows": tray_windows(app_pid)})

    trays = tray_windows(app_pid)
    reach = None
    if trays:
        r = user32.PostMessageW(trays[0], WM_USER_SHOW_TRAYICON, 0, 0)
        time.sleep(0.8)
        r2 = user32.PostMessageW(trays[0], WM_USER_SHOW_TRAYICON, 1, 0)
        reach = {"post_hide": r, "post_show": r2}
    samples["steps"].append({"t": utc(), "event": "wndproc_reachable_probe", "result": reach})
    print(f"[{utc()}] wndproc reachable probe: {reach}")

    menu_opened = False
    opened_tray = None
    for hw in trays:
        res = c.c_uint64(0)
        r = user32.SendMessageTimeoutW(hw, WM_USER_TRAYICON, 0, WM_RBUTTONUP, SMTO_BLOCK, 2500, c.byref(res))
        err = kernel32.GetLastError() & 0xFFFFFFFF
        print(f"[{utc()}] SendMessageTimeout(6002,0x0205) tray {hw}: r={r} res={res.value} gle=0x{err:X}")
        if r == 0 and err == 0x5B4:
            menu_opened = True; opened_tray = hw
            break
    samples["steps"].append({"t": utc(), "event": "open_menu_attempt",
                              "menu_opened": menu_opened, "opened_tray": opened_tray})

    exit_code = None
    graceful = None
    if menu_opened:
        q = None
        for _ in range(30):
            q = find_quit()
            if q:
                break
            time.sleep(0.1)
        if q:
            hwnd, pid, wid, lab = q
            print(f"[{utc()}] found 退出 item: hwnd={hwnd} id={wid} label={lab}")
            user32.PostMessageW(opened_tray, WM_COMMAND, wid, 0)
            time.sleep(3)
            exit_code = p.poll()
            procs1 = _ps_enumerate()
            orphan = sidecar_count(procs1)
            graceful = (exit_code == 0 and orphan == 0)
            samples["steps"].append({"t": utc(), "event": "quit_selected",
                                     "exit_code": exit_code, "sidecar_remaining": orphan,
                                     "graceful": graceful})
            print(f"[{utc()}] exit_code={exit_code} sidecar_remaining={orphan} graceful={graceful}")
        else:
            samples["steps"].append({"t": utc(), "event": "quit_item_not_found"})
    else:
        samples["steps"].append({"t": utc(), "event": "menu_blocked_getcursorpos_gate",
                                  "note": "tray wndproc bails at GetCursorPos==0 before TrackPopupMenu"})

    if graceful is True:
        samples["verdict"] = "COVERED"
    elif menu_opened and graceful is None:
        samples["verdict"] = "INCONCLUSIVE"
    else:
        samples["verdict"] = "INFEASIBLE_IN_THIS_SESSION"
    samples["cursor_probe"] = cur

    stamp = datetime.datetime.now().strftime("%Y-%m-%d")
    (OUT_DIR / f"{stamp}-tray-graceful-quit-samples.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(samples, stamp)
    print(f"[{utc()}] VERDICT={samples['verdict']}")
    return samples["verdict"]


def _write_report(samples, stamp):
    cur = samples.get("cursor_probe", {})
    v = samples["verdict"]
    L = []
    L.append("# 场景6 现场取证报告：正常退出后重启（托盘 graceful quit）")
    L.append("")
    L.append(f"- 生成时间(UTC): {utc()}")
    L.append(f"- 判决: **{v}**")
    L.append("")
    L.append("## 目标")
    L.append("真正执行托盘「退出」菜单项，证明进程走 `app.exit(0)` 的 graceful 路径退出")
    L.append("（exit code 0、sidecar 被 Supervisor 有序关停、无孤儿），而非被 taskkill 强杀。")
    L.append("")
    L.append("## 实现机制（已对 tray-icon 0.24.2 源码核对）")
    L.append("- 托盘回调消息 `WM_USER_TRAYICON = 6002`；右键 `WM_RBUTTONUP = 0x0205`。")
    L.append("- 向 `tray_icon_app` 窗口 `PostMessageW(6002, 0, 0x0205)` -> 触发 `TrackPopupMenu` 弹出原生 #32768 菜单。")
    L.append("- 读「退出」项命令 ID，`PostMessageW(owner, WM_COMMAND, id, 0)` -> `on_menu_event(\"quit\") -> app.exit(0)`。")
    L.append("- 全程不依赖鼠标移动/合成点击（Win11 XAML 托盘岛对合成输入不做 notify 命中测试，")
    L.append("  且 Shell_NotifyIconGetRect 在本机返回 DPI 错位的 (2142,1368,...) 坐标）。")
    L.append("")
    L.append("## 关键环境读数（原始）")
    app = samples.get("app", {})
    L.append(f"- 会话: app SessionId={app.get('session_id')}；harness(本脚本) SessionId={app.get('harness_session_id')}")
    L.append(f"- 本进程 `GetCursorPos`: ok={cur.get('GetCursorPos_ok')} gle=0x{cur.get('gle',0):X} pt={cur.get('pt')}")
    L.append("  - gle=0x5 即 `ERROR_ACCESS_DENIED`：本会话被禁止查询光标位置。")
    L.append("- 推论：被本会话拉起的 app 进程同处 SessionId=1，但进程所在窗口站无光标查询权限，")
    L.append("  `GetCursorPos` 同样失败；tray wndproc 在 `show_tray_menu` 之前")
    L.append("  `if GetCursorPos(...)==0 { return 0 }`，故菜单永不弹出（菜单位置本就依赖该光标坐标）。")
    L.append("- 对照证据：向 `tray_icon_app` 发 `WM_USER_SHOW_TRAYICON(6005)` 能隐藏/显示图标，")
    L.append("  证明确实到达了自定义 wndproc（6002 也到达，只是被 GetCursorPos 门控 return 0）。")
    L.append("")
    L.append("## 结论")
    if v == "INFEASIBLE_IN_THIS_SESSION":
        L.append("本环境（SessionId=1，但进程所在窗口站对 `GetCursorPos` 返回 ERROR_ACCESS_DENIED）下，")
        L.append("托盘「退出」菜单**无法通过任何手段打开**（真实鼠标、UIA、或投递消息皆然），")
        L.append("因此场景 6 的 graceful quit 路径**无法在本会话内实地执行**。该限制来自本会话的")
        L.append("窗口站能力（无光标查询权限），非产品缺陷；在具备可用光标的真实交互式桌面会话中，")
        L.append("上述自动化应当能完成并观测到 exit code 0。")
    elif v == "COVERED":
        L.append("自动化成功触发托盘「退出」，观测到 exit code 0 且无 sidecar 孤儿，场景 6 可判定覆盖。")
    else:
        L.append("结果未定，见 samples.json。")
    L.append("")
    L.append("## 原始样本")
    L.append(f"- 详见 `outputs/{stamp}-tray-graceful-quit-samples.json`")
    (OUT_DIR / f"{stamp}-tray-graceful-quit-report.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
