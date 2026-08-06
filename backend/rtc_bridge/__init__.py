"""rtc_bridge —— PC 侧 RTC 桥（独立进程，RtcPeer 接口抽象见 ARCHITECTURE §5.2）

拓扑：
    sidecar(Electron trtc-electron-sdk) ──localhost WS :19092──▶ rtc_bridge.py
                                                                     │
                                                                     ▼
                                                          PeerVoiceSession
                                                          ├ EndDetectFeeder → ApmBridge.feed_pcm
                                                          ├ ApmBridge（MiniCPM-o，原样复用）
                                                          └ DownlinkShaper → on_audio_out → WS 下发 sidecar

健康检查：GET 127.0.0.1:19093/health、/metrics（看门狗判定用；待命态也算健康）。
运行（cwd=backend）：python -m rtc_bridge.main
"""
from .config import BridgeConfig, load_bridge_config
from .health import HealthServer
from .server import BridgeServer

__all__ = ["BridgeConfig", "load_bridge_config", "BridgeServer", "HealthServer"]
