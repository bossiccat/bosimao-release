"""PC 侧中继客户端库（M2，独立于 app 可单独启动）

职责：连中继（role=pc，配对）→ 与本地 voice 网关 /ws/voice 桥接
- 手机音频帧（E2EE 密文）→ 解密 → 转发本地网关（明文 PCM 帧）
- 网关回复（控制帧 + 下行音频）→ 下行音频 E2EE 加密 → 上行回手机
- 传输层帧（ping/pong/heartbeat）在本客户端各自应答，不透传：对中继的 ping 回 pong（中继协议对称）；
  对网关的 ping 回 heartbeat（网关上行合法保活帧只有 heartbeat，pong 是下行帧）
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

import ssl
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

# 中继假死感知（Task4 加固）：
# - 注册后等待中继首个响应（心跳 ping 15s / paired / peer_left / error 等均算响应）
# - 连续 SUSPECT_DEAD_STRIKES 次"WS 连上但无任何响应"→ 判定中继实例异常 → 退避 SUSPECT_DEAD_BACKOFF_S
#   （而不是每 15s 空转重试，日志从 "relay event: error" 无限循环变为明确告警 + 暂停）
PAIR_RESPONSE_TIMEOUT_S = 20   # 需 > 中继 heartbeat_interval_s(15)，健康中继必能收到首帧
SUSPECT_DEAD_STRIKES = 3       # 连续 N 次"连上但无响应"判定假死
SUSPECT_DEAD_BACKOFF_S = 60    # 判定假死后的重连退避时长


class RelayClient:
    """PC 侧客户端：relay ↔ 本地 voice 网关 双向桥接"""

    def __init__(
        self,
        relay_url: str,
        token: str,
        device_id: str,
        pairing_code: str,
        gateway_url: str = "ws://127.0.0.1:8000/ws/voice",
        gateway_ca: str | None = None,
        e2ee: RelayE2EE | None = None,
        gateway_heartbeat_interval_s: float = 15.0,
    ) -> None:
        self.relay_url = relay_url
        self.token = token
        self.device_id = device_id
        self.pairing_code = pairing_code
        self.gateway_url = gateway_url
        self.gateway_ca = gateway_ca
        self.e2ee = e2ee
        # 网关保活：主动心跳间隔（对齐手机端 startHeartbeat 15s）。
        # 网关 heartbeat_timeout_s(60) 检查"距最后收帧"，只被动应答（收到 ping 才回）时
        # 恰好在发 ping 后立即检查 → 应答未到即超时；必须主动刷新（2026-08-05 现场 30s 必踢）
        self.gateway_heartbeat_interval_s = gateway_heartbeat_interval_s
        self._replay_up = ReplayGuard()          # 上行（手机→PC）防重放
        self._last_session_id = ""               # 新会话（重新配对）时 seq 从 0 重启，需重置防重放
        self._relay_ws: Any = None
        self._gw_ws: Any = None
        self._stop = False
        self._paired = asyncio.Event()
        self.peer_device: str = ""
        self.stats = {"up_audio": 0, "down_audio": 0, "control": 0, "reconnects": 0}
        # 中继假死感知状态
        self._pair_timeout_strikes = 0       # 连续"连上但无响应"次数
        self._suspect_dead_until = 0.0       # 判定假死后禁止重连的截止时间（epoch）

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        """连接 relay + gateway 并启动双向循环（阻塞直到 stop）；任一连接断开自动整体重连（常驻保障）"""
        await self._connect_gateway()
        await self._connect_relay()
        while not self._stop:
            tasks = [
                asyncio.create_task(self._relay_loop()),
                asyncio.create_task(self._gateway_loop()),
                asyncio.create_task(self._gateway_heartbeat()),
            ]
            try:
                # 任一 loop 退出（连接断开）→ 取消另一个 → 整体重连
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
            except Exception as e:  # noqa: BLE001
                logger.warning("loop exited unexpectedly: %s — reconnecting", e)
            if self._stop:
                break
            # 中继/网关连接断开：重新建立（内部指数退避）；配对由手机下次 pair 时恢复
            try:
                await self._connect_gateway()
                await self._connect_relay()
            except Exception as e:  # noqa: BLE001
                logger.warning("reconnect failed: %s", e)

    async def stop(self) -> None:
        self._stop = True
        for ws in (self._relay_ws, self._gw_ws):
            if ws is not None:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---------- 中继假死感知（Task4） ----------
    def _record_pair_timeout(self) -> None:
        """记录一次"WS 连上但中继无响应"；连续 SUSPECT_DEAD_STRIKES 次判定实例假死→退避"""
        self._pair_timeout_strikes += 1
        if self._pair_timeout_strikes >= SUSPECT_DEAD_STRIKES:
            self._suspect_dead_until = time.time() + SUSPECT_DEAD_BACKOFF_S
            self._pair_timeout_strikes = 0
            logger.warning(
                "relay instance suspected dead (pair timeout x%d), backoff %ds",
                SUSPECT_DEAD_STRIKES, SUSPECT_DEAD_BACKOFF_S,
            )
        else:
            logger.warning(
                "relay pair timeout (strike %d/%d), will recheck",
                self._pair_timeout_strikes, SUSPECT_DEAD_STRIKES,
            )

    async def _wait_relay_backoff(self) -> None:
        """若中继被判假死，等待剩余退避时间后再重连（避免假死期间疯狂重试）"""
        remaining = self._suspect_dead_until - time.time()
        if remaining > 0:
            logger.info("relay suspect-dead backoff: sleeping %.0fs before reconnect", remaining)
            await asyncio.sleep(remaining)

    # ---------- 连接 ----------
    async def _connect_relay(self) -> None:
        backoff_idx = 0
        while not self._stop:
            await self._wait_relay_backoff()
            try:
                url = self.relay_url
                if self.token:
                    sep = "&" if "?" in url else "?"
                    url = f"{url}{sep}token={self.token}"
                ws = await websockets.connect(url, ping_interval=None, proxy=None)
                await ws.send(make_pair_frame("pc", self.device_id, self.pairing_code))
                # 注册即成功：中继对"无对端配对"静默不响应（协议缺陷），等待 paired 会 10s 超时 →
                # 重连死循环 + 旧连接泄漏（注册表残留半死连接导致手机"配对成功但无回传"）。
                # paired 事件由手机配对时经 _relay_loop/_up_control 收到（session_id 变化重置防重放）。
                self._relay_ws = ws
                self._last_session_id = ""
                self._pair_timeout_strikes = 0
                logger.info("relay registered, waiting for phone pair (code=%s)", self.pairing_code)
                return
            except Exception as e:  # noqa: BLE001 - 重连退避
                self.stats["reconnects"] += 1
                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                logger.warning("relay connect failed (%s), retry in %ss", e, delay)
                backoff_idx += 1
                await asyncio.sleep(delay)

    async def _connect_gateway(self) -> None:
        backoff_idx = 0
        # wss 网关（TLS 底座后 backend 切 https）需信任自签 CA；ca 路径由 --gateway-ca 传入。
        ssl_ctx = None
        if self.gateway_ca and self.gateway_url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context(cafile=self.gateway_ca)
        while not self._stop:
            try:
                self._gw_ws = await websockets.connect(self.gateway_url, ping_interval=None, proxy=None, ssl=ssl_ctx)
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

    async def _gateway_heartbeat(self) -> None:
        """网关保活：周期性主动发 heartbeat（对齐手机端 15s 行为，见 __init__ 注释）。

        不依赖"收到网关 ping 才回"：网关 heartbeat_loop 发 ping 后**立即**检查 last_rx，
        被动应答必然晚于检查 → 每周期被误踢。主动 heartbeat 让 last_rx 持续刷新。
        """
        while not self._stop:
            await asyncio.sleep(self.gateway_heartbeat_interval_s)
            if self._stop or self._gw_ws is None:
                return
            try:
                await self._gw_ws.send(json.dumps({"type": "heartbeat", "ts": time.time()}))
            except Exception:  # noqa: BLE001 - 网关断开：退出触发 start() 整体重连
                return

    # ---------- relay → gateway（手机 → PC） ----------
    async def _relay_loop(self) -> None:
        while not self._stop:
            ws = self._relay_ws
            if ws is None:
                return
            try:
                # 假死感知：健康中继每 15s 发 heartbeat ping，20s 内必有响应；
                # 若 WS 连上但业务卡死（中继实例假死），recv 超时 → 记 strike 并退出触发整体重连
                raw = await asyncio.wait_for(ws.recv(), timeout=PAIR_RESPONSE_TIMEOUT_S)
                self._pair_timeout_strikes = 0   # 收到任何中继消息 → 中继业务存活
                if isinstance(raw, bytes):
                    if not is_audio_frame(raw):
                        continue
                    await self._up_audio(raw)
                else:
                    await self._up_control(raw)
            except asyncio.TimeoutError:
                self._record_pair_timeout()
                logger.warning("relay pair timeout: no response in %ss", PAIR_RESPONSE_TIMEOUT_S)
                return  # 退出让 start() 重连（重连前会遵守假死退避）
            except Exception as e:  # noqa: BLE001 - relay 断开 -> 退出让 start() 重连
                logger.warning("relay loop end: %s", e)
                return

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
        if mtype in ("ping", "pong"):
            if mtype == "ping":
                await self._relay_ws.send(json.dumps({"type": "pong", "ts": time.time()}))
            # pong = 中继对本站 ping 的应答回执（中继 forward 拦截 pong 并弹回，见 relay_server.py）。
            # 必须吞掉：透传给网关会被判"未知控制帧类型: pong" → 网关回 error →
            # error 透传中继 → 无 peer 时回 no_peer error → 15s 无限循环（2026-08-05 现场日志）
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
        while not self._stop:
            ws = self._gw_ws
            if ws is None:
                return
            try:
                raw = await ws.recv()
                if isinstance(raw, bytes):
                    if not is_audio_frame(raw):
                        continue
                    await self._down_audio(raw)
                else:
                    await self._down_control(raw)
            except Exception as e:  # noqa: BLE001 - 网关断开 -> 退出让 start() 重连
                logger.warning("gateway loop end: %s", e)
                return

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
                # 网关 ping 应答必须是 heartbeat，不是 pong：
                # 网关上行合法帧（schemas.CTRL_OK）只含 heartbeat，pong 是下行帧（网关→客户端）。
                # 回 pong 会被网关判为"未知控制帧类型"且不刷新 last_rx → 30s 后心跳超时被踢
                # （2026-08-05 现场：手机已连接但网关持续报"未知控制帧类型: pong"）。
                await self._gw_ws.send(json.dumps({"type": "heartbeat", "ts": time.time()}))
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
    parser.add_argument("--gateway-ca", default=os.environ.get("VOICE_GATEWAY_CA", ""),
                        help="wss 网关的自签 CA 证书路径（certs/ca.crt），信任它用于 TLS 校验")
    parser.add_argument("--pairing-code", required=True)
    parser.add_argument("--device-id", default="jax-pc-01")
    parser.add_argument("--token", default=os.environ.get("RELAY_TOKEN", ""))
    parser.add_argument("--e2ee-key", default=os.environ.get("RELAY_E2EE_KEY", ""),
                        help="E2EE 密钥：32 字节 base64 或明文 passphrase（SHA-256 派生，与 App VoiceCipher 对齐）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    e2ee = RelayE2EE(load_e2ee_key(args.e2ee_key)) if args.e2ee_key else None
    client = RelayClient(args.relay, args.token, args.device_id, args.pairing_code,
                         gateway_url=args.gateway, gateway_ca=args.gateway_ca or None, e2ee=e2ee)
    try:
        await client.start()
    except KeyboardInterrupt:
        await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
