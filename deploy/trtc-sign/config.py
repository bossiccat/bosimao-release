"""trtc-sign 云函数配置（仅环境变量注入，禁硬编码密钥）

- TRTC_SDKAPPID：腾讯云 TRTC 应用 ID（int）
- TRTC_SECRETKEY：TRTC 控制台 SecretKey（唯一存云函数环境变量，禁入库/日志/代码）
- TRTC_ROOM_PREFIX：房间号前缀（默认 jax-）
- TRTC_USER_SIG_EXPIRE_S：userSig 有效期秒（契约 ≤600，默认 600）
- TRTC_DEVICE_WHITELIST：可选 device_id 白名单（逗号分隔；留空 = 放行全部）

纯标准库（os），云函数无需第三方依赖。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_ROOM_PREFIX = "jax-"
DEFAULT_EXPIRE_S = 600


class TrtcSignConfig:
    """云函数运行配置（惰性从环境变量加载一次）"""

    def __init__(self, env: dict | None = None) -> None:
        env = env if env is not None else os.environ
        self.sdk_app_id = self._parse_int(env.get("TRTC_SDKAPPID", ""), 0)
        self.secret_key = (env.get("TRTC_SECRETKEY") or "").strip()
        self.room_prefix = (env.get("TRTC_ROOM_PREFIX") or DEFAULT_ROOM_PREFIX).strip() or DEFAULT_ROOM_PREFIX
        self.expire_s = self._parse_int(
            env.get("TRTC_USER_SIG_EXPIRE_S", ""), DEFAULT_EXPIRE_S
        )
        # 契约硬约束：userSig 有效期 ≤ 600s
        if self.expire_s > 600:
            logger.warning("TRTC_USER_SIG_EXPIRE_S=%s 超过契约上限 600s，强制回退 600s", self.expire_s)
            self.expire_s = 600
        if self.expire_s <= 0:
            self.expire_s = DEFAULT_EXPIRE_S
        raw_whitelist = (env.get("TRTC_DEVICE_WHITELIST") or "").strip()
        self.device_whitelist = {x.strip() for x in raw_whitelist.split(",") if x.strip()}

    @staticmethod
    def _parse_int(raw: str, default: int) -> int:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def is_configured(self) -> bool:
        return bool(self.sdk_app_id and self.secret_key)
