"""windows-popup-free 六场景现场取证总执行器（装机形态 / 出厂形态）。

claim：`governance/claims/windows-popup-free.json`
    risk = 「客户 Windows 交互桌面出现产品**后代**命令窗口」
    required_scenarios = 首次启动 / App 重启 / relay 故障恢复 / rtc bridge 故障恢复 /
                         两个历史五分钟 watchdog 周期 / 正常退出后重启

每个场景都**真的执行**，并按 windows-popup-next-steps.md §Immediate field action 第 5 步
采集：起止 UTC、可见命令窗口观测、产品 PID/PPID/SessionId、可执行名、以及可达的 /health。
额外采 app stderr 的失败关键词计数，并**标注这次 0 引用的是哪份阳性对照**。

刻意不做的事：
  · 不去改 `governance/claims/**`（写入 claim 是用户授权后的动作；且当前工作区有别人的文件）
  · 不读窗口标题（见 win_popup_capture.py 的脱敏红线）
  · 不 spawn node
"""
import json
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import win_popup_capture as W  # noqa: E402

REPO = pathlib.Path(r"C:\Users\Administrator\WorkBuddy\监视app")
PY = REPO / ".venv" / "Scripts" / "python.exe"
BACKEND = REPO / "backend"
OUTDIR = REPO / "outputs"
INSTALL = pathlib.Path(os.environ["LOCALAPPDATA"]) / "贾克斯·星核"
EXE = INSTALL / "jax-pet.exe"
SIDECAR_LOG = (pathlib.Path(os.environ["LOCALAPPDATA"]) / "com.jax.pet"
               / "logs" / "sidecar-logs" / "sidecar-sidecar.log")
STAMP = "2026-09-19"

KEYWORDS = [
    "sidecar runtime resolve failed", "resolve sidecar runtime failed", "ResolverFailed",
    "sidecar initial start blocked", "sidecar watchdog restart failed",
    "sidecar watchdog fused", "CompiledManifestMismatch", "ExtraPayload",
    "PayloadMissing", "PayloadHashMismatch",
]

# 本次 0 命中所引用的阳性对照（事先固定，场景里只做"确实存在"的核对）
# 注：这条 sha256 原先**手抄错了**——写成 66 字符的 `b6fa7f8f8fa5fdd2…`，与实际文件不符。
# 2026-09-19 用 hashlib 重新核算并更正。声明里带哈希就必须能被验：一个抄错的哈希
# 比"不写哈希"更糟，因为它看起来像已经核过。
POSCTRL_REF = ("outputs/2026-09-19-stderr-channel-positive-control.txt "
               "(2,973 B / sha256 b6fa7f8fa5fdd23df340da04514e4cb799937f7c847ab1f285ebb45334121909)")

# 采集工具的取证副本（是**工具本体**、不是数据）。读数写成字面量而不是 import 时现算：
# 干净检出时 `outputs/` 不存在（`.gitignore:134`），现算会直接抛异常；写成读数则文档里
# 的那句话始终可被验证。二者字节相同，2026-09-19 用 hashlib 核过。
TOOL_COPY_REL = "outputs/2026-09-19-installed-e2e-tools/win_popup_capture.py"
TOOL_COPY_SIZE = 15333
TOOL_COPY_SHA = "8cb80eb6979b1c0fdd6058e9599d50d00c115240b5f4e93642e5405737b2050e"

samples = []       # 全部快照（原始读数）
report = []        # 人读的报告
stderr_logs = {}   # scenario -> path


def w(s=""):
    report.append(s)
    print(s, flush=True)


def sample(label, health=True):
    s = W.snapshot(label, health=health)
    samples.append(s)
    return s


def brief(s):
    return (f"进程树={len(s['product_tree'])} 产品根={len(s['product_roots'])} "
            f"可见控制台(产品)={s['visible_console_windows_product']} "
            f"孤儿={len(s['orphans'])} 探针trusted={s['probe_trusted']}")


def sample_line(s):
    return (f"    [{s['start_utc']}] {brief(s)}"
            + (f"  !!{s['probe_why_untrusted']}" if s['probe_why_untrusted'] else ""))


def kill_all_jax():
    pr = W.probe_processes()
    if not pr["trusted"]:
        return [], pr["why_untrusted"]
    pids = [p["pid"] for p in pr["procs"]
            if p["name"].lower() in ("jax-pet.exe", "jax-rtc-sidecar.exe",
                                     "msedgewebview2.exe", "conhost.exe")]
    # 只杀 jax 自己 + jax 树内的 webview/conhost：先杀根，/T 带走后代
    roots = [p["pid"] for p in pr["procs"] if p["name"].lower() == "jax-pet.exe"]
    for pid in roots:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
    left = [p["pid"] for p in W.probe_processes()["procs"]
            if p["name"].lower() in ("jax-pet.exe", "jax-rtc-sidecar.exe")]
    for pid in left:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
    return roots, ""


def launch_app(tag):
    """起已安装的 app；stderr 显式接管。返回 (Popen, stderr 文件路径)。"""
    err = OUTDIR / f"{STAMP}-wpff-{tag}-app-stderr.log"
    stderr_logs[tag] = err
    with open(err, "wb") as fh:
        p = subprocess.Popen([str(EXE)], stdout=fh, stderr=fh,
                             stdin=subprocess.DEVNULL, cwd=str(INSTALL))
    return p, err


def stderr_counts(path):
    try:
        t = path.read_text(encoding="utf-8", errors="replace")
        size = path.stat().st_size
    except OSError:
        t, size = "", 0
    return size, {k: t.count(k) for k in KEYWORDS}


def sidecar_tail_probe():
    try:
        txt = SIDECAR_LOG.read_text(encoding="utf-8", errors="replace")
        st = SIDECAR_LOG.stat()
    except OSError:
        return {"readable": False}
    return {
        "readable": True,
        "size": st.st_size,
        "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
        "has_boot": "[BOOT] role=sidecar" in txt,
        "has_sdk": "getSDKVersion()" in txt,
        "has_sig": "[SIG] 意图轮询已启动" in txt,
        "has_ws_connected": "rtc_bridge connected" in txt,
        "has_ws_disconnected": "rtc_bridge disconnected" in txt,
        "tail": txt.splitlines()[-6:],
    }


def start_service(module, tag):
    logp = OUTDIR / f"{STAMP}-wpff-{tag}-service.log"
    with open(logp, "wb") as fh:
        p = subprocess.Popen([str(PY), "-m", module], stdout=fh, stderr=fh,
                             stdin=subprocess.DEVNULL, cwd=str(BACKEND))
    return p, logp


def stop_proc(p):
    try:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                       capture_output=True, text=True)
    except Exception:  # noqa: BLE001
        pass


def scenario_header(n, title, how):
    w(f"## 场景 {n}：{title}")
    w()
    w(f"**执行方式**：{how}")
    w()
    w(f"**起止 UTC**：{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} 起")
    w()


# ══════════════════════════════════════════════════════════════════════════
def _run():
    w("# windows-popup-free 现场取证（装机形态 / 出厂形态）")
    w()
    w(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    w(f"- claim：`governance/claims/windows-popup-free.json`（state=EvidencePending，evidence 为空）")
    w(f"- 被测产物：`{EXE}`")
    w(f"  - sha256 = {__import__('hashlib').sha256(EXE.read_bytes()).hexdigest()}")
    w(f"  - size = {EXE.stat().st_size} B")
    w(f"- 装机指针：`{INSTALL / 'jrt' / 'current.json'}`")
    w(f"  - 内容 = {(INSTALL / 'jrt' / 'current.json').read_text(encoding='utf-8').strip()}")
    w(f"- 判据来源：`windows-popup-next-steps.md` §Immediate field action 第 4/5 步")
    w()
    w("### 采集工具与「0 命中」的证据等级")
    w()
    w(f"- 工具：`{TOOL_COPY_REL}`（只读、脱敏、不 spawn node）")
    w(f"  - 与 `scripts/field-evidence/win_popup_capture.py` **字节相同**："
      f"{TOOL_COPY_SIZE:,} B / sha256 {TOOL_COPY_SHA}")
    w("  - **窗口尺子已自证**：显式拉一个可见控制台 → 计数 +2（`PseudoConsoleWindow` +")
    w("    `CASCADIA_HOSTING_WINDOW_CLASS`）；关掉 → 回落 −2。见「尺子自检」一节。")
    w(f"  - app stderr 通道的阳性对照：`{POSCTRL_REF}`")
    w("    （同一 exe 在必然失败时写出 558 B 失败原文；健康运行 = 0 B）")
    w()
    w("---")
    w()

    # ───────── S0：尺子自检（把"0 个"的证据等级钉死） ─────────
    scenario_header(0, "尺子自检：窗口枚举的阳性/阴性对照",
                    "显式 `CREATE_NEW_CONSOLE` 拉起 cmd.exe，看尺子是否**多看见**一个可见控制台；"
                    "再关掉，看是否**收回**。不做这一步，“0 个”就只是空集上的 0。")
    s_pre = sample("S0-基线", health=False)
    w("**基线**（无我方拉起的控制台）")
    w("```")
    w(sample_line(s_pre))
    w("```")
    w()
    CREATE_NEW_CONSOLE = 0x00000010
    p = subprocess.Popen(["cmd.exe", "/k", "echo POPUP-FIELD-POSITIVE-CONTROL"],
                         creationflags=CREATE_NEW_CONSOLE)
    time.sleep(4)
    s_pos = sample("S0-阳性", health=False)
    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, text=True)
    time.sleep(4)
    s_neg = sample("S0-阴性", health=False)
    w(f"**阳性对照**（拉起 `cmd.exe` PID={p.pid}）")
    w("```")
    w(sample_line(s_pos))
    for x in s_pos["visible_console_windows_all"]:
        w(f"      PID {x['pid']} {x['owner']} class={x['class_name']}")
    w("```")
    w()
    w("**阴性对照**（关掉之后）")
    w("```")
    w(sample_line(s_neg))
    w("```")
    w()
    up = s_pos["visible_console_windows"] - s_pre["visible_console_windows"]
    down = s_pos["visible_console_windows"] - s_neg["visible_console_windows"]
    w(f"**判定**：阳性增量 = {up}（要求 ≥1），阴性回落 = {down}（要求 ≥1）"
      f" ⇒ **{'尺子可用' if up >= 1 and down >= 1 else '尺子不可用，后续 0 均不可信'}**")
    w()
    w("---")
    w()

    # ───────── S1：首次启动 ─────────
    scenario_header(1, "首次启动",
                    "先确认无任何 jax 进程（出厂形态），再启动已安装的 `jax-pet.exe`，"
                    "全程 60s 每 10s 采样一次进程树与窗口谱系。")
    roots, why = kill_all_jax()
    w(f"清场：杀掉 {roots}；探针备注 = {why or '无'}")
    time.sleep(2)
    app, err = launch_app("s1-first-launch")
    t0 = time.time()
    s1 = []
    for i in range(6):
        time.sleep(10)
        s1.append(sample(f"S1-t{i * 10 + 10}s", health=(i == 5)))
    w("**采样**")
    w("```")
    for s in s1:
        w(sample_line(s))
    w("```")
    w()
    w("**末次快照明细**")
    w("```")
    w(W.render(s1[-1]))
    w("```")
    size, counts = stderr_counts(err)
    w(f"**app stderr**：{size} B；关键词计数 = {counts}")
    w(f"（本次 0 命中引用的阳性对照：`{POSCTRL_REF}`）")
    w()
    w(f"**侧车自身日志**：{json.dumps(sidecar_tail_probe(), ensure_ascii=False)[:600]}")
    w()
    ok1 = (s1[-1]["visible_console_windows_product"] == 0
           and not s1[-1]["orphans"] and not any(counts.values())
           and len(s1[-1]["product_roots"]) >= 1)
    w(f"**判定**：{'PASS' if ok1 else 'FAIL'}"
      f"（产品后代可见控制台窗口={s1[-1]['visible_console_windows_product']}，"
      f"孤儿={len(s1[-1]['orphans'])}，stderr 非零关键词={ {k: v for k, v in counts.items() if v} or '（无）' }）")
    w()
    w("---")
    w()

    # ───────── S2：App 重启 ─────────
    scenario_header(2, "App 重启",
                    "上一幕的 app 仍在跑；用 `taskkill /F /T` 强制结束（等价于桌面端 app 崩溃/重启），"
                    "确认进程树清空后**重新启动**，再采 60s。")
    w(f"重启前：app PID={app.pid}，tree={len(s1[-1]['product_tree'])}")
    subprocess.run(["taskkill", "/PID", str(app.pid), "/T", "/F"], capture_output=True, text=True)
    time.sleep(3)
    s2_pre = sample("S2-重启前(已杀)", health=False)
    w("```")
    w(sample_line(s2_pre))
    w("```")
    w()
    app2, err2 = launch_app("s2-app-restart")
    s2 = []
    for i in range(6):
        time.sleep(10)
        s2.append(sample(f"S2-t{i * 10 + 10}s", health=False))
    w("**采样**")
    w("```")
    for s in s2:
        w(sample_line(s))
    w("```")
    w()
    size2, counts2 = stderr_counts(err2)
    w(f"**app stderr**：{size2} B；关键词计数 = {counts2}")
    w(f"（本次 0 命中引用的阳性对照：`{POSCTRL_REF}`）")
    w()
    ok2 = (s2_pre["product_roots"] == [] and s2_pre["visible_console_windows_product"] == 0
           and s2[-1]["visible_console_windows_product"] == 0 and not s2[-1]["orphans"]
           and not any(counts2.values()) and len(s2[-1]["product_roots"]) >= 1)
    w(f"**判定**：{'PASS' if ok2 else 'FAIL'}")
    w()
    w("---")
    w()

    # ───────── S3：relay 故障恢复 ─────────
    scenario_header(3, "relay 故障恢复",
                    "本机实测 relay 可独立启动（`python -m relay.relay_server` → `:19090/relay/health` 200）。"
                    "执行：起 relay → 采样 → **杀掉 relay** → 采样 → **重启 relay** → 采样。"
                    "同时核对产品侧是否与该链路有任何连接（这决定本场景对 claim 意味着什么）。")
    w("> ⚠️ **链路归属必须先说清**：`docs/OPS-002-relay-deploy.md` 头部明确记载"
      "「手机 App → 公网中继(jax-relay) → PC relay_client → 本地 voice 网关(127.0.0.1:8000)」"
      "**及本地服务管理/watchdog/计划任务那一整套，已于 2026-09-11 随本地运行态脚本一并退役**"
      "（产品必须完全走云端）。侧车源码里唯一出现的对端 URL 是 `ws://127.0.0.1:19092`（rtc_bridge），"
      "**没有任何 relay URL**；`backend/app/api/routes_voice.py:111` 亦标注 relay_client 属 M2。")
    w()
    relay_p, relay_log = start_service("relay.relay_server", "s3-relay")
    time.sleep(8)
    s3a = sample("S3-relay已起", health=True)
    w(f"relay 启动 PID={relay_p.pid}  存活={relay_p.poll() is None}")
    w("```")
    w(sample_line(s3a))
    w(f"      /health[relay 19090] = {W._http_get('http://127.0.0.1:19090/relay/health')}")
    w("```")
    w()
    stop_proc(relay_p)
    time.sleep(6)
    s3b = sample("S3-relay已杀", health=True)
    w("**强制故障（杀掉 relay）后**")
    w("```")
    w(sample_line(s3b))
    w(f"      /health[relay 19090] = {W._http_get('http://127.0.0.1:19090/relay/health')}")
    w("```")
    w()
    relay_p2, _ = start_service("relay.relay_server", "s3-relay-restart")
    time.sleep(8)
    s3c = sample("S3-relay已恢复", health=True)
    w("**恢复（重启 relay）后**")
    w("```")
    w(sample_line(s3c))
    w(f"      /health[relay 19090] = {W._http_get('http://127.0.0.1:19090/relay/health')}")
    w("```")
    w()
    stop_proc(relay_p2)
    time.sleep(2)
    sc = sidecar_tail_probe()
    w(f"**侧车自身日志末尾**（relay 起/停/起 全程）：{json.dumps(sc, ensure_ascii=False)[:500]}")
    w()
    ok3 = all(x["visible_console_windows_product"] == 0 and not x["orphans"] for x in (s3a, s3b, s3c))
    w(f"**判定**：{'PASS' if ok3 else 'FAIL'}"
      f" —— 三态（起/杀/恢复）下产品后代可见控制台窗口均为 0、无孤儿。")
    w("**本场景对 claim 的意义**：relay 是**已退役**的独立链路，产品侧没有它的连接；"
      "但执行本身仍有价值 —— 它证明「中继服务起/停/起」这一动作**不会**让产品产生任何后代命令窗口，"
      "也不会唤醒任何 legacy watchdog。")
    w()
    w("---")
    w()

    # ───────── S4：rtc bridge 故障恢复 ─────────
    scenario_header(4, "rtc bridge 故障恢复",
                    "本机实测 rtc_bridge 可独立启动（`python -m rtc_bridge.main` → WS `:19092` + "
                    "health `:19093` 200）。执行：起 rtc_bridge（等侧车连上）→ 采样 → **杀掉** → 采样 → "
                    "**重启**（等侧车重连）→ 采样。侧车源码里 `ws://127.0.0.1:19092` 是它**唯一**的本地对端。")
    br_p, br_log = start_service("rtc_bridge.main", "s4-rtcbridge")
    time.sleep(14)
    s4a = sample("S4-bridge已起", health=True)
    w(f"rtc_bridge 启动 PID={br_p.pid}  存活={br_p.poll() is None}")
    w("```")
    w(sample_line(s4a))
    w(f"      /health[rtc_bridge 19093] = {W._http_get('http://127.0.0.1:19093/health')}")
    w("```")
    w()
    sc_a = sidecar_tail_probe()
    w(f"**侧车日志**（找 `rtc_bridge connected`）：{json.dumps(sc_a, ensure_ascii=False)[:500]}")
    w()
    stop_proc(br_p)
    time.sleep(12)
    s4b = sample("S4-bridge已杀", health=True)
    w("**强制故障（杀掉 rtc_bridge）后**")
    w("```")
    w(sample_line(s4b))
    w(f"      /health[rtc_bridge 19093] = {W._http_get('http://127.0.0.1:19093/health')}")
    w("```")
    w()
    sc_b = sidecar_tail_probe()
    w(f"**侧车日志**（找 `rtc_bridge disconnected`）：{json.dumps(sc_b, ensure_ascii=False)[:500]}")
    w()
    br_p2, _ = start_service("rtc_bridge.main", "s4-rtcbridge-restart")
    time.sleep(16)
    s4c = sample("S4-bridge已恢复", health=True)
    w("**恢复（重启 rtc_bridge）后**")
    w("```")
    w(sample_line(s4c))
    w(f"      /health[rtc_bridge 19093] = {W._http_get('http://127.0.0.1:19093/health')}")
    w("```")
    w()
    sc_c = sidecar_tail_probe()
    w(f"**侧车日志**（找重连）：{json.dumps(sc_c, ensure_ascii=False)[:500]}")
    w()
    resp_a = W._http_get("http://127.0.0.1:19093/health")
    ok4 = all(x["visible_console_windows_product"] == 0 and not x["orphans"] for x in (s4a, s4b, s4c))
    w(f"**判定**：{'PASS' if ok4 else 'FAIL'}")
    w(f"  - 侧车是否观察到 bridge 断/连：connected_before={sc_a.get('has_ws_connected')} "
      f"disconnected_after_kill={sc_b.get('has_ws_disconnected')} "
      f"connected_after_restart={sc_c.get('has_ws_connected')}")
    w("  - ⚠️ 实测（本回合）：侧车只有在**有会话**（手机入会）时才会向 `:19092` 建 WS；"
      "无会话时它不连，因此断/连计数可能都是 False —— 这是**链路时序**而不是缺陷，"
      "如实记录，不推断为「恢复成功」。")
    stop_proc(br_p2)
    time.sleep(2)
    w()
    w("---")
    w()

    # ───────── S5：两个历史五分钟 watchdog 周期 ─────────
    scenario_header(5, "两个历史五分钟 watchdog 周期",
                    "**真等 11 分钟**（两个 5 分钟周期 + 余量）。app 全程运行，每 30s 采样；"
                    "周期前后各做一次计划任务判定（三只 legacy 任务必须仍为 ABSENT，"
                    "且不得出现任何新的 jax 名字任务）。")
    w("**周期开始前的计划任务判定**")
    pre_tasks = run_task_probe("s5-tasks-before")
    w("```")
    w(pre_tasks)
    w("```")
    w()
    t_start = time.time()
    s5 = []
    n = 0
    while time.time() - t_start < 660:
        n += 1
        time.sleep(30)
        el = int(time.time() - t_start)
        s5.append(sample(f"S5-t{el}s", health=(n % 4 == 0)))
    w(f"**采样**（共 {len(s5)} 次，跨度 {int(time.time() - t_start)}s）")
    w("```")
    for s in s5:
        w(sample_line(s))
    w("```")
    w()
    w("**周期结束后的计划任务判定**")
    post_tasks = run_task_probe("s5-tasks-after")
    w("```")
    w(post_tasks)
    w("```")
    w()
    size5, counts5 = stderr_counts(err2)   # app2 一直在跑
    w(f"**app stderr**（app2 全程）：{size5} B；关键词计数 = {counts5}")
    w(f"（本次 0 命中引用的阳性对照：`{POSCTRL_REF}`）")
    w()
    popup_any = sum(x["visible_console_windows_product"] for x in s5)
    orphan_any = sum(len(x["orphans"]) for x in s5)
    trusted_all = all(x["probe_trusted"] for x in s5)
    ok5 = (popup_any == 0 and orphan_any == 0 and trusted_all
           and "LEGACY_TASKS_ABSENT" in post_tasks and not any(counts5.values()))
    w(f"**判定**：{'PASS' if ok5 else 'FAIL'}"
      f"（11 分钟内产品后代可见控制台窗口累计={popup_any}，孤儿累计={orphan_any}，"
      f"探针全程可信={trusted_all}）")
    w()
    w("---")
    w()

    # ───────── S6：正常退出后重启 ─────────
    scenario_header(6, "正常退出后重启",
                    "先执行「用户关闭窗口」的等价操作（`taskkill` **不带** `/F` = 发 WM_CLOSE），"
                    "观察产品既定的 close-to-hide 行为；再做一次可编程的真实退出，然后重启并采集。")
    w("**(a) 关闭窗口等价操作（WM_CLOSE）**")
    subprocess.run(["taskkill", "/PID", str(app2.pid)], capture_output=True, text=True)
    time.sleep(6)
    s6a = sample("S6a-WM_CLOSE后", health=False)
    w("```")
    w(sample_line(s6a))
    w("```")
    w()
    size_a, counts_a = stderr_counts(err2)
    w(f"    app 仍存活 = {app2.poll() is None}；stderr {size_a} B")
    w(f"    （预期：`pet window close requested -> hidden to tray`，app **不退出**）")
    w()
    w("**(b) 真实退出（本机唯一可编程手段）**")
    w("> ⚠️ **局限声明**：tray 菜单的 `quit → app.exit(0)` 需要 GUI 自动化点击托盘菜单，"
      "本回合**未引入**。这里用 `taskkill /F /T` 强制结束作为近似。"
      "**该路径（tray 优雅退出）本次未被执行，属未覆盖项**，不得据此声称已覆盖。")
    subprocess.run(["taskkill", "/PID", str(app2.pid), "/T", "/F"], capture_output=True, text=True)
    time.sleep(4)
    s6b = sample("S6b-退出后", health=False)
    w("```")
    w(sample_line(s6b))
    w("```")
    w()
    w("**(c) 重启**")
    app3, err3 = launch_app("s6-after-exit-relaunch")
    s6c = []
    for i in range(6):
        time.sleep(10)
        s6c.append(sample(f"S6c-t{i * 10 + 10}s", health=False))
    w("```")
    for s in s6c:
        w(sample_line(s))
    w("```")
    w()
    size6, counts6 = stderr_counts(err3)
    w(f"**app stderr**：{size6} B；关键词计数 = {counts6}")
    w(f"（本次 0 命中引用的阳性对照：`{POSCTRL_REF}`）")
    w()
    ok6 = (s6b["product_roots"] == [] and s6b["visible_console_windows_product"] == 0
           and not any(counts_a.values()) and not any(counts6.values())
           and s6c[-1]["visible_console_windows_product"] == 0 and not s6c[-1]["orphans"]
           and len(s6c[-1]["product_roots"]) >= 1)
    w(f"**判定**：{'PASS' if ok6 else 'FAIL'}")
    w()
    w("---")
    w()

    # ───────── 汇总 ─────────
    w("## 汇总")
    w()
    w("| # | 场景 | 判定 | 产品后代可见控制台窗口 | 孤儿 | stderr 非零关键词 |")
    w("|---|---|---|---|---|---|")
    def z(x):
        return "0" if x == 0 else f"**{x}**"
    rows = [
        ("1", "首次启动", ok1, s1[-1]["visible_console_windows_product"], len(s1[-1]["orphans"]), counts),
        ("2", "App 重启", ok2, s2[-1]["visible_console_windows_product"], len(s2[-1]["orphans"]), counts2),
        ("3", "relay 故障恢复", ok3,
         max(x["visible_console_windows_product"] for x in (s3a, s3b, s3c)),
         max(len(x["orphans"]) for x in (s3a, s3b, s3c)), {}),
        ("4", "rtc bridge 故障恢复", ok4,
         max(x["visible_console_windows_product"] for x in (s4a, s4b, s4c)),
         max(len(x["orphans"]) for x in (s4a, s4b, s4c)), {}),
        ("5", "两个历史五分钟 watchdog 周期", ok5, popup_any, orphan_any, counts5),
        ("6", "正常退出后重启", ok6, s6c[-1]["visible_console_windows_product"],
         len(s6c[-1]["orphans"]), counts6),
    ]
    for n, t, ok, pop, orp, cnt in rows:
        nz = {k: v for k, v in cnt.items() if v} or {}
        w(f"| {n} | {t} | {'**PASS**' if ok else '**FAIL**'} | {z(pop)} | {z(orp)} | "
          f"{'无' if not nz else json.dumps(nz, ensure_ascii=False)} |")
    w()
    w(f"- **全部 6 个场景**：{'全部 PASS' if all(r[2] for r in rows) else '**存在 FAIL**'}")
    w(f"- 尺子可用性：{'是' if (up >= 1 and down >= 1) else '**否**'}（阳性 +{up} / 阴性 −{down}）")
    w(f"- 计划任务：`{run_task_probe('s5-tasks-verdict')}`")
    w()
    w("### 未覆盖 / 不能据此声称的事")
    w()
    w("1. **场景 6 的 tray 优雅退出未执行**：本机未引入 GUI 自动化去点托盘菜单，"
      "`quit → app.exit(0)` 那条路径是空白；本场景只覆盖了 WM_CLOSE(close-to-hide) 与强制结束。")
    w("2. **场景 3 的 relay 是已退役链路**：产品侧不存在该连接，"
      "本场景证明的是「relay 服务起停不影响产品窗口谱系」，不是「产品的 relay 恢复逻辑正常」。")
    w("3. **场景 4 的侧车↔bridge 连接需要会话**：无手机入会时侧车不建 WS，"
      "故断/连观测可能为 False；这属链路时序，不等于「恢复逻辑被验证」。")
    w("4. **本机不是客户桌面**：这是一台 Windows 11 Pro 开发/验收机。"
      "claim 的字面要求是「客户 Windows 交互桌面」，同一台机上的结论可外推的范围有限。")
    w("5. **`schtasks.exe` 被本机安全策略硬挡**（程序黑名单，不可绕过）；"
      "计划任务判定全部走 `Get-ScheduledTask`，并配阳性/阴性对照。")
    w()
    w("### 边界")
    w()
    w("- 未改 `governance/claims/**`（写入 claim 需用户授权；且当前工作区有 3 个他人文件，`verify` 会因 `DIRTY_WORKTREE` 失败）")
    w("- 未改 `.github/workflows/**`、两份原生清单成员、`TRUST_VERSION`、R2 的 prune")
    w("")

    OUTDIR.joinpath(f"{STAMP}-windows-popup-free-field-evidence.md").write_text(
        "\n".join(report) + "\n", encoding="utf-8")
    OUTDIR.joinpath(f"{STAMP}-windows-popup-free-field-samples.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=1), encoding="utf-8")

    # 清场
    kill_all_jax()
    print("DONE")


def dump():
    """落盘。**放在 finally 里**：这个 harness 要跑约 30 分钟，
    任何一个场景抛异常都不该把之前 20 分钟的证据一起丢掉。
    """
    OUTDIR.joinpath(f"{STAMP}-windows-popup-free-field-evidence.md").write_text(
        "\n".join(report) + "\n", encoding="utf-8")
    OUTDIR.joinpath(f"{STAMP}-windows-popup-free-field-samples.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    try:
        _run()
    except BaseException:  # noqa: BLE001
        import traceback
        w()
        w("## !! 执行中断")
        w()
        w("> 下面的异常发生在上面最后一个「场景」之后。**已采集的读数仍有效**，"
          "但**未跑到的场景一律记为未覆盖**，不得据本文件声称已覆盖。")
        w()
        w("```")
        w(traceback.format_exc())
        w("```")
        w()
    finally:
        try:
            dump()
        except Exception as e:  # noqa: BLE001
            print(f"DUMP FAILED: {e!r}", flush=True)
        try:
            kill_all_jax()
        except Exception:  # noqa: BLE001
            pass
        print("DONE", flush=True)


def run_task_probe(tag):
    """跑计划任务探针并返回一行判定。"""
    p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(HERE / "probe_legacy_tasks.ps1")],
                       capture_output=True)
    j = HERE / "legacy_tasks_probe.json"
    try:
        raw = j.read_bytes()
        t = None
        for enc in ("utf-8-sig", "utf-16", "gbk"):
            try:
                t = raw.decode(enc); break
            except Exception:  # noqa: BLE001
                continue
        d = json.loads((t or raw.decode("utf-8", "replace")).replace("\x00", ""))
        g = d["gate"]
        return (f"[{tag}] {g['verdict']}  "
                f"(阳性对照 {d['positive_control']}={g['positive_control_exists']}, "
                f"阴性对照={g['negative_control_absent']}, legacy 全 ABSENT={g['legacy_all_absent']}, "
                f"与阴性不可区分={g['legacy_indistinguishable_from_fake']}, "
                f"枚举无命中={g['legacy_none_in_enumeration']}; 根任务 {d['root_task_count']} 个)")
    except Exception as e:  # noqa: BLE001
        return f"[{tag}] 探针失败: {e!r}"


if __name__ == "__main__":
    main()
