"""P0 取证：下行/上行 PCM 落盘开关（JAX_DOWN_PCM_DUMP）

背景：埋点已证明 sidecar 出口 TTFB 正常、丢弃极少，但「PC 侧下行音频内容
本身是否干净（有无空隙/静音洞/采样错乱）」缺直接证据，TRTC→手机最后一跳
无法归因。开启 JAX_DOWN_PCM_DUMP=<prefix> 后：
- 下行：_send_frame 处（shaper 节拍后、实际发往 sidecar 的帧）原样落盘
  <prefix>.pcm——dump 里的洞 = 真实送达的洞
- 上行：_consume_up pop 后（实际送上云端的原始帧，队列丢弃另有计数）
  原样落盘 <prefix>.up.pcm
- close 时补写 <prefix>.meta.json（帧数/字节数/起止 mono/采样率/帧长）
写入不拖慢音频路径：内存缓冲，达阈值经 to_thread 异步落盘；I/O 异常只
警告一次，绝不影响下行。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from rtc_bridge.frame_meta import DownFrame
from rtc_bridge.pcm_dump import PcmDumpSink
from rtc_bridge.session import PeerVoiceSession


class StubApm:
    instances: list["StubApm"] = []

    def __init__(self, on_audio_out=None, on_text=None, on_state=None,
                 on_error=None, api_url="", system_prompt="", token="") -> None:
        self.fed: list[bytes] = []
        self.closed = False
        self.dead = False
        StubApm.instances.append(self)

    async def feed_pcm(self, pcm: bytes) -> None:
        if self.closed or self.dead:
            return
        self.fed.append(pcm)

    async def close(self) -> None:
        self.closed = True

    async def start(self) -> None:
        pass


@pytest.fixture
def stub_apm(monkeypatch):
    StubApm.instances = []
    monkeypatch.setattr("rtc_bridge.session.ApmBridge", StubApm)
    return StubApm.instances


async def _sent(msg: dict) -> None:
    pass


def _make_session(**kwargs) -> PeerVoiceSession:
    return PeerVoiceSession(
        device_id="dev-1", room_id="room-1", send_msg=_sent,
        apm_api_url="ws://fake", apm_system_prompt="p",
        **kwargs,
    )


# ---------- 关闭态：零文件零开销 ----------

@pytest.mark.asyncio
async def test_dump_disabled_by_default(monkeypatch, stub_apm):
    monkeypatch.delenv("JAX_DOWN_PCM_DUMP", raising=False)
    s = _make_session()
    await s.start()
    assert s._pcm_dump is None, "默认必须关闭（零开销路径）"
    await s._send_frame(DownFrame(payload=b"\x01\x02" * 320))
    await s.close()


# ---------- 开启态：字节守恒 + meta ----------

@pytest.mark.asyncio
async def test_sink_bytes_conserved_and_meta(tmp_path):
    """写入字节总数 == 推入字节数（假帧序列），meta 字段齐全"""
    prefix = str(tmp_path / "cap")
    sink = PcmDumpSink(prefix)
    down_frames = [bytes([i]) * 640 for i in range(1, 6)]   # 5×640B 假下行帧
    up_frames = [bytes([100 + i]) * 320 for i in range(4)]  # 4×320B 假上行帧
    for f in down_frames:
        sink.write_down(f)
    for f in up_frames:
        sink.write_up(f)
    await sink.close()

    down = (tmp_path / "cap.pcm").read_bytes()
    up = (tmp_path / "cap.up.pcm").read_bytes()
    assert down == b"".join(down_frames), "下行落盘必须逐字节等于推入序列"
    assert up == b"".join(up_frames), "上行落盘必须逐字节等于推入序列"

    meta = json.loads((tmp_path / "cap.meta.json").read_text(encoding="utf-8"))
    assert meta["sample_rate"] == 16000
    assert meta["frame_ms"] == 20
    assert "started_mono" in meta and "ended_mono" in meta
    assert meta["down"]["frames"] == 5 and meta["down"]["bytes"] == 5 * 640
    assert meta["up"]["frames"] == 4 and meta["up"]["bytes"] == 4 * 320


@pytest.mark.asyncio
async def test_session_wiring_down_and_up(tmp_path, monkeypatch, stub_apm):
    """session 接线：env 开启后下行帧（_send_frame）与上行帧（喂入 feeder 前）
    都进 dump；close 落 meta"""
    prefix = str(tmp_path / "sess")
    monkeypatch.setenv("JAX_DOWN_PCM_DUMP", prefix)
    s = _make_session()
    await s.start()
    await s.on_peer_enter("user-1")
    assert s._pcm_dump is not None, "env 开启后必须创建 sink"

    # 下行：3 帧 640B（经 shaper 节拍后实际发往 sidecar 的帧）
    down_frames = [bytes([i]) * 640 for i in range(1, 4)]
    for f in down_frames:
        await s._send_frame(DownFrame(payload=f))

    # 上行：响帧（RMS>400）经 on_up_audio → 队列 → 消费 → feeder 前捕获
    up_frames = [b"\x20\x10" * 160 for _ in range(3)]  # RMS≈4120
    for f in up_frames:
        await s.on_up_audio(f)
    for _ in range(200):
        if len(StubApm.instances[0].fed) >= 3:
            break
        await asyncio.sleep(0.01)

    await s.close()

    down = (tmp_path / "sess.pcm").read_bytes()
    assert down == b"".join(down_frames), "下行 dump 应等于实际发出的帧序列"
    up = (tmp_path / "sess.up.pcm").read_bytes()
    assert up == b"".join(up_frames), "上行 dump 应等于喂入 feeder 的原始帧"
    meta = json.loads((tmp_path / "sess.meta.json").read_text(encoding="utf-8"))
    assert meta["down"]["frames"] == 3 and meta["up"]["frames"] == 3


@pytest.mark.asyncio
async def test_open_failure_degrades_gracefully(tmp_path):
    """目录不可写：只降级（无文件、无异常、写入为 no-op），绝不影响调用方"""
    bad_prefix = str(tmp_path / "no_such_dir" / "cap")
    sink = PcmDumpSink(bad_prefix)
    sink.write_down(b"\x00" * 640)   # 不得抛异常
    sink.write_up(b"\x00" * 320)
    await sink.close()               # 不得抛异常
    assert not (tmp_path / "no_such_dir").exists()


# ---------- 证据隔离：同 prefix 重跑不得叠加污染 ----------

@pytest.mark.asyncio
async def test_same_prefix_second_run_does_not_append(tmp_path):
    """同 prefix 二次落盘必须换名，绝不叠加到上一轮文件上。

    取证场景下重跑是常态；若以 append 模式写进同一文件，两轮 PCM 会被拼成
    一段连续音频，事后几乎无法发现，直接导致归因结论错误。
    """
    prefix = str(tmp_path / "cap")

    first = PcmDumpSink(prefix)
    first.write_down(b"\x01" * 640)
    first.write_up(b"\x11" * 320)
    await first.close()
    first_down = (tmp_path / "cap.pcm").read_bytes()
    first_up = (tmp_path / "cap.up.pcm").read_bytes()
    assert first_down == b"\x01" * 640
    assert first_up == b"\x11" * 320

    second = PcmDumpSink(prefix)
    second.write_down(b"\x02" * 640)
    second.write_up(b"\x22" * 320)
    await second.close()

    # 首轮证据必须逐字节保持原样（既没被追加、也没被覆盖）——这是污染的直接判据，
    # 故意放在「是否换名」之前，好让 RED 阶段暴露真实缺陷而不是属性缺失
    assert (tmp_path / "cap.pcm").read_bytes() == first_down, "首轮下行证据被污染"
    assert (tmp_path / "cap.up.pcm").read_bytes() == first_up, "首轮上行证据被污染"
    # 第二轮必须换名：不得与首轮同一路径
    assert second.prefix != prefix, "同 prefix 重跑必须换名，不能复用已存在的目标"
    # 第二轮只含自己这一轮的字节
    assert Path(second.prefix + ".pcm").read_bytes() == b"\x02" * 640
    assert Path(second.prefix + ".up.pcm").read_bytes() == b"\x22" * 320
    # 第二轮的 meta 独立，且路径记录的是换名后的文件
    meta2 = json.loads(Path(second.prefix + ".meta.json").read_text(encoding="utf-8"))
    assert meta2["down"]["bytes"] == 640
    assert meta2["up"]["bytes"] == 320


@pytest.mark.asyncio
async def test_fresh_prefix_keeps_requested_name(tmp_path):
    """无冲突时保持调用方指定的名字（换名只在冲突时发生，不能无谓改变路径）"""
    prefix = str(tmp_path / "clean")
    sink = PcmDumpSink(prefix)
    sink.write_down(b"\x03" * 640)
    await sink.close()
    assert sink.prefix == prefix, "无冲突时不应改名"
    assert (tmp_path / "clean.pcm").read_bytes() == b"\x03" * 640
