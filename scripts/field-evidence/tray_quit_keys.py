# -*- coding: utf-8 -*-
"""S6 keeper v2：真实键盘路径的托盘优雅退出取证。

与 tray_graceful_quit.py 的差别：
- 该工具用 GetMenu/GetMenuItemInfo 枚举菜单项 —— 对 TrackPopupMenu 弹出菜单
  结构性失效（弹出菜单不经 GetMenu 暴露），实测 INCONCLUSIVE。
- 本工具在菜单打开后发送真实键盘事件（END 定位末项「退出」→ RETURN 激活），
  与人类点击路径等价。会话须解锁（GetCursorPos ok=1）。

判定 COVERED：退出码 0 且无 sidecar 残留。
"""
import ctypes, ctypes.wintypes as wt, datetime, json, os, subprocess, sys, time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

EXE = os.path.join(os.environ["LOCALAPPDATA"], "贾克斯·星核", "jax-pet.exe")
OUT_DIR = r"C:\Users\Administrator\WorkBuddy\监视app\outputs"
WM_USER_SHOW_TRAYICON = 0x0600 + 1   # 与 tray_graceful_quit.py 相同约定
WM_USER_TRAYICON = 0x0600 + 2
WM_RBUTTONUP = 0x0205
SMTO_BLOCK = 0x0001

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_END, VK_RETURN, VK_DOWN, VK_ESCAPE = 0x23, 0x0D, 0x28, 0x1B


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", INPUTUNION)]


def utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def key(vk, up=False):
    ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=(KEYEVENTF_KEYUP if up else 0),
                    time=0, dwExtraInfo=None)
    inp = INPUT(type=INPUT_KEYBOARD, union=INPUTUNION(ki=ki))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def tap(vk):
    key(vk); time.sleep(0.06); key(vk, up=True); time.sleep(0.06)


def probe_cursor():
    p = wt.POINT()
    ok = user32.GetCursorPos(ctypes.byref(p))
    return ok, kernel32.GetLastError() & 0xFFFFFFFF


def tray_windows(pid):
    out = []
    def cb(h, _):
        pr = wt.DWORD(0); user32.GetWindowThreadProcessId(h, ctypes.byref(pr))
        if pr.value == pid:
            cls = ctypes.create_unicode_buffer(256); user32.GetClassNameW(h, cls, 256)
            if cls.value == "tray_icon_app":
                out.append(int(h))
        return 1
    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_int, wt.HWND, ctypes.c_longlong)(cb), 0)
    return out


def sidecar_count():
    n = 0
    for line in subprocess.run(["tasklist", "/FO", "CSV"], capture_output=True).stdout.decode("gbk", "replace").splitlines():
        if "jax-rtc-sidecar.exe" in line:
            n += 1
    return n


def main():
    samples = {"scenario": "6-graceful-tray-quit-v2-keys", "steps": [], "verdict": None}
    ok, gle = probe_cursor()
    samples["steps"].append({"t": utc(), "event": "cursor_probe", "ok": ok, "gle": gle})
    print(f"[{utc()}] GetCursorPos ok={ok} gle=0x{gle:X}")
    if not ok:
        print(f"[{utc()}] VERDICT=SESSION_LOCKED")
        samples["verdict"] = "SESSION_LOCKED"
        _save(samples); print(f"[{utc()}] VERDICT={samples['verdict']}"); return samples

    p = subprocess.Popen([EXE], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    app_pid = p.pid
    print(f"[{utc()}] launched app pid={app_pid}")
    time.sleep(13)

    trays = tray_windows(app_pid)
    samples["steps"].append({"t": utc(), "event": "tray_windows", "hwnds": trays})
    print(f"[{utc()}] tray windows: {trays}")
    if not trays:
        samples["verdict"] = "NO_TRAY_WINDOW"
        _save(samples); print(f"[{utc()}] VERDICT={samples['verdict']}"); return samples

    # 经典坑修复：应用不在前台时 TrackPopupMenu 菜单瞬间自关。
    # 用 AttachThreadInput + SetForegroundWindow 把前台让给应用的可见窗口。
    our_tid = kernel32.GetCurrentThreadId()
    fg_done = False
    def find_visible_tauri(pid):
        out = []
        def cb(h, _):
            pr = wt.DWORD(0); user32.GetWindowThreadProcessId(h, ctypes.byref(pr))
            if pr.value == pid:
                cls = ctypes.create_unicode_buffer(256); user32.GetClassNameW(h, cls, 256)
                if cls.value == "Tauri Window" and user32.IsWindowVisible(h):
                    out.append(int(h))
            return 1
        user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_int, wt.HWND, ctypes.c_longlong)(cb), 0)
        return out
    for vis in find_visible_tauri(app_pid):
        # 前台锁经典解法：先 tap ALT（本进程由此获得设前台许可），再 Attach+Set
        tap(0x12)  # VK_MENU / ALT
        time.sleep(0.15)
        app_tid = user32.GetWindowThreadProcessId(wt.HWND(vis), None)
        if app_tid:
            user32.AttachThreadInput(our_tid, app_tid, True)
            ok_fg = user32.SetForegroundWindow(wt.HWND(vis))
            user32.AttachThreadInput(our_tid, app_tid, False)
            fg_done = fg_done or bool(ok_fg)
    time.sleep(0.4)
    samples["steps"].append({"t": utc(), "event": "foreground_forced", "ok": fg_done})
    print(f"[{utc()}] foreground forced: {fg_done}")

    # 打开菜单：WM_RBUTTONUP；TrackPopupMenu 模态 → SendMessage 超时即「菜单已打开」
    opened = None
    for hw in trays:
        res = ctypes.c_size_t(0)
        r = user32.SendMessageTimeoutW(hw, WM_USER_TRAYICON, 0, WM_RBUTTONUP, SMTO_BLOCK, 2500, ctypes.byref(res))
        err = kernel32.GetLastError() & 0xFFFFFFFF
        print(f"[{utc()}] SendMessageTimeout tray {hw}: r={r} gle=0x{err:X}")
        samples["steps"].append({"t": utc(), "event": "open_menu", "hwnd": hw, "r": r, "gle": err})
        if r == 0 and err == 0x5B4:
            opened = hw
            break

    if not opened:
        samples["verdict"] = "MENU_NOT_OPENED"
        _save(samples); print(f"[{utc()}] VERDICT={samples['verdict']}"); return samples

    time.sleep(0.5)
    # 键盘导航：END → 末项「退出」→ RETURN 激活（真实输入事件路径）
    tap(VK_END); time.sleep(0.3); tap(VK_RETURN)
    print(f"[{utc()}] keys sent: END, RETURN")
    samples["steps"].append({"t": utc(), "event": "keys_sent", "keys": ["END", "RETURN"]})

    exit_code = None
    for _ in range(30):
        rc = p.poll()
        if rc is not None:
            exit_code = rc
            break
        time.sleep(0.2)

    time.sleep(2)
    sc = sidecar_count()
    graceful = (exit_code == 0 and sc == 0)
    samples["steps"].append({"t": utc(), "event": "quit_result",
                             "exit_code": exit_code, "sidecar_remaining": sc, "graceful": graceful})
    print(f"[{utc()}] exit_code={exit_code} sidecar_remaining={sc} graceful={graceful}")

    samples["verdict"] = "COVERED" if graceful else "QUIT_FAILED"
    _save(samples)
    print(f"[{utc()}] VERDICT={samples['verdict']}")
    return samples


def _save(samples):
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(OUT_DIR, f"{stamp}-tray-quit-v2-samples.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(samples, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
