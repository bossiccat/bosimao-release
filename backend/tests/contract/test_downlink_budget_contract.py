"""契约：下行队列预算必须能容纳一整段回复，且 config/session 两处默认值不得漂移。

背景（2026-09-13）
----------------
下行预算原为 200 帧 / 1s，但模型以**突发**方式一次推下整段回复（实测 6.88s 音频约
1.5s 墙钟到齐），而 shaper 按实时 50 帧/s 出队 ⇒ 帧在队列里停留 >1s 被当陈旧数据
丢弃，约 38% 音频整帧丢失（字被切断 ⇒「3 倍速 + 吐字不清」）。下行「早到」的帧是
待播内容而非陈旧遥测，预算尺度必须按整段回复配置。

本测试守护两条不变量：
1. 容量 ≥ 帧龄窗口：`down_max_frames * down_frame_ms >= down_max_frame_age_ms`
   （否则队列里必然有帧在被消费前就超龄）；
2. config.py 与 session.py 的下行默认值来自**同一组常量**（防两处再次漂移）。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from rtc_bridge.config import BridgeConfig  # noqa: E402
from rtc_bridge.session import PeerVoiceSession  # noqa: E402


def test_downlink_capacity_covers_frame_age_window():
    cfg = BridgeConfig()
    capacity_ms = cfg.down_max_frames * cfg.down_frame_ms
    assert capacity_ms >= cfg.down_max_frame_age_ms, (
        "下行必须能装下一整段回复而不触发帧龄丢弃："
        f"{cfg.down_max_frames} 帧 × {cfg.down_frame_ms}ms = {capacity_ms}ms "
        f"< max_frame_age_ms={cfg.down_max_frame_age_ms}ms"
    )


def test_downlink_bytes_match_frame_count():
    cfg = BridgeConfig()
    assert cfg.down_max_bytes == cfg.down_max_frames * 640, (
        "down_max_bytes 必须 = down_max_frames × 640B（16k mono s16 20ms 帧）"
    )


def test_downlink_defaults_agree_between_config_and_session():
    cfg = BridgeConfig()
    params = inspect.signature(PeerVoiceSession.__init__).parameters
    assert params["down_max_frames"].default == cfg.down_max_frames
    assert params["down_max_bytes"].default == cfg.down_max_bytes
    assert params["down_max_frame_age_ms"].default == cfg.down_max_frame_age_ms


def test_upstream_budget_kept_at_low_latency_telemetry_scale():
    """上行保持 100 帧 / 1s：迟到的上行帧确实无价值，该保护是对的。"""
    cfg = BridgeConfig()
    assert cfg.up_max_frames == 100
    assert cfg.up_max_frame_age_ms == 1000
