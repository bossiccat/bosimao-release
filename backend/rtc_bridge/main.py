"""rtc_bridge 入口（独立进程）：装配 → 起 WS 服务端 + 健康检查 → 常驻事件循环

运行（cwd=backend）：
    python -m rtc_bridge.main
    # 环境变量：RTC_BRIDGE_WS_PORT(19092) / RTC_BRIDGE_HEALTH_PORT(19093) / APM_* 可选
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

import websockets

from .config import load_bridge_config
from .health import HealthServer
from .server import BridgeServer

logger = logging.getLogger(__name__)


async def main_async() -> None:
    cfg = load_bridge_config()
    state: dict = {
        "sidecar_connected": False,
        "room_id": "",
        "device_id": "",
        "sidecar_sdk_version": "",
        "started_ts": time.time(),
    }

    bridge = BridgeServer(cfg, state)
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
