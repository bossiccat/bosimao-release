"""RTC 会话签发服务（TRTC room_id + userSig 签发，PC-INTEGRATION §2.3 契约）

职责：
- 校验 device_id；
- 生成/复用 room_id（TRTC_ROOM_PREFIX + device_id，同 device 幂等）；
- 用 .env 的 TRTC_SECRETKEY 按 TLSSigAPIv2 官方算法签发 userSig（≤600s）；
- 返回 {room_id, user_id, user_sig, sdk_app_id, scene}。

契约（架构师裁决 2026-08-06）：手机 userId = 请求 device_id（原 pc-phone 定值废弃）。
手机用自己的 device_id 作为 TRTC userId 进房；PC sidecar 是另一条路径自行拿
userSig（userId=jax-pc-sidecar）。

SecretKey 只从 .env（Settings.trtc_secretkey）注入；本模块禁止硬编码密钥。
"""
from __future__ import annotations

import logging
import re

from pydantic import BaseModel

from .usersig import gen_user_sig

logger = logging.getLogger(__name__)

# device_id：字母/数字/下划线/连字符，1~64 字符（TRTC userId/房间字符串同字符集）
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 进房场景（纯语音通话）
SCENE_AUDIO_CALL = "audio_call"


class RtcSessionConfig(BaseModel):
    """TRTC 会话配置（全部来自 .env，不落 yaml）"""

    sdk_app_id: int = 0
    secret_key: str = ""
    room_prefix: str = "jax-"
    user_sig_expire_s: int = 600   # 契约：≤600s


class RtcSessionError(Exception):
    """会话签发基类错误"""


class ConfigMissingError(RtcSessionError):
    """TRTC 凭据未配置（SDKAppID/SecretKey 缺失）"""


class InvalidDeviceIdError(RtcSessionError):
    """device_id 不合法"""


class RtcSessionService:
    """会话签发（无状态：room_id 由 device_id 确定性派生，天然幂等）

    幂等语义：同 device_id → 同 room_id（无需内存状态）；userSig 每次重签（短时效）。
    user_id 与 device_id 同值（架构师裁决：手机 userId = device_id）。
    """

    def __init__(self, cfg: RtcSessionConfig) -> None:
        self.cfg = cfg

    def is_configured(self) -> bool:
        return bool(self.cfg.sdk_app_id and self.cfg.secret_key)

    def issue(self, device_id: str) -> dict:
        """签发会话凭证；凭据缺失抛 ConfigMissingError，device_id 非法抛 InvalidDeviceIdError"""
        if not self.is_configured():
            raise ConfigMissingError("TRTC 凭据未配置（SDKAppID/SecretKey 缺失）")
        self._validate_device_id(device_id)

        room_id = f"{self.cfg.room_prefix}{device_id}"
        user_sig = gen_user_sig(
            sdk_app_id=self.cfg.sdk_app_id,
            secret_key=self.cfg.secret_key,
            user_id=device_id,
            expire_s=self.cfg.user_sig_expire_s,
        )
        logger.info("issue rtc session room=%s user_id=%s sdk_app_id=%s", room_id, device_id, self.cfg.sdk_app_id)
        return {
            "room_id": room_id,
            "user_id": device_id,
            "user_sig": user_sig,
            "sdk_app_id": self.cfg.sdk_app_id,
            "scene": SCENE_AUDIO_CALL,
        }

    @staticmethod
    def _validate_device_id(device_id: str) -> None:
        if not isinstance(device_id, str) or not _DEVICE_ID_RE.match(device_id):
            raise InvalidDeviceIdError("device_id 不合法：仅允许字母/数字/下划线/连字符，1~64 字符")


def build_session_service_from_settings(settings) -> RtcSessionService:
    """从 Settings（.env）装配 RtcSessionService

    settings 需含 trtc_sdkappid / trtc_secretkey / trtc_room_prefix 字段。
    """
    cfg = RtcSessionConfig(
        sdk_app_id=int(settings.trtc_sdkappid or 0),
        secret_key=settings.trtc_secretkey or "",
        room_prefix=settings.trtc_room_prefix or "jax-",
    )
    return RtcSessionService(cfg)
