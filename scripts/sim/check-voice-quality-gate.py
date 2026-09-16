"""语音质量发布门禁 —— 三项指标（完整度 / 流畅度 / 打断质量）的**可执行阈值**。

为什么它是独立脚本，而不是 CI 里的一步
--------------------------------------
本门禁必须跑真实媒体面：Windows + Electron + 本机 TRTC 对端 + 一个已配对设备凭证。
GitHub Actions 的 ubuntu runner 上**根本跑不起来**（`sidecar/test/` 里已有三个文件断言
`node_modules/electron/dist/electron.exe` 这种 Windows 硬路径，实测无 node_modules 时
78 例里 7 例必失败）。所以把它放在 CI 里只会是一条永远红或永远被跳过步骤。
**诚实的做法：它是发布前在本机/Windows runner 上跑的门禁**，不是 CI 步骤。

判据（全部来自实测基线，2026-09-13 修复后）
-------------------------------------------
| 指标 | 阈值 | 依据 |
|---|---|---|
| 同文本语速比（语音口径） | **≥ 0.85** | 修复前 0.36×（下行队列丢 38% 音频）；修复后 5 轮 0.91–1.04 |
| `queue_drops_down` | **= 0** | 下行是"要播的内容"，丢一帧即用户少听一帧 |
| 下行 age-drop 告警 | **= 0** | 同上；这是"队列按整段回复配置"是否失效的直接信号 |
| `queue_drops_up` | 仅告警 | 上行按帧龄丢**语义上正确**（迟到帧无用），且实测 4 轮只出现 1 次 |
| 打断：`emit_stop_ms` 有值 | **每轮都要有** | 桥侧权威口径；手机侧口径实测会给 null，不可用 |

用法（整条命令内跑完，本平台会收割跨调用子进程）：
    ./.venv/Scripts/python.exe scripts/sim/check-voice-quality-gate.py [clean轮数] [barge轮数]
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "scripts" / "sim"
D = ROOT / "outputs" / "deploy-backup-20260911"
PY = sys.executable
ELECTRON = ROOT / "sidecar" / "node_modules" / "electron" / "dist" / "electron.exe"

# --- 阈值（契约测试会锁这几个数，改它们必须是有意识的决定） ---
MIN_RATIO_SPEECH = 0.85
REQUIRE_DOWNLINK_DROPS_ZERO = True
REQUIRE_BARGE_EMIT_MS = True


class GateRefusal(RuntimeError):
    """门禁前置条件不满足 —— 拒绝出数，绝不用「跳过」冒充「通过」。"""


def preflight() -> None:
    if sys.platform != "win32":
        raise GateRefusal(
            f"本门禁需要真实媒体面（Windows + Electron + TRTC 对端），当前平台 {sys.platform}。"
            "**不要**在 CI/容器里跑它，也不要把它降级成静态检查。"
        )
    if not ELECTRON.is_file():
        raise GateRefusal(
            f"缺少 Electron（{ELECTRON}）。先 `cd sidecar && npm ci`，否则跑出来的是假结果。"
        )
    if not (ROOT / ".env").is_file():
        raise GateRefusal("缺少 .env（控制面地址与凭据）。没有它无法配对，也拿不到真实数字。")


def _run_repeat(rounds: int, *, barge: bool) -> list[dict]:
    """跑一轮测量并要求产物是**本次新生成**的。

    ⚠️ 必须先把旧产物删掉。否则测量子进程若秒退（缺依赖、参数错、异常），
    门禁会读到**上一次运行遗留的 JSON** 并报"通过" —— 实测踩过：整轮 6 秒返回
    "通过"，而真实跑一轮要 90 秒。**过期产物冒充成功结果是典型的假绿。**
    """
    out = D / ("repeat-barge.json" if barge else "repeat-clean.json")
    out.unlink(missing_ok=True)          # 先删：不存在才可能证明是本次生成的
    argv = [PY, str(SIM / "measure-rate-repeat.py"), str(rounds)]
    if barge:
        argv.append("--barge")
    proc = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    if not out.is_file():
        raise GateRefusal(
            f"测量未产出 {out.name}（rc={proc.returncode}）；"
            f"stderr 尾部：{proc.stderr[-400:]}"
        )
    if proc.returncode != 0:
        raise GateRefusal(f"测量子进程非零退出 rc={proc.returncode}：{proc.stderr[-400:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def _check_clean(rows: list[dict]) -> tuple[list[str], list[str]]:
    fails, warns = [], []
    if not rows:
        fails.append("clean 轮次为空")
        return fails, warns
    for r in rows:
        tag = f"轮 {r.get('run')}"
        if r.get("state") != "replied" or not r.get("reply_frames"):
            fails.append(f"{tag}: 没拿到回复（state={r.get('state')} reply_frames={r.get('reply_frames')}）")
            continue
        ratio = r.get("ratio_speech")
        if ratio is None:
            fails.append(f"{tag}: 语速比不可得（文本提取或参照不可用）—— 不接受缺席，宁可不报")
        elif ratio < MIN_RATIO_SPEECH:
            fails.append(f"{tag}: 语速比 {ratio} < {MIN_RATIO_SPEECH} ⇒ **下行丢了音频**（不是模型说得快）")
        # ⚠️ 缺席（None / 键不存在）必须判 FAIL，与上面 `ratio_speech is None` 的缺席语义
        # **对齐**。旧写法 `and r.get("queue_drops_down")` 里 None 为假 ⇒ 把「压根没测到」
        # 判成「零丢帧 = 通过」，而同一个门禁对 ratio 的缺席却是硬 FAIL —— 两套语义。
        # 触发路径零前置条件：run-sim-e2e.py:167 用一次 5s urlopen 超时读桥指标，失败即把
        # summary["bridge_metrics"] 置 None，measure-rate-repeat.py:120-123 再 `or {}`
        # 取默认 ⇒ 字段整体缺失，而那一轮可能真的跑成功了。
        drops_down = r.get("queue_drops_down")
        if REQUIRE_DOWNLINK_DROPS_ZERO and drops_down is None:
            fails.append(f"{tag}: queue_drops_down 不可得（桥指标未取到）—— 不接受缺席，宁可不报")
        elif drops_down:
            fails.append(f"{tag}: queue_drops_down={drops_down} ≠ 0")
        down_age = [l for l in (r.get("age_drop_lines") or []) if "[down]" in l]
        if down_age:
            fails.append(f"{tag}: 出现下行 age-drop：{down_age[0]}")
        up_drops = r.get("queue_drops_up") or 0
        if up_drops:
            warns.append(f"{tag}: queue_drops_up={up_drops}（上行帧龄丢帧语义上正确，仅记录）")
    return fails, warns


def _check_barge(rows: list[dict]) -> tuple[list[str], list[str]]:
    fails, warns = [], []
    if not rows:
        warns.append("未跑打断轮次")
        return fails, warns
    for r in rows:
        tag = f"打断轮 {r.get('run')}"
        lines = r.get("barge_stop") or []
        if not lines:
            fails.append(f"{tag}: 未产出桥侧 `barge_in stop` 日志（权威口径缺席）")
            continue
        if REQUIRE_BARGE_EMIT_MS and "emit_stop_ms=" not in lines[-1]:
            fails.append(f"{tag}: 日志缺 emit_stop_ms：{lines[-1][:120]}")
        if "stop_ms=" in lines[-1] and "emit_stop_ms=" not in lines[-1]:
            fails.append(f"{tag}: 出现已废弃的裸 stop_ms= 口径（恒为 0，是量错事件）")
        if r.get("barge_in_attempted") is not True:
            warns.append(f"{tag}: 本轮没有真的插话（barge_in_attempted≠true）")
    return fails, warns


def main() -> int:
    try:
        preflight()
    except GateRefusal as exc:
        print(f"门禁拒绝运行：{exc}")
        return 2

    clean_n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    barge_n = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    print(f"跑 {clean_n} 轮完整回复 + {barge_n} 轮打断（约 {(clean_n + barge_n) * 45}s）...")
    fails, warns = [], []
    f1, w1 = _check_clean(_run_repeat(clean_n, barge=False))
    f2, w2 = _check_barge(_run_repeat(barge_n, barge=True))
    fails += f1 + f2
    warns += w1 + w2

    print("\n================ 语音质量门禁 ================")
    for w in warns:
        print(f"  [warn] {w}")
    for f in fails:
        print(f"  [FAIL] {f}")
    if fails:
        print(f"\n结论：**不通过**（{len(fails)} 项失败）—— 商业发布维持 NO-GO")
        return 1
    print(f"\n结论：**通过**（语速比 ≥{MIN_RATIO_SPEECH}，下行零丢帧，打断口径可判读）"
          f"{f'，另有 {len(warns)} 条告警' if warns else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
