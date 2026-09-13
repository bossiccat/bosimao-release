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
"""
from __future__ import annotations

import asyncio
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from rtc_bridge.session import PeerVoiceSession  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


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


# ---------- 保底源码扫描（原有用例，保留） ----------

def test_session_dispatches_flush_on_barge_in() -> None:
    s = (ROOT / "backend" / "rtc_bridge" / "session.py").read_text(encoding="utf-8")
    assert '"flush_downlink"' in s, "打断时必须下发 flush_downlink"
    # 必须与 shaper.reset() 同一处（都在 barge_in 开窗的路径上）
    idx_reset = s.find("self.shaper.reset()")
    idx_flush = s.find('"flush_downlink"')
    assert idx_reset != -1 and idx_flush != -1
    assert 0 < idx_flush - idx_reset < 1200, "flush 指令应紧邻 shaper.reset()（同一打断分支）"


def test_sidecar_executes_flush_on_pacer() -> None:
    s = (ROOT / "sidecar" / "rtc.js").read_text(encoding="utf-8")
    assert "flush_downlink" in s, "sidecar 必须响应 flush_downlink"
    assert "pacer.clear()" in s, "响应里必须真的清空节拍器队列"


def test_pacer_exposes_clear() -> None:
    s = (ROOT / "sidecar" / "downlink_pacer.js").read_text(encoding="utf-8")
    assert "clear()" in s and "_queue.length = 0" in s, "clear() 必须真正清空队列"
