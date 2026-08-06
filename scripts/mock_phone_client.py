"""全链路模拟联调脚本（M2）：mock_phone → relay_server → relay_client → voice 网关 → 回传

用法：
    python scripts/mock_phone_client.py                     # 骨架：内嵌 relay + mock 网关（mock STT/brain/TTS）
    python scripts/mock_phone_client.py --gateway real      # 真实网关：relay_client 对接 backend /ws/voice（需已起 8000）
    python scripts/mock_phone_client.py --rounds 3          # 多轮测 P50 端到端

链路：手机模拟端 → WS 中继（19090，纯透传）→ PC relay_client → 本地 voice 网关半双工 → 回传
断言：手机收到 reply_done + 下行音频非空；记录各段耗时 + 端到端（目标 <5s）。
E2EE：手机与 PC 共享 RELAY_E2EE_KEY（AES-256-GCM，AAD 含 seq 防重放）；中继只见密文。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402

from relay.config import RelayConfig, load_relay_config  # noqa: E402
from relay.relay_client import RelayClient  # noqa: E402
from relay.relay_protocol import (  # noqa: E402
    AUDIO_FRAME_HEADER_LEN,
    RelayE2EE,
    ReplayGuard,
    chunk_payload,
    decode_audio_frame,
    encode_audio_frame,
    make_pair_frame,
)
from relay.relay_server import create_relay_app  # noqa: E402

logger = logging.getLogger("mock_phone")


class MockVoiceGateway:
    """骨架 voice 网关：mock STT/brain/TTS（半双工路径 B，与 /ws/voice 同协议）"""

    def __init__(self, text: str = "帮我重构数据层", reply: str = "好的，收到：帮我重构数据层") -> None:
        self.text = text
        self.reply = reply
        self.audio = b"\xff\xfb" * 3200                     # 模拟 TTS 音频（~100ms）
        self.t_first_audio: float | None = None
        self.t_audio_end: float | None = None
        self.t_reply_done: float | None = None

    async def handler(self, ws) -> None:
        hello = json.loads(await ws.recv())
        assert hello.get("type") == "hello"
        await ws.send(json.dumps({"type": "ready", "session_id": "mock-gw",
                                  "audio": {"up": "pcm_s16le_16k", "down": "mp3_24k"}}))
        buf = bytearray()
        try:
            while True:
                raw = await ws.recv()
                if isinstance(raw, str):
                    msg = json.loads(raw)
                    t = msg.get("type")
                    if t in ("ping", "heartbeat"):
                        await ws.send(json.dumps({"type": "pong", "ts": time.time()}))
                    elif t == "audio_end":
                        self.t_audio_end = time.time()
                        await self._reply(ws, bytes(buf))
                        buf.clear()
                else:
                    if self.t_first_audio is None:
                        self.t_first_audio = time.time()
                    buf += raw[AUDIO_FRAME_HEADER_LEN:]
        except Exception as e:  # noqa: BLE001 - 网关断开
            logger.debug("mock gateway closed: %s", e)

    async def _reply(self, ws, pcm: bytes) -> None:
        # mock 处理：STT 固定文本 → brain/local 回复 → TTS 固定音频
        await ws.send(json.dumps({"type": "transcript", "text": self.text, "is_final": True}))
        await ws.send(json.dumps({"type": "session_state", "state": "speaking", "ts": time.time()}))
        await ws.send(json.dumps({"type": "audio_start", "format": "mp3_24k", "seq": 0}))
        for i, chunk in enumerate(chunk_payload(self.audio)):
            await ws.send(encode_audio_frame(i, int(time.time() * 1000), chunk))
        await ws.send(json.dumps({"type": "audio_end", "seq": len(self.audio) // (64 * 1024),
                                  "reason": "done"}))
        self.t_reply_done = time.time()
        await ws.send(json.dumps({"type": "reply_done", "route": "local",
                                  "text": self.reply, "ts": time.time()}))


async def run_phone_round(relay_url: str, token: str, pairing_code: str,
                          e2ee: RelayE2EE | None, pcm: bytes) -> dict:
    """手机模拟端：配对 → 发唤醒+音频 → 收回传 → 断言 reply_done + 音频"""
    url = relay_url
    if token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={token}"
    t = {}
    guard = ReplayGuard()
    down = bytearray()
    async with websockets.connect(url, proxy=None) as ws:
        t0 = time.time()
        await ws.send(make_pair_frame("phone", "mock-phone-01", pairing_code, token=token))
        paired = json.loads(await ws.recv())
        assert paired["type"] == "paired", f"pair failed: {paired}"
        t1 = time.time()

        t2 = time.time()
        await ws.send(json.dumps({"type": "wake", "ts": int(time.time() * 1000)}))
        await ws.send(json.dumps({"type": "audio_start", "ts": int(time.time() * 1000)}))
        seq = 0
        for chunk in chunk_payload(pcm, 1600):
            payload = e2ee.encrypt_audio(seq, int(time.time() * 1000), chunk) if e2ee else chunk
            await ws.send(encode_audio_frame(seq, int(time.time() * 1000), payload))
            seq += 1
        await ws.send(json.dumps({"type": "audio_end", "ts": int(time.time() * 1000)}))

        got_reply = False
        while True:
            raw = await ws.recv()
            if isinstance(raw, bytes):
                chunk = decode_audio_frame(raw)
                guard.check("down", chunk.seq)
                payload = e2ee.decrypt_audio(chunk.seq, chunk.ts_ms, chunk.payload) if e2ee else chunk.payload
                down += payload
            else:
                msg = json.loads(raw)
                if msg.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong", "ts": time.time()}))
                elif msg.get("type") == "reply_done":
                    t5 = time.time()
                    got_reply = True
                    break
    assert got_reply, "未收到 reply_done"
    assert len(down) > 0, "下行音频为空"
    return {"pair_s": t1 - t0, "e2e_s": t5 - t2, "down_bytes": len(down)}


async def wait_relay_up(port: int, timeout_s: float = 8.0) -> None:
    async with httpx.AsyncClient(timeout=1.0) as ac:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                r = await ac.get(f"http://127.0.0.1:{port}/relay/health")
                if r.status_code == 200:
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.15)
    raise RuntimeError(f"relay server 未在 {timeout_s}s 内就绪")


async def main() -> int:
    parser = argparse.ArgumentParser(description="M2 全链路模拟联调")
    parser.add_argument("--relay-port", type=int, default=19090)
    parser.add_argument("--relay-url", default=None,
                        help="外部中继地址（如 wss://公网中继/relay/ws）；提供时跳过本地内嵌中继，用于跨网络联调")
    parser.add_argument("--token", default=os.environ.get("RELAY_TOKEN", ""),
                        help="中继鉴权 token（默认取环境变量 RELAY_TOKEN）")
    parser.add_argument("--gateway-port", type=int, default=18000)
    parser.add_argument("--gateway", choices=["mock", "real"], default="mock",
                        help="mock=内嵌骨架网关；real=对接 backend /ws/voice(8000)")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--pairing-code", default="123456")
    parser.add_argument("--stt-text", default="帮我重构数据层")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    # E2EE 密钥：优先 .env RELAY_E2EE_KEY，缺省生成开发密钥（手机/PC 共享）
    env_key = (os.environ.get("RELAY_E2EE_KEY") or "").strip()
    e2ee = RelayE2EE(__import__("base64").b64decode(env_key)) if env_key else None
    dev_key_b64 = env_key or __import__("base64").b64encode(os.urandom(32)).decode()

    # 1) 中继：默认启动本地内嵌中继；--relay-url 提供时改用外部中继（跨网络联调）
    relay_url = args.relay_url
    relay_task = None
    relay_server = None
    if relay_url is None:
        relay_cfg = RelayConfig(port=args.relay_port, heartbeat_timeout_s=60, session_timeout_s=600)
        relay_app = create_relay_app(relay_cfg)
        relay_server = uvicorn.Server(uvicorn.Config(relay_app, host="127.0.0.1",
                                                     port=args.relay_port, log_level="warning"))
        relay_task = asyncio.create_task(relay_server.serve())
        await wait_relay_up(args.relay_port)
        relay_url = f"ws://127.0.0.1:{args.relay_port}/relay/ws"
        print(f"[relay] 中继已就绪 {relay_url} e2ee={'on' if e2ee else 'off'}")
    else:
        print(f"[relay] 使用外部中继 {relay_url} token={'on' if args.token else 'off'} "
              f"e2ee={'on' if e2ee else 'off'}（跳过本地内嵌中继）")

    # 2) 启动骨架网关（mock）或直连真实网关
    gw_task = None
    gw_url = f"ws://127.0.0.1:{args.gateway_port}/ws/voice"
    mock_gw = None
    if args.gateway == "mock":
        mock_gw = MockVoiceGateway(text=args.stt_text)
        gw_server = await websockets.serve(mock_gw.handler, "127.0.0.1", args.gateway_port)
        gw_task = asyncio.create_task(gw_server.wait_closed())
        print(f"[gateway] mock 骨架网关已就绪 ws://127.0.0.1:{args.gateway_port}/ws/voice")
    else:
        gw_url = os.environ.get("VOICE_GATEWAY_URL", "ws://127.0.0.1:8000/ws/voice")
        print(f"[gateway] 对接真实 voice 网关 {gw_url}（需 backend 已起 8000）")

    # 3) 启动 PC 侧 relay_client（与网关桥接）
    pc = RelayClient(
        relay_url=relay_url,
        token=args.token, device_id="jax-pc-01", pairing_code=args.pairing_code,
        gateway_url=gw_url, e2ee=e2ee,
    )
    pc_task = asyncio.create_task(pc.start())

    # 4) 手机端跑 N 轮
    pcm = bytes((i * 7) % 256 for i in range(32000))  # 模拟 ~1s PCM16 16k
    try:
        for r in range(1, args.rounds + 1):
            res = await run_phone_round(relay_url,
                                        args.token, args.pairing_code, e2ee, pcm)
            gw_proc = ""
            if mock_gw is not None and mock_gw.t_first_audio is not None and mock_gw.t_reply_done is not None:
                gw_proc = f"{mock_gw.t_reply_done - mock_gw.t_first_audio:.3f}"
            print(f"[round {r}] 配对 {res['pair_s']*1000:.0f}ms | "
                  f"网关处理 {gw_proc}ms | 端到端 {res['e2e_s']*1000:.0f}ms | 下行 {res['down_bytes']}B")
            if res["e2e_s"] >= 5.0:
                print("[FAIL] 端到端超过目标 5s")
                return 1
        print(f"[OK] {args.rounds} 轮全链路联调通过（目标 <5s）")
        return 0
    finally:
        await pc.stop()
        pc_task.cancel()
        if relay_server is not None:
            relay_server.should_exit = True
        if relay_task is not None:
            try:
                await asyncio.wait_for(relay_task, timeout=3)
            except Exception:  # noqa: BLE001
                pass
        if gw_task is not None:
            gw_task.cancel()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
