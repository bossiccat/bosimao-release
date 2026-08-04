"""中继配置（backend/relay/ 独立启动；.env RELAY_TOKEN / RELAY_E2EE_KEY）

- RELAY_TOKEN：中继鉴权；未配置 → 开发态放行（日志告警）
- RELAY_E2EE_KEY：AES-256-GCM 预共享密钥（32 字节 base64）；未配置 → 生成开发密钥并告警
- 凭据仅从环境变量读取，不入库、不进日志
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from .relay_protocol import gen_dev_key_b64, load_e2ee_key

logger = logging.getLogger(__name__)

RELAY_PORT = 19090
RELAY_PATH = "/relay/ws"


@dataclass
class RelayConfig:
    host: str = "0.0.0.0"
    port: int = RELAY_PORT
    token: str = ""
    require_token: bool = False
    e2ee_key: bytes = b""
    e2ee_enabled: bool = False
    dev_key_b64: str = ""                       # 仅开发态：生成的密钥（供联调对端使用）
    heartbeat_interval_s: float = 15.0
    heartbeat_timeout_s: float = 60.0
    session_timeout_s: float = 600.0            # 未配对连接存活上限
    max_sessions_per_code: int = 1              # 单 pairing_code 并发会话 ≤1（单用户）
    extra: dict = field(default_factory=dict)


def load_relay_config(env: dict | None = None) -> RelayConfig:
    """从环境变量加载配置；env 可注入（测试用）"""
    env = env if env is not None else os.environ
    cfg = RelayConfig()
    cfg.token = (env.get("RELAY_TOKEN") or "").strip()
    cfg.require_token = bool(cfg.token)
    if not cfg.token:
        logger.warning("RELAY_TOKEN 未配置，中继处于开发态（无鉴权）")
    raw_key = (env.get("RELAY_E2EE_KEY") or "").strip()
    if raw_key:
        cfg.e2ee_key = load_e2ee_key(raw_key)
        cfg.e2ee_enabled = True
    else:
        cfg.dev_key_b64 = gen_dev_key_b64()
        cfg.e2ee_key = load_e2ee_key(cfg.dev_key_b64)
        cfg.e2ee_enabled = True
        logger.warning(
            "RELAY_E2EE_KEY 未配置，已生成开发密钥（联调对端需使用同一密钥）：%s", cfg.dev_key_b64
        )
    if env.get("RELAY_PORT"):
        try:
            cfg.port = int(env["RELAY_PORT"])
        except ValueError:  # noqa: BLE001 - 非法端口回落默认
            pass
    return cfg
