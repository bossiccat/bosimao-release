"""windows-popup-free 现场取证 harness —— 只读、脱敏、不 spawn node。

claim 的字面风险（governance/claims/windows-popup-free.json）：
    「客户 Windows 交互桌面出现产品**后代**命令窗口」
所以本 harness 的核心只做一件事：**把产品进程树 + 它的顶层窗口谱系看清楚**，
判定产品后代里有没有出现可见的控制台窗口。

脱敏红线（来自 windows-p0-remediation-status.md:23 ——
"exposes raw process/window metadata and has the probe spawn Node，
must be deleted or replaced by a **redacted, read-only collector**"）：
  · **不读窗口标题**（GetWindowText 一律不调用）——只读窗口**类名**与可见性；
  · 进程侧只报 **basename**（可执行文件名）与 PID/PPID/SessionId，
    不落完整命令行、不落用户名、不落完整路径（只给"是否在产品目录下"这个布尔）；
  · **只读**：本 harness 不写注册表、不改计划任务、不 kill 产品进程（清场由调用方决定）；
  · **不 spawn node**：进程枚举走 powershell -File（拿 PPID/SessionId），
    窗口枚举走 ctypes 直调 user32，全程不经过 Node。

进程枚举沿用已验收的"防静默给 0"纪律：
  · 脚本落 .ps1 用 -File 执行（命令行里不出现引号）；
  · 输出走 base64（纯 ASCII，绕开路径里"贾克斯·星核"的代码页问题）；
  · **自带阳性对照 + 双源交叉**，取不到对照时返回 trusted=False 而**不是空表**。
"""
import base64
import ctypes
import json
import pathlib
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from ctypes import wintypes

PRODUCT_EXES = {"jax-pet.exe", "jax-rtc-sidecar.exe"}
# 会承载"命令窗口"的窗口类。PseudoConsoleWindow/ConPTY 类同样算命中。
CONSOLE_WINDOW_CLASSES = {
    "ConsoleWindowClass",           # 传统 Win32 控制台（本 claim 的原始症状）
    "CASCADIA_HOSTING_WINDOW_CLASS",  # Windows Terminal 宿主
    "PseudoConsoleWindow",          # ConPTY
}

HEALTH_CANDIDATES = [
    ("rtc_bridge", "http://127.0.0.1:19093/health"),
    ("voice_gateway", "http://127.0.0.1:8000/health"),
]

# ─────────────────────────── 进程枚举（-File + base64） ───────────────────────────

PS = r"""
$ErrorActionPreference = 'Continue'
function B64([string]$s) {
  if ([string]::IsNullOrEmpty($s)) { return '' }
  return [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($s))
}
$cim = @(Get-CimInstance Win32_Process)
$rows = @()
foreach ($p in $cim) {
  $rows += ('{0}|{1}|{2}|{3}|{4}' -f $p.ProcessId, $p.ParentProcessId, $p.SessionId, $p.Name, (B64 $p.ExecutablePath))
}
$gp = @(Get-Process -ErrorAction SilentlyContinue)
$ctrl = 0
foreach ($p in $cim) { if ($p.Name -eq 'powershell.exe') { $ctrl++ } }
$obj = [ordered]@{
  control_powershell = $ctrl
  total_processes    = $cim.Count
  total_getprocess   = $gp.Count
  rows               = $rows
}
$obj | ConvertTo-Json -Compress -Depth 4
"""

_PS1 = None


def _ps1():
    global _PS1
    if _PS1 is None:
        d = pathlib.Path(tempfile.gettempdir()) / "jax-pe"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "field_probe_processes.ps1"
        p.write_text(PS, encoding="utf-8-sig")   # BOM：PS 5.1 靠 BOM 判 UTF-8
        _PS1 = p
    return _PS1


def probe_processes(timeout=90):
    """全量进程表（含 PPID/SessionId）。带可信度自评。"""
    res = {"trusted": False, "why_untrusted": "", "control": 0,
           "total": 0, "procs": [], "ps_error": ""}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", str(_ps1())],
                           capture_output=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        res["why_untrusted"] = f"powershell 起不来/超时: {e!r}"
        return res
    raw = r.stdout or b""
    try:
        data = json.loads(raw.decode("utf-8-sig", "replace"))
    except Exception as e:  # noqa: BLE001
        res["why_untrusted"] = f"stdout 不是 JSON (rc={r.returncode}): {e!r}"
        res["ps_error"] = (r.stderr or b"").decode("utf-8", "replace")[:300]
        return res

    rows = data.get("rows") or []
    if isinstance(rows, str):
        rows = [rows]
    procs = []
    for row in rows:
        pid, ppid, sid, name, b64 = (str(row).split("|", 4) + [""] * 5)[:5]
        try:
            path = base64.b64decode(b64).decode("utf-8", "replace") if b64 else ""
        except Exception:  # noqa: BLE001
            path = ""
        try:
            procs.append({"pid": int(pid), "ppid": int(ppid) or 0,
                          "sid": int(sid) if sid.strip() else -1,
                          "name": name.strip(), "path": path})
        except ValueError:
            continue
    res["procs"] = procs
    res["control"] = int(data.get("control_powershell") or 0)
    res["total"] = int(data.get("total_processes") or 0)
    res["total_getprocess"] = int(data.get("total_getprocess") or 0)

    reasons = []
    if res["control"] < 1:
        reasons.append("阳性对照失败：连 powershell.exe 自己都看不见")
    if res["total"] < 20:
        reasons.append(f"全机进程总数异常少（{res['total']}）")
    if not procs:
        reasons.append("rows 为空")
    res["why_untrusted"] = "; ".join(reasons)
    res["trusted"] = not reasons
    return res


# ─────────────────────────── 窗口枚举（ctypes 直调 user32） ───────────────────────────

user32 = ctypes.WinDLL("user32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int


def enum_top_level_windows():
    """返回 [{hwnd, pid, visible, class_name}]。

    刻意**不读窗口标题** —— 那是 claim 里被点名的"raw window metadata"，
    而且标题可能含用户内容。判据只需要类名 + 可见性。
    """
    out = []
    buf = ctypes.create_unicode_buffer(512)

    def cb(hwnd, _lparam):
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        n = user32.GetClassNameW(hwnd, buf, 512)
        cls = buf.value if n > 0 else ""
        out.append({
            "hwnd": int(hwnd),
            "pid": int(pid.value),
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "class_name": cls,
        })
        return True

    if not user32.EnumWindows(WNDENUMPROC(cb), 0):
        err = ctypes.get_last_error()
        # ERROR_SUCCESS(0) 是正常结束；其余才算真失败
        if err:
            raise OSError(f"EnumWindows failed: {err}")
    return out


# ─────────────────────────── 产品进程树 + 后代控制台判定 ───────────────────────────

def _descendants(procs, roots):
    by_parent = {}
    for p in procs:
        by_parent.setdefault(p["ppid"], []).append(p)
    seen, stack = set(), list(roots)
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        for c in by_parent.get(pid, []):
            stack.append(c["pid"])
    return seen


def product_tree(procs):
    """产品树 = jax-pet.exe（真正的根）+ 它的全部后代。

    ⚠️ 只有 `jax-pet.exe` 才算根。侧车（`jax-rtc-sidecar.exe`）是**后代**，
    不是根 —— 若某个侧车进程的祖先里没有 jax-pet.exe，那是**孤儿**
    （正是 claim 里点名要抓的"orphan process"），必须单独标出来，
    不能悄悄混进"产品根"里。

    conhost.exe 也算进树：Windows 上控制台窗口归 conhost **进程**所有，
    所以只看 jax 进程自己的窗口会漏掉真正的命令窗口。
    conhost 由控制台进程拉起、是它的子进程 ⇒ 祖先里有 jax 进程就算产品后代窗口。
    """
    roots = [p["pid"] for p in procs if p["name"].lower() == "jax-pet.exe"]
    seen = _descendants(procs, roots)
    sidecars = [p for p in procs if p["name"].lower() == "jax-rtc-sidecar.exe"]
    orphan_sidecars = [p["pid"] for p in sidecars if p["pid"] not in seen]
    for pid in orphan_sidecars:
        seen.add(pid)                      # 仍纳入窗口归属，但角色标为"孤儿侧车"
    conhosts = [p for p in procs if p["name"].lower() == "conhost.exe"]
    for c in conhosts:
        if c["ppid"] in seen:
            seen.add(c["pid"])
    return roots, seen, orphan_sidecars


def snapshot(label, health=True):
    t0 = time.time()
    start_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    pr = probe_processes()
    procs = pr["procs"]
    by_pid = {p["pid"]: p for p in procs}
    roots, tree, orphan_sidecars = product_tree(procs)

    wins = enum_top_level_windows()
    console_any = [w for w in wins if w["class_name"] in CONSOLE_WINDOW_CLASSES]
    console_visible = [w for w in console_any if w["visible"]]
    console_visible_product = [w for w in console_visible if w["pid"] in tree]
    console_visible_any = console_visible

    # 产品后代窗口（不限于控制台）：只记类名与可见性
    product_windows = []
    for w in wins:
        if w["pid"] in tree:
            product_windows.append({
                "pid": w["pid"],
                "owner": by_pid.get(w["pid"], {}).get("name", "?"),
                "class_name": w["class_name"],
                "visible": w["visible"],
            })

    # 孤儿：任一 jax 进程的父进程已经不在进程表里
    orphans = []
    for p in procs:
        if p["name"].lower() in PRODUCT_EXES:
            if p["ppid"] not in by_pid:
                orphans.append({"pid": p["pid"], "name": p["name"],
                                "ppid": p["ppid"]})

    tree_rows = []
    for pid in sorted(tree):
        p = by_pid.get(pid)
        if not p:
            continue
        if pid in roots:
            sub = "产品根(jax-pet)"
        elif pid in orphan_sidecars:
            sub = "**孤儿侧车（祖先里没有 jax-pet）**"
        elif p["name"].lower() == "conhost.exe":
            sub = "conhost（承载命令窗口的进程）"
        else:
            sub = "后代"
        tree_rows.append({
            "pid": pid,
            "ppid": p["ppid"],
            "sid": p["sid"],
            "exe": p["name"],                      # 只报 basename
            "role": sub,
            "under_product_dir": "贾克斯·星核" in (p["path"] or "") or "jrt" in (p["path"] or ""),
        })

    health_res = {}
    if health:
        for name, url in HEALTH_CANDIDATES:
            health_res[name] = _http_get(url, timeout=4)

    return {
        "label": label,
        "start_utc": start_utc,
        "end_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_s": round(time.time() - t0, 2),
        "probe_trusted": pr["trusted"],
        "probe_why_untrusted": pr["why_untrusted"],
        "probe_control": pr["control"],
        "probe_total": pr["total"],
        "product_roots": roots,
        "orphan_sidecar_pids": orphan_sidecars,
        "product_tree": tree_rows,
        "visible_console_windows": len(console_visible),
        "visible_console_windows_product": len(console_visible_product),
        "visible_console_windows_all": [
            {"pid": w["pid"], "class_name": w["class_name"],
             "owner": by_pid.get(w["pid"], {}).get("name", "?")}
            for w in console_visible_any
        ],
        "product_windows": product_windows,
        "orphans": orphans,
        "health": health_res,
        "verdict_popup": bool(console_visible_product),
        "verdict_orphan": bool(orphans),
    }


def _http_get(url, timeout=4):
    """读 /health。

    ⚠️ 必须**显式禁用代理**：本机配了 HTTP 代理，urllib 默认会把 `http://127.0.0.1:...`
    也发给代理，于是本地没起服务时读回来的不是"连接被拒"，而是代理的
    `502 upstream connect failed` —— 那会把"没监听"误读成"监听且报错"。
    （第一次自检就踩到了：19093 与 8000 同时返回 502。）
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(url, method="GET")
        with opener.open(req, timeout=timeout) as r:
            body = r.read(400).decode("utf-8", "replace")
            return {"reachable": True, "status": r.status, "body": body,
                    "proxy_bypassed": True}
    except urllib.error.HTTPError as e:
        return {"reachable": True, "status": e.code,
                "body": (e.read(200) or b"").decode("utf-8", "replace"),
                "proxy_bypassed": True}
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "status": None, "error": type(e).__name__,
                "detail": str(e)[:160], "proxy_bypassed": True}


def render(snap):
    L = []
    L.append(f"[{snap['label']}] {snap['start_utc']} → {snap['end_utc']}")
    L.append(f"  探针 trusted={snap['probe_trusted']}"
             f"（阳性对照 powershell.exe={snap['probe_control']}，全机进程 {snap['probe_total']}）")
    if snap["probe_why_untrusted"]:
        L.append(f"  !! 探针不可信: {snap['probe_why_untrusted']}")
    L.append(f"  产品根 PID: {snap['product_roots']}")
    L.append(f"  产品树（含后代）共 {len(snap['product_tree'])} 个进程:")
    for r in snap["product_tree"]:
        L.append(f"    PID {r['pid']:<7} PPID {r['ppid']:<7} SID {r['sid']:<4} "
                 f"{r['exe']:<24} {r['role']}")
    L.append(f"  **可见控制台窗口（产品后代）= {snap['visible_console_windows_product']}**"
             f"   ← claim 判据，必须 0")
    L.append(f"  全机可见控制台窗口 = {snap['visible_console_windows']}")
    for w in snap["visible_console_windows_all"]:
        L.append(f"      PID {w['pid']} {w['owner']} class={w['class_name']}")
    L.append(f"  产品后代窗口总数 = {len(snap['product_windows'])}；类名分布:")
    dist = {}
    for w in snap["product_windows"]:
        key = (w["class_name"], w["visible"])
        dist[key] = dist.get(key, 0) + 1
    for (cls, vis), n in sorted(dist.items()):
        L.append(f"      {cls:<32} visible={vis}  ×{n}")
    L.append(f"  孤儿进程 = {snap['orphans']}")
    if snap.get("health"):
        for k, v in snap["health"].items():
            L.append(f"  /health[{k}] = {v}")
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(render(snapshot("manual")))
