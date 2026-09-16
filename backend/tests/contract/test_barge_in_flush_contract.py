"""契约：打断必须**下沉冲刷到 sidecar**（不能只清 rtc_bridge 队列），且两条路径对称。

背景（2026-09-13）
----------------
`session.py` 的打断处理只做 `shaper.reset()`（清 rtc_bridge 未推送帧），但下游还有
sidecar 的 `DownlinkPacer` 队列（最多 50 帧 = 1 秒）。不清它，用户插话后旧回复会继续
播完这 1 秒 —— 实测打断延迟 1.75s，其中约 1s 由此而来（是引入节拍器时的代价，必须配冲刷）。

而审计发现一条**不对称**：本地能量打断路径做了 `shaper.reset()` + 下发 `flush_downlink`，
云端 VAD 路径（`_on_server_user_speech`）**只做 `shaper.reset()`**，没有下发 flush ——
云端判定打断时 sidecar 队列里的旧音频会继续播完（与实测打断延迟 ~1.15s 量级吻合）。

本测试守住三段：判定侧下发（**两条路径都测**，行为断言）→ 传输 → sidecar 侧执行。

2026-09-16 加固：第三段「sidecar 侧执行」原先只是源码字符串扫描
（`assert "pacer.clear()" in rtc.js`），对行为改变完全无感；现改为在
`sidecar/test/barge-in-flush-exec.test.js` 里用 vm 加载真实 rtc.js 驱动真实 ctrl 回调，
断言节拍器队列真的被清空，并由本文件实际执行该 node 用例。
"""
from __future__ import annotations

import asyncio
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from rtc_bridge.session import PeerVoiceSession  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-cloudrun.yml"
_SIDECAR_FLUSH_CASE = ROOT / "sidecar" / "test" / "barge-in-flush-exec.test.js"
_NODE = shutil.which("node")


# ---------- 不注入任何替身：真的走 PeerVoiceSession 默认路径 ----------
# 铁律「依赖注入的替身覆盖率 ≠ 默认路径覆盖率」：本文件早期版本用 monkeypatch 把
# `rtc_bridge.session.ApmBridge` 换成桩，那样只是**替身**被走通，默认路径仍未被覆盖。
# 实测（2026-09-13）真实 `ApmBridge.__init__` 完全惰性（不连网、不建 WS，仅创建
# asyncio.Lock），且它没有 `cancel_response` ⇒ `_cancel_model_response()` fail-soft
# 直接返回、不触网。因此这里**不 patch 任何东西**，构造真实引擎跑被测入口。
# send_msg 是被测对象的**边界端口**（注入它是合法的），断言的是被测代码真的把 ctrl
# 消息交给了它 —— 而不是"某个替身被调用过"。


def _loud_frame() -> bytes:
    """rms=2000 的 20ms 帧（> 本地 barge-in 门限 800）。"""
    return struct.pack("<320h", *([2000] * 320))


def _flush_msgs(sent: list[dict]) -> list[dict]:
    return [m for m in sent if m.get("action") == "flush_downlink"]


# ---------- 行为断言：两条打断路径都必须下发 flush_downlink ----------

def test_cloud_vad_barge_in_dispatches_flush():
    """云端 VAD 路径（_on_server_user_speech）必须下发 flush_downlink（本次修复点）。

    走**真实默认路径**：真实 ApmBridge，不 patch、不 mock。
    """
    sent: list[dict] = []

    async def send_msg(msg: dict) -> None:
        sent.append(msg)

    async def scenario() -> None:
        s = PeerVoiceSession(device_id="d", room_id="r", send_msg=send_msg,
                             apm_api_url="ws://fake", apm_system_prompt="p")
        s._down_speaking = True          # AI 正在播报
        await s._on_server_user_speech()

    asyncio.run(scenario())

    assert _flush_msgs(sent), (
        "云端 VAD 判定打断时必须下发 flush_downlink，否则 sidecar 里最多 1s 旧音频继续播完"
    )
    assert sent[0]["type"] == "ctrl"


def test_local_energy_barge_in_dispatches_flush():
    """本地能量路径（on_up_audio 的高能量持续确认）同样必须下发 flush_downlink。

    走**真实默认路径**：真实 ApmBridge，不 patch、不 mock。
    """
    sent: list[dict] = []

    async def send_msg(msg: dict) -> None:
        sent.append(msg)

    async def scenario() -> None:
        s = PeerVoiceSession(device_id="d", room_id="r", send_msg=send_msg,
                             apm_api_url="ws://fake", apm_system_prompt="p")
        s._down_speaking = True
        s._down_speaking_since = time.time() - 1.0   # 越过宽限期
        s._barge_in = False
        for _ in range(3):                            # 持续 3 帧（默认 sustain）
            await s.on_up_audio(_loud_frame())

    asyncio.run(scenario())

    assert _flush_msgs(sent), "本地能量判定打断时同样必须下发 flush_downlink"


def test_both_barge_in_paths_are_symmetric_in_source():
    """源码级保险：两条路径都必须出现 flush_downlink（防止未来只改一条）。"""
    s = (ROOT / "backend" / "rtc_bridge" / "session.py").read_text(encoding="utf-8")
    assert s.count('"flush_downlink"') >= 2, (
        "本地能量路径与云端 VAD 路径都应下发 flush_downlink（当前只有一条 = 不对称回归）"
    )


# ---------- sidecar 侧执行：行为断言（已取代原源码字符串扫描） ----------
# 说明：`test_session_dispatches_flush_on_barge_in` 仍是源码扫描，但它是**下面那条行为
# 断言之外的冗余保险**，且它守的是 Python 侧（本文件已用真实会话做了行为断言），
# 与「字符串在但逻辑死」那种假绿不同：Python 侧的行为断言在
# `test_local_energy_barge_in_dispatches_flush` / `test_cloud_vad_barge_in_dispatches_flush`。
# 唯一残留的纯扫描是 `test_pacer_exposes_clear`（见文末注释）。

def test_session_dispatches_flush_on_barge_in() -> None:
    s = (ROOT / "backend" / "rtc_bridge" / "session.py").read_text(encoding="utf-8")
    assert '"flush_downlink"' in s, "打断时必须下发 flush_downlink"
    # 必须与 shaper.reset() 同一处（都在 barge_in 开窗的路径上）
    idx_reset = s.find("self.shaper.reset()")
    idx_flush = s.find('"flush_downlink"')
    assert idx_reset != -1 and idx_flush != -1
    assert 0 < idx_flush - idx_reset < 1200, "flush 指令应紧邻 shaper.reset()（同一打断分支）"


def test_sidecar_executes_flush_on_pacer() -> None:
    """sidecar 侧的执行必须是**行为断言**，且必须真的在门禁里跑。

    为什么删掉了原来的源码字符串扫描（2026-09-16）
    ----------------------------------------------
    旧实现是：

        assert "flush_downlink" in src
        assert "pacer.clear()" in src

    把 rtc.js 的 `if (action === 'flush_downlink')` 改成 `... && false` 之后，
    这两个字符串依然都在 ⇒ **测试照样绿**，而线上打断冲刷已彻底失效（被打断的旧回复
    会把节拍器里最多 1 秒的积压播完，正是实测打断延迟 1.75s 的主要来源）。
    字符串扫描看见的是「源码里存在这句话」，要守的却是「收到 ctrl 时它真的执行了」。

    现在的行为断言在 `sidecar/test/barge-in-flush-exec.test.js`：用 vm 加载**真实**
    rtc.js，只把边界端口换成记录型替身，然后驱动真实的 ctrl 回调，断言节拍器队列
    真的从 30 帧变 0 帧。

    本用例守两段（缺一不可）
    ------------------------
      1. 那份行为用例存在，且**没有被 sidecar 门禁排除**——门禁用排除法，
         被排除等于永远不执行，写了也白写；
      2. 真的用 node 执行它并要求全绿。

    为什么这里不 skip：CI 的 `Pre-deploy gate - sidecar node tests` 与 backend 契约
    套件在**同一个 job** 里跑，node 是硬前提。缺失时静默跳过会让这条契约彻底消失，
    正是本仓反复出现的「未测到 ≡ 通过」假绿形态。
    """
    assert _SIDECAR_FLUSH_CASE.is_file(), (
        f"缺少 sidecar 打断冲刷的行为断言文件：{_SIDECAR_FLUSH_CASE.name}"
    )

    # 1) 必须落在门禁的「可跑」一侧。
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    assert "node --test" in workflow, "sidecar 门禁必须真的跑 node --test"
    m = re.search(r"exclude='([^']*)'", workflow)
    assert m, "sidecar 门禁的排除清单不见了（结构变了，请同步本用例）"
    assert "barge-in-flush-exec" not in m.group(1), (
        "新行为用例被 sidecar 门禁排除 ⇒ CI 永远不会执行它（等于没写）"
    )

    # 2) 真的执行它。
    assert _NODE, (
        "未找到 node：CI 的 sidecar 门禁与 backend 契约套件在同一个 job 里跑，"
        "node 是硬前提；请安装 node 或修正 PATH（不跳过——跳过等于让这条契约消失）"
    )
    proc = subprocess.run(
        [_NODE, "--test", str(_SIDECAR_FLUSH_CASE)],
        cwd=str(ROOT / "sidecar"),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=180,
    )
    assert proc.returncode == 0, (
        "sidecar 打断冲刷行为断言未通过\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_pacer_exposes_clear() -> None:
    """残留的源码扫描：其**行为**覆盖在 `sidecar/test/downlink_pacer.test.js`
    （`clear() 立即丢弃全部待发帧，并计入 dropped（但不动 sent）`），本用例只做存在性兜底。
    """
    s = (ROOT / "sidecar" / "downlink_pacer.js").read_text(encoding="utf-8")
    assert "clear()" in s and "_queue.length = 0" in s, "clear() 必须真正清空队列"
