"""PeerVoiceSession —— 1 房间 = 1 sidecar WS 连接 + 1 ApmBridge（PC-INTEGRATION §4.3）

职责：
- 上行：sidecar 收的手机音频（16k s16）→ EndDetectFeeder（停顿补静音）→ ApmBridge.feed_pcm
- 下行：ApmBridge.on_audio_out（16k s16）→ DownlinkShaper（拆帧 + 节拍）→ WS 下发 sidecar
- 生命周期：远端进入 → 重置说完判定/整形器；远端离开 → 释放 APM 会话（懒初始化保持）
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, Awaitable, Callable

from app.voice.apm_bridge import ApmBridge
from app.voice.end_detect import EndDetectFeeder

from .shaper import DownlinkShaper

logger = logging.getLogger(__name__)

# WS 消息类型（与 sidecar/bridge.js 对齐）
MSG_UP_AUDIO = "up_audio"
MSG_DOWN_AUDIO = "down_audio"
MSG_PEER_STATE = "peer_state"
MSG_CTRL = "ctrl"


class PeerVoiceSession:
    """单设备语音会话（手机 ↔ sidecar ↔ rtc_bridge ↔ apm_bridge ↔ MiniCPM-o）"""

    def __init__(
        self,
        device_id: str,
        room_id: str,
        send_msg: Callable[[dict], Awaitable[None]],
        apm_api_url: str,
        apm_system_prompt: str,
        apm_token: str = "",
        down_frame_ms: int = 20,
        sample_rate: int = 16000,
    ) -> None:
        self.device_id = device_id
        self.room_id = room_id
        self._send_msg = send_msg
        self.apm = ApmBridge(
            on_audio_out=self._on_audio_out,
            on_text=self._on_text,
            on_state=self._on_state,
            api_url=apm_api_url,
            system_prompt=apm_system_prompt,
            token=apm_token,
        )
        self.feeder = EndDetectFeeder(feed=self.apm.feed_pcm, sample_rate=sample_rate)
        self.shaper = DownlinkShaper(send_frame=self._send_frame, frame_ms=down_frame_ms, sample_rate=sample_rate)
        self._up_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._consumer: asyncio.Task | None = None
        self._peer_entered = False
        self._peer_user_id = ""
        self._closed = False
        self._started = False
        # 指标
        self.stats = {
            "up_frames": 0,
            "up_bytes": 0,
            "down_frames": 0,
            "down_bytes": 0,
            "last_peer_ts": 0.0,
            "apm_session_state": "idle",
            "reconnects": 0,
        }
        self.last_activity_ts = time.time()

    async def start(self) -> None:
        """启动上行消费协程 + 下行整形器（不进房，APM 保持懒初始化）"""
        if self._started:
            return
        self._started = True
        self.shaper.start()
        self._consumer = asyncio.create_task(self._consume_up())
        logger.info("rtc session started device=%s room=%s", self.device_id, self.room_id)

    # ---------- 上行 ----------
    async def on_up_audio(self, pcm: bytes) -> None:
        """sidecar 推来的手机 16k s16 → 入队（不阻塞 WS 回调）"""
        if self._closed:
            return
        self.last_activity_ts = time.time()
        self.stats["up_frames"] += 1
        self.stats["up_bytes"] += len(pcm)
        self._up_q.put_nowait(pcm)

    async def _consume_up(self) -> None:
        while not self._closed:
            pcm = await self._up_q.get()
            try:
                await self.feeder.feed(pcm)
            except Exception as e:  # noqa: BLE001
                logger.warning("feed apm failed: %s", e)

    # ---------- 下行 ----------
    async def _on_audio_out(self, pcm: bytes) -> None:
        """ApmBridge 下行回调 → 整形器"""
        if self._closed:
            return
        self.stats["down_frames"] += 1
        self.stats["down_bytes"] += len(pcm)
        await self.shaper.push(pcm)

    async def _send_frame(self, frame: bytes) -> None:
        await self._send_msg({"type": MSG_DOWN_AUDIO, "pcm_b64": base64.b64encode(frame).decode("ascii")})

    async def _on_text(self, text: str) -> None:
        logger.info("apm text: %s", text[:120])

    async def _on_state(self, state: str) -> None:
        self.stats["apm_session_state"] = state

    # ---------- 远端状态 ----------
    async def on_peer_enter(self, user_id: str) -> None:
        """手机（远端）加入：重置说完判定与整形器，防跨会话状态污染（relay 教训）"""
        self._peer_entered = True
        self._peer_user_id = user_id
        self.stats["last_peer_ts"] = time.time()
        self.feeder.reset()
        self.shaper.reset()
        logger.info("peer enter device=%s peer=%s", self.device_id, user_id)

    async def on_peer_leave(self, user_id: str) -> None:
        """手机（远端）离开：释放 APM 会话，回待命"""
        self._peer_entered = False
        self._peer_user_id = ""
        logger.info("peer leave device=%s peer=%s", self.device_id, user_id)
        try:
            await self.apm.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("apm close on peer leave failed: %s", e)
        self.stats["apm_session_state"] = "closed"

    # ---------- 关闭 ----------
    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.apm.close()
        except Exception:  # noqa: BLE001
            pass
        await self.shaper.stop()
        if self._consumer is not None:
            self._consumer.cancel()
            try:
                await self._consumer
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        logger.info("rtc session closed device=%s", self.device_id)
