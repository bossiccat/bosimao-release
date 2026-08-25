"""ApmBridge — MiniCPM-o Realtime API 云端全双工引擎（M3 路径 A 云版，spec §8.2）

桥接：VoiceSession（手机 WS） ↔ MiniCPM-o Realtime API（wss://minicpmo45.modelbest.cn/v1/realtime?mode=audio）

- 上行：手机 16k s16 PCM 帧 → 累积 1s 块 → float32 base64 → input.append（官方 chunk-ms=1000 节奏）
- 下行：API audio delta（24k f32 base64）→ 重采样 16k s16 PCM → 回调 on_audio_out（走现有二进制音频帧）
- 全双工：无 VAD 轮次控制，随时打断（force_listen=false，模型原生 barge-in）
- 鉴权：当前匿名可用（无需 key）；预留 Authorization 注入点
- 代理：必须绕过系统代理（本机 Clash 127.0.0.1:7890 未运行会劫持连接——2026-08-05 实测）

用法（独立验证）：
    python -m backend.app.voice.apm_bridge --wav tmp/poc_b3_ask_16k.wav --out tmp/bridge_out.wav
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Awaitable, Callable

from .apm_handshake import open_session
from .apm_reconnect import reconnect

import numpy as np

logger = logging.getLogger(__name__)

# MiniCPM-o Realtime API 端点（官方文档 https://minicpmo45.modelbest.cn/docs）
DEFAULT_API_URL = "wss://minicpmo45.modelbest.cn/v1/realtime?mode=audio"
DEFAULT_SYSTEM_PROMPT = "你是贾克斯，一个中文语音助手。回答简短自然，有问必答。"

# 上行块：官方 probe 默认 chunk-ms=1000（1s @16k s16 = 32000B）
UPLINK_CHUNK_BYTES = 16000 * 2  # 1s * 16bit
# API 输出：24k 单声道 float32
OUT_RATE = 24000
OUT_DTYPE = np.float32
# 下行转 16k s16（与现有上行/音频帧协议一致）
DOWN_RATE = 16000


def f32_to_s16_16k(audio_f32_24k: bytes) -> bytes:
    """24k f32 PCM → 16k s16 PCM（线性抽取 3:2 + int16 量化）"""
    arr = np.frombuffer(audio_f32_24k, dtype=np.float32)
    # 24k -> 16k：取每 3 样本的第 2 个（24k*2/3 = 16k）
    step = OUT_RATE / DOWN_RATE  # 1.5
    idx = (np.arange(int(len(arr) / step)) * step).astype(np.int64)
    idx = idx[idx < len(arr)]
    down = np.clip(arr[idx], -1.0, 1.0)
    return (down * 32767.0).astype(np.int16).tobytes()


class ApmBridge:
    """MiniCPM-o Realtime API 全双工桥接（单会话）"""

    def __init__(
        self,
        on_audio_out: Callable[[bytes], Awaitable[None]],
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_state: Callable[[str], Awaitable[None]] | None = None,
        api_url: str = DEFAULT_API_URL,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        token: str = "",
    ) -> None:
        self._on_audio_out = on_audio_out
        self._on_text = on_text
        self._on_state = on_state
        self._api_url = api_url
        self._system_prompt = system_prompt
        self._token = token
        self._ws: Any = None
        self._up_buf = bytearray()          # 16k s16 上行累积
        self._send_lock = asyncio.Lock()
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self._session_id = ""
        self._started = False               # 懒初始化：首个音频块到达才建会话（避免空闲连接被服务端回收）
        self._reconnect_lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        """APM 实时会话是否已真实建立（懒初始化完成后为 True）"""
        return self._started

    async def start(self) -> None:
        """连接 API + 会话初始化 + 启动接收循环（阻塞直到就绪）"""
        self._ws, self._session_id = await open_session(
            self._api_url, self._token, self._system_prompt
        )
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._started = True

    async def feed_pcm(self, s16_bytes: bytes) -> None:
        """上行：手机 16k s16 PCM 帧 → 累积 1s 块发送"""
        if self._closed:
            return
        if not self._started:
            # 懒初始化：首个音频块到达才建会话（relay 常驻时不能提前建——空闲会被服务端回收，
            # 2026-08-06 现场：01:27 建连、01:37 手机配对，连接已死 → uplink send failed → 无回复）
            await self.start()
            self._started = True
        if self._ws is None:
            return
        self._up_buf.extend(s16_bytes)
        while len(self._up_buf) >= UPLINK_CHUNK_BYTES:
            chunk = bytes(self._up_buf[:UPLINK_CHUNK_BYTES])
            del self._up_buf[:UPLINK_CHUNK_BYTES]
            await self._send_chunk(chunk)

    async def _send_chunk(self, s16_chunk: bytes) -> None:
        """发一个 1s 块：s16 → f32 → base64 → input.append；断线自动重连重发（一次）"""
        f32 = np.frombuffer(s16_chunk, dtype=np.int16).astype(np.float32) / 32768.0
        b64 = base64.b64encode(f32.tobytes()).decode("ascii")
        async with self._send_lock:
            for attempt in range(2):
                try:
                    await self._ws.send(json.dumps({
                        "type": "input.append",
                        "input": {"audio": b64, "force_listen": False},
                    }))
                    return
                except Exception as e:  # noqa: BLE001
                    if attempt == 0 and not self._closed:
                        logger.warning("apm uplink send failed (%s), reconnecting…", e)
                        await self._reconnect()
                        if self._ws is None:
                            return
                    else:
                        logger.warning("apm uplink send failed after reconnect: %s", e)
                        return

    async def _reconnect(self) -> None:
        """断线重连：关旧连接 → 重新 start；失败则等下一块音频重试。"""
        await reconnect(
            lock=self._reconnect_lock,
            is_closed=lambda: self._closed,
            get_ws=lambda: self._ws,
            set_ws=lambda value: setattr(self, "_ws", value),
            get_recv_task=lambda: self._recv_task,
            set_recv_task=lambda value: setattr(self, "_recv_task", value),
            start=self.start,
        )

    async def _recv_loop(self) -> None:
        """下行：SSE/JSON 事件循环 → audio delta 转 16k s16 → on_audio_out"""
        assert self._ws is not None
        while not self._closed:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=600)
            except asyncio.TimeoutError:
                logger.info("apm recv idle 600s, session timeout")
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("apm recv end: %s", e)
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "response.output.delta":
                kind = msg.get("kind")
                if kind == "text" and msg.get("text") and self._on_text:
                    await self._on_text(msg["text"])
                elif kind == "audio" and msg.get("audio"):
                    try:
                        pcm = f32_to_s16_16k(base64.b64decode(msg["audio"]))
                    except Exception as e:  # noqa: BLE001
                        logger.warning("apm audio decode fail: %s", e)
                        continue
                    await self._on_audio_out(pcm)
                elif kind == "listen" and self._on_state:
                    await self._on_state("listening")
            elif mtype == "session.closed":
                logger.info("apm session closed: %s", msg.get("reason"))
                break
            elif mtype == "error":
                logger.error("apm error: %s", msg)
                break

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "session.close", "reason": "user_stop"}))
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._recv_task is not None:
            self._recv_task.cancel()


if __name__ == "__main__":
    from .apm_verify import main

    main()
