"""S6 补证采样器：用户亲手托盘优雅退出的并行现场取证（旁观，零注入）。

背景（第三波审计 P1）：场景 6 tray 优雅退出从未真实执行。取证会话 GetCursorPos
ACCESS_DENIED(0x5)，产品 tray wndproc 光标查询失败即 return，托盘菜单在无光标
会话物理上打不开（2026-09-24/27/28 三次同因）——**唯一合规补证路径 = 用户在真实
交互桌面亲手执行**。本脚本不做任何注入/模拟/自动化，只旁观采样并判定。

判据（与六场景同口径，复用 win_popup_capture）：
  · start_seen        监控窗口内亲眼看到 jax-pet.exe 出现（「本次成功绑定本次新建」）
  · quit_observed     随后亲眼看到产品树全部消失（用户退出）
  · grace             退出观察窗（20s）内：sidecar 残留=0、孤儿=0、
                      产品后代可见控制台窗口=0
  · trusted_all_witnessed  全程 probe_trusted=True（防静默给 0）
  verdict = CLEAN_EXIT  | PARTIAL_NO_START | CONSOLE_SEEN | RESIDUE | UNTRUSTED | TIMEOUT

输出：
  outputs/2026-10-03-s6-manual-tray-quit-samples.jsonl   每行一个快照
  outputs/2026-10-03-s6-manual-tray-quit-verdict.json    终判
"""
import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import win_popup_capture as W  # noqa: E402

REPO = HERE.parent.parent
OUT = REPO / "outputs"
JSONL = OUT / "2026-10-03-s6-manual-tray-quit-samples.jsonl"
VERDICT = OUT / "2026-10-03-s6-manual-tray-quit-verdict.json"

STAMP = "2026-10-03T_s6-manual"
POLL = 1.0          # 采样间隔（probe 通道本身约 1-2s，实际节奏 2-3s）
GRACE_SECS = 20     # 产品树消失后的残留观察窗
MAX_SECS = 30 * 60  # 整体超时保护


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def root_pids(pr):
    return sorted(p["pid"] for p in pr["procs"] if p["name"].lower() == "jax-pet.exe")


def any_product(pr):
    return any(p["name"].lower() in W.PRODUCT_EXES for p in pr["procs"])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    OUT.mkdir(exist_ok=True)
    fh = JSONL.open("a", encoding="utf-8", newline="\n")

    t0 = time.time()
    start_seen = False
    quit_seen = False
    trusted_all = True
    n_samples = 0
    grace_until = None
    worst = {"console_product": 0, "orphans": 0, "sidecar_residue": 0}

    log("S6 手动托盘退出采样器启动——等待用户启动 jax-pet …")

    while time.time() - t0 < MAX_SECS:
        pr = W.probe_processes(timeout=30)
        if not pr["trusted"]:
            trusted_all = False
            log(f"!! probe untrusted: {pr.get('why_untrusted')}")
        has_root = bool(root_pids(pr))
        has_prod = any_product(pr)

        phase = "WAIT_START"
        if start_seen and has_prod:
            phase = "ALIVE"
        elif start_seen and not has_prod:
            phase = "GONE_OBSERVE"
            if grace_until is None:
                grace_until = time.time() + GRACE_SECS
                quit_seen = True
                log(">>> 产品树全部消失——进入 20s 残留观察窗")
        elif not start_seen and has_root:
            start_seen = True
            phase = "ALIVE"
            log(">>> 目标到 jax-pet.exe 首次出现（start_seen=True）")

        snap = W.snapshot(f"{STAMP}#{n_samples}", health=False)
        snap["phase"] = phase
        snap["start_seen"] = start_seen
        snap["sidecar_residue"] = sum(
            1 for p in pr["procs"] if p["name"].lower() == "jax-rtc-sidecar.exe")
        fh.write(json.dumps(snap, ensure_ascii=False) + "\n")
        fh.flush()
        n_samples += 1
        worst["console_product"] = max(worst["console_product"],
                                       snap["visible_console_windows_product"])
        worst["orphans"] = max(worst["orphans"], len(snap["orphans"]))
        worst["sidecar_residue"] = max(worst["sidecar_residue"],
                                       snap["sidecar_residue"]) if quit_seen else 0

        if quit_seen and time.time() >= grace_until:
            break
        time.sleep(POLL)

    fh.close()

    if not trusted_all:
        verdict = "UNTRUSTED"
    elif not start_seen:
        verdict = "TIMEOUT" if quit_seen is False and time.time() - t0 >= MAX_SECS else "PARTIAL_NO_START"
    elif not quit_seen:
        verdict = "TIMEOUT"
    elif worst["console_product"] > 0:
        verdict = "CONSOLE_SEEN"
    elif worst["orphans"] > 0 or worst["sidecar_residue"] > 0:
        verdict = "RESIDUE"
    else:
        verdict = "CLEAN_EXIT"

    v = {
        "verdict": verdict,
        "stamp": STAMP,
        "window_secs": round(time.time() - t0, 1),
        "n_samples": n_samples,
        "start_seen": start_seen,
        "quit_observed": quit_seen,
        "worst": worst,
        "trusted_all_witnessed": trusted_all,
        "criteria": {
            "tray_quit_observed": verdict == "CLEAN_EXIT",
            "sidecar_residue": 0,
            "orphans": 0,
            "visible_console_windows_product": 0,
        },
        "method": "human hand on tray menu; sampler passive-only (no injection/simulation)",
    }
    VERDICT.write_text(json.dumps(v, ensure_ascii=False, indent=2),
                       encoding="utf-8", newline="\n")
    log(f"verdict={verdict} samples={n_samples} worst={worst} start_seen={start_seen} quit={quit_seen}")


if __name__ == "__main__":
    main()
