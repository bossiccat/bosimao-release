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
import os
import ssl
import time
from typing import Any, Awaitable, Callable

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

    async def start(self) -> None:
        """连接 API + 会话初始化 + 启动接收循环（阻塞直到就绪）"""
        # 绕过系统代理：本机 Clash(127.0.0.1:7890) 未运行会劫持全部外连（2026-08-05 实测）
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
            os.environ.pop(k, None)
        import websockets

        headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
        try:
            self._ws = await websockets.connect(
                self._api_url, ssl=ssl.create_default_context(), additional_headers=headers,
                open_timeout=20.0, max_size=16 * 1024 * 1024,
            )
        except TypeError:
            # 旧版 websockets 用 extra_headers
            self._ws = await websockets.connect(
                self._api_url, ssl=ssl.create_default_context(), extra_headers=headers,
                open_timeout=20.0, max_size=16 * 1024 * 1024,
            )
        # 排队 → 就绪
        while True:
            msg = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=15))
            if msg.get("type") in ("session.queue_done", "queue_done"):
                break
            if msg.get("type") == "error":
                raise RuntimeError(f"API 排队失败: {msg}")
        # 会话初始化
        await self._ws.send(json.dumps({
            "type": "session.init",
            "payload": {"system_prompt": self._system_prompt},
        }))
        while True:
            msg = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=15))
            if msg.get("type") == "session.created":
                self._session_id = msg.get("session_id", "")
                logger.info("apm session created: %s", self._session_id)
                break
            if msg.get("type") == "error":
                raise RuntimeError(f"API 会话失败: {msg}")
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
        """断线重连：关旧连接 → 重新 start（连 ws + queue + session.init）；失败则标记不可用等下一块音频"""
        async with self._reconnect_lock:
            if self._closed:
                return
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:  # noqa: BLE001
                    pass
            self._ws = None
            if self._recv_task is not None:
                self._recv_task.cancel()
                self._recv_task = None
            try:
                await self.start()
            except Exception as e:  # noqa: BLE001
                logger.error("apm reconnect failed: %s", e)
                self._ws = None

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


# ---------- 独立验证 ----------
async def _verify(wav_path: str, out_path: str) -> None:
    import wave

    audio_out: list[bytes] = []
    text_out: list[str] = []
    first_out_t: float | None = None
    t0 = time.perf_counter()

    async def on_audio(pcm: bytes) -> None:
        nonlocal first_out_t
        if first_out_t is None:
            first_out_t = time.perf_counter()
            print(f"首音频 @{(first_out_t-t0)*1000:.0f}ms, {len(pcm)}B")
        audio_out.append(pcm)

    async def on_text(t: str) -> None:
        text_out.append(t)
        print(f"  [text @{(time.perf_counter()-t0)*1000:.0f}ms] {t!r}")

    bridge = ApmBridge(on_audio_out=on_audio, on_text=on_text)
    await bridge.start()
    print(f"会话就绪 @{(time.perf_counter()-t0)*1000:.0f}ms")

    w = wave.open(wav_path)
    pcm = w.readframes(w.getnframes())
    # 按 40ms 帧喂（模拟手机 40ms 采集帧）
    frame = 1600  # 40ms @16k = 1600 样本 = 3200B
    for i in range(0, len(pcm), frame * 2):
        await bridge.feed_pcm(pcm[i : i + frame * 2])
        await asyncio.sleep(0.04)
    # 尾部 3s 静音（VAD 判定说完）
    silence = b"\x00\x00" * 16000 * 3
    await bridge.feed_pcm(silence)
    # 等回复（最多 25s）
    deadline = time.perf_counter() + 25
    while time.perf_counter() < deadline and not audio_out:
        await asyncio.sleep(0.2)
    await bridge.close()

    print(f"文本: {''.join(text_out)[:200]!r}")
    print(f"音频块: {len(audio_out)}, 总字节: {sum(len(b) for b in audio_out)}")
    if audio_out and out_path:
        with wave.open(out_path, "wb") as wo:
            wo.setnchannels(1)
            wo.setsampwidth(2)
            wo.setframerate(16000)
            wo.writeframes(b"".join(audio_out))
        print(f"已保存: {out_path}")


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="ApmBridge 独立验证")
    parser.add_argument("--wav", required=True, help="16k s16 mono WAV 输入")
    parser.add_argument("--out", default="", help="输出 WAV（下行音频拼接）")
    args = parser.parse_args()
    asyncio.run(_verify(args.wav, args.out))


if __name__ == "__main__":
    main()
