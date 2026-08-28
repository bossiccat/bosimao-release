"""rtc_bridge 入口（独立进程）：装配 → 起 WS 服务端 + 健康检查 → 常驻事件循环

运行（cwd=backend）：
    python -m rtc_bridge.main
    # 环境变量：RTC_BRIDGE_WS_PORT(19092) / RTC_BRIDGE_HEALTH_PORT(19093) / APM_* 可选
    # BRAIN_API_URL 可选（如 http://127.0.0.1:8000/api/v1/brain），设则 AI 文本路由到 Brain
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
import urllib.request

import websockets

from .config import load_bridge_config
from .health import HealthServer
from .server import BridgeServer

logger = logging.getLogger(__name__)


def _make_brain_callback(api_url: str):
    """创建异步回调：AI 文本 → Brain API /intent 做意图提取。

    Brain 不可用时降级为日志（不影响语音会话）。
    """
    base = api_url.rstrip("/")

    async def on_voice_intent(text: str) -> None:
        payload = json.dumps({
            "text": text[:2000],
            "source": "voice",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/intent",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=5.0)
            body = json.loads(resp.read())
            task_id = body.get("data", {}).get("task_id", "?")
            logger.info("voice intent routed to brain: task=%s", task_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("voice intent brain API call failed (degraded): %s", e)

    return on_voice_intent


async def _make_agent_tool_callback(registry):
    async def handle(name: str, args: dict, call_id: str) -> str:
        del call_id
        result = registry.handle_tool(name, args)
        return json.dumps(result, ensure_ascii=False)
    return handle


async def main_async() -> None:
    cfg = load_bridge_config()
    state: dict = {
        "sidecar_connected": False,
        "room_id": "",
        "device_id": "",
        "sidecar_sdk_version": "",
        "started_ts": time.time(),
    }

    # Brain API 路由（可选）：设 BRAIN_API_URL 则 AI 文本自动路由到 Brain 做意图提取
    brain_api_url = os.environ.get("BRAIN_API_URL", "").strip()
    on_voice_intent = _make_brain_callback(brain_api_url) if brain_api_url else None
    if on_voice_intent:
        logger.info("voice intent routing enabled: brain_api=%s", brain_api_url)
    else:
        logger.info("voice intent routing disabled (BRAIN_API_URL not set)")

    bridge = BridgeServer(cfg, state, on_voice_intent=on_voice_intent)
    await bridge.start_command_consumer()
    health = HealthServer(
        cfg.health_host,
        cfg.health_port,
        state,
        on_test_audio=bridge.send_test_audio,
        test_audio_enabled=cfg.test_audio_enabled,
    )

    await health.start()
    logger.info("rtc_bridge starting: ws=127.0.0.1:%s health=127.0.0.1:%s",
                cfg.ws_port, cfg.health_port)

    async with websockets.serve(bridge.handler, cfg.ws_host, cfg.ws_port,
                                max_size=4 * 1024 * 1024, ping_interval=20, ping_timeout=60):
        logger.info("rtc_bridge ws server ready on %s:%s", cfg.ws_host, cfg.ws_port)
        # 常驻；Ctrl+C / SIGTERM 优雅退出
        stop_event = asyncio.Event()

        def _request_stop(*_a) -> None:
            stop_event.set()

        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _request_stop)
                except NotImplementedError:
                    pass  # Windows 部分信号不支持
        except Exception:  # noqa: BLE001
            pass

        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        await bridge.stop_command_consumer()
        await health.stop()
        logger.info("rtc_bridge stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
