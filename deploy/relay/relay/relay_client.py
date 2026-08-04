"""PC 侧中继客户端库（M2，独立于 app 可单独启动）

职责：连中继（role=pc，配对）→ 与本地 voice 网关 /ws/voice 桥接
- 手机音频帧（E2EE 密文）→ 解密 → 转发本地网关（明文 PCM 帧）
- 网关回复（控制帧 + 下行音频）→ 下行音频 E2EE 加密 → 上行回手机
- 传输层帧（ping/pong/heartbeat）在本客户端各自应答，不透传
- 断线重连：指数退避（1s→2s→4s→…上限 30s）；网关断线同样重连

用法（联调）：
    python -m backend.relay.relay_client --relay ws://127.0.0.1:19090/relay/ws \
        --pairing-code 123456 --gateway ws://127.0.0.1:8000/ws/voice
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import websockets

from .relay_protocol import (
    RelayE2EE,
    ReplayGuard,
    decode_audio_frame,
    encode_audio_frame,
    is_audio_frame,
    make_pair_frame,
)

logger = logging.getLogger(__name__)

RECONNECT_BACKOFF = [1, 2, 4, 8, 16, 30]


class RelayClient:
    """PC 侧客户端：relay ↔ 本地 voice 网关 双向桥接"""

    def __init__(
        self,
        relay_url: str,
        token: str,
        device_id: str,
        pairing_code: str,
        gateway_url: str = "ws://127.0.0.1:8000/ws/voice",
        e2ee: RelayE2EE | None = None,
    ) -> None:
        self.relay_url = relay_url
        self.token = token
        self.device_id = device_id
        self.pairing_code = pairing_code
        self.gateway_url = gateway_url
        self.e2ee = e2ee
        self._replay_up = ReplayGuard()          # 上行（手机→PC）防重放
        self._last_session_id = ""               # 新会话（重新配对）时 seq 从 0 重启，需重置防重放
        self._relay_ws: Any = None
        self._gw_ws: Any = None
        self._stop = False
        self._paired = asyncio.Event()
        self.peer_device: str = ""
        self.stats = {"up_audio": 0, "down_audio": 0, "control": 0, "reconnects": 0}

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        """连接 relay + gateway 并启动双向循环（阻塞直到 stop）"""
        await self._connect_gateway()
        await self._connect_relay()
        await asyncio.gather(
            self._relay_loop(),
            self._gateway_loop(),
        )

    async def stop(self) -> None:
        self._stop = True
        for ws in (self._relay_ws, self._gw_ws):
            if ws is not None:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---------- 连接 ----------
    async def _connect_relay(self) -> None:
        backoff_idx = 0
        while not self._stop:
            try:
                url = self.relay_url
                if self.token:
                    sep = "&" if "?" in url else "?"
                    url = f"{url}{sep}token={self.token}"
                self._relay_ws = await websockets.connect(url, ping_interval=None)
                await self._relay_ws.send(make_pair_frame("pc", self.device_id, self.pairing_code))
                async with asyncio.timeout(10):
                    raw = await self._relay_ws.recv()
                msg = json.loads(raw)
                if msg.get("type") != "paired":
                    raise RuntimeError(f"relay pair failed: {msg}")
                self.peer_device = msg.get("peer", {}).get("device_id", "")
                self._paired.set()
                logger.info("relay paired: session=%s peer=%s", msg.get("session_id"), self.peer_device)
                return
            except Exception as e:  # noqa: BLE001 - 重连退避
                self.stats["reconnects"] += 1
                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                logger.warning("relay connect failed (%s), retry in %ss", e, delay)
                backoff_idx += 1
                await asyncio.sleep(delay)

    async def _connect_gateway(self) -> None:
        backoff_idx = 0
        while not self._stop:
            try:
                self._gw_ws = await websockets.connect(self.gateway_url, ping_interval=None)
                await self._gw_ws.send(json.dumps({
                    "type": "hello", "role": "pc", "device_id": self.device_id,
                    "app_version": "relay-client-0.1.0", "engine": "relay",
                }, ensure_ascii=False))
                async with asyncio.timeout(10):
                    raw = await self._gw_ws.recv()
                msg = json.loads(raw)
                if msg.get("type") != "ready":
                    raise RuntimeError(f"gateway hello failed: {msg}")
                logger.info("voice gateway ready: %s", msg.get("session_id"))
                return
            except Exception as e:  # noqa: BLE001
                self.stats["reconnects"] += 1
                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                logger.warning("gateway connect failed (%s), retry in %ss", e, delay)
                backoff_idx += 1
                await asyncio.sleep(delay)

    # ---------- relay → gateway（手机 → PC） ----------
    async def _relay_loop(self) -> None:
        ws = self._relay_ws
        try:
            while not self._stop:
                raw = await ws.recv()
                if isinstance(raw, bytes):
                    if not is_audio_frame(raw):
                        continue
                    await self._up_audio(raw)
                else:
                    await self._up_control(raw)
        except Exception as e:  # noqa: BLE001 - relay 断开
            logger.warning("relay loop end: %s", e)

    async def _up_audio(self, raw: bytes) -> None:
        try:
            chunk = decode_audio_frame(raw)
            self._replay_up.check("phone", chunk.seq)
            payload = chunk.payload
            if self.e2ee is not None:
                payload = self.e2ee.decrypt_audio(chunk.seq, chunk.ts_ms, payload)
            await self._gw_ws.send(encode_audio_frame(chunk.seq, chunk.ts_ms, payload))
            self.stats["up_audio"] += 1
        except ValueError as e:
            logger.warning("drop up audio frame: %s", e)

    async def _up_control(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        mtype = msg.get("type")
        if mtype == "ping":
            await self._relay_ws.send(json.dumps({"type": "pong", "ts": time.time()}))
            return
        if mtype == "heartbeat":
            await self._relay_ws.send(json.dumps({"type": "pong", "ts": msg.get("ts", time.time())}))
            return
        if mtype in ("paired", "peer_left", "error"):
            logger.info("relay event: %s", mtype)
            if mtype == "paired" and msg.get("session_id") != self._last_session_id:
                # 新会话：手机 seq 从 0 重启，重置防重放（旧会话残留 seq 不误杀新帧）
                self._replay_up = ReplayGuard()
                self._last_session_id = msg.get("session_id", "")
            elif mtype == "peer_left":
                self._replay_up = ReplayGuard()
            return
        await self._gw_ws.send(raw)
        self.stats["control"] += 1

    # ---------- gateway → relay（PC → 手机） ----------
    async def _gateway_loop(self) -> None:
        ws = self._gw_ws
        try:
            while not self._stop:
                raw = await ws.recv()
                if isinstance(raw, bytes):
                    if not is_audio_frame(raw):
                        continue
                    await self._down_audio(raw)
                else:
                    await self._down_control(raw)
        except Exception as e:  # noqa: BLE001 - 网关断开
            logger.warning("gateway loop end: %s", e)

    async def _down_audio(self, raw: bytes) -> None:
        try:
            chunk = decode_audio_frame(raw)
            payload = chunk.payload
            if self.e2ee is not None:
                payload = self.e2ee.encrypt_audio(chunk.seq, chunk.ts_ms, payload)
            await self._relay_ws.send(encode_audio_frame(chunk.seq, chunk.ts_ms, payload))
            self.stats["down_audio"] += 1
        except ValueError as e:
            logger.warning("drop down audio frame: %s", e)

    async def _down_control(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        mtype = msg.get("type")
        if mtype in ("ping", "pong"):
            if mtype == "ping":
                await self._gw_ws.send(json.dumps({"type": "pong", "ts": time.time()}))
            return
        await self._relay_ws.send(raw)
        self.stats["control"] += 1


async def main() -> None:
    import argparse
    import os

    from .relay_protocol import load_e2ee_key

    parser = argparse.ArgumentParser(description="PC 侧中继客户端（联调用）")
    parser.add_argument("--relay", default=os.environ.get("RELAY_URL", "ws://127.0.0.1:19090/relay/ws"))
    parser.add_argument("--gateway", default=os.environ.get("VOICE_GATEWAY_URL", "ws://127.0.0.1:8000/ws/voice"))
    parser.add_argument("--pairing-code", required=True)
    parser.add_argument("--device-id", default="jax-pc-01")
    parser.add_argument("--token", default=os.environ.get("RELAY_TOKEN", ""))
    parser.add_argument("--e2ee-key", default=os.environ.get("RELAY_E2EE_KEY", ""))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    e2ee = RelayE2EE(load_e2ee_key(args.e2ee_key)) if args.e2ee_key else None
    client = RelayClient(args.relay, args.token, args.device_id, args.pairing_code,
                         gateway_url=args.gateway, e2ee=e2ee)
    try:
        await client.start()
    except KeyboardInterrupt:
        await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
