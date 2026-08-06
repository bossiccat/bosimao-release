"""trtc-sign 云函数签名服务（PC-INTEGRATION §2.3 / ARCHITECTURE §3.4 契约）

端点语义（HTTP 触发器路由）：
- POST /api/v1/voice/session            手机 KWS 唤醒后调用：签发手机凭证
                                        （room_id=prefix+device_id, user_id=device_id, user_sig, sdk_app_id, scene）
- GET  /api/v1/voice/session/pending     PC 轮询会话意图（发现手机发起的会话）
- POST /api/v1/voice/session/sign        PC 取自身 userSig（user_id=jax-pc-sidecar，同一 room_id）

幂等：room_id 由 device_id 确定性派生（prefix+device_id），无状态天然幂等；
userSig 每次重签（短时效 ≤600s）。同 device 会话期内重复请求复用同一房间。

注意：会话意图为进程内内存存储（MVP 单用户/单实例足够）。
多实例部署需改用 Redis/TencentDB 共享存储，见 README 限制说明。
"""
from __future__ import annotations

import logging
import re
import time

# SCF 函数根为扁平目录（非 package）：绝对导入
from usersig import gen_user_sig

logger = logging.getLogger(__name__)

SCENE_AUDIO_CALL = "audio_call"
SIDECAR_USER_ID = "jax-pc-sidecar"     # PC sidecar 固定 userId（与手机 userId=device_id 区分）

# device_id：字母/数字/下划线/连字符，1~64 字符（TRTC userId/房间字符串同字符集）
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# user_id：同上（jax-pc-sidecar 合法）
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 会话意图保鲜期：PC 轮询窗口（手机先入房等待，PC 通常 ≤2s 加入）
INTENT_FRESH_S = 120


class SignError(Exception):
    """签名错误基类"""


class ConfigMissingError(SignError):
    """TRTC 凭据未配置"""


class InvalidDeviceIdError(SignError):
    """device_id 不合法"""


class InvalidUserIdError(SignError):
    """user_id 不合法"""


class UnknownDeviceError(SignError):
    """pending/sign 请求的 device_id 无会话意图（手机未先调 session）"""


class TrtcSignService:
    """云函数签名服务（无状态幂等；会话意图仅内存存储）"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._intents: dict[str, dict] = {}   # device_id -> {"room_id": str, "ts": float}

    # ---------- 会话意图（内存存储，单实例） ----------
    def _record_intent(self, device_id: str, room_id: str) -> None:
        self._intents[device_id] = {"room_id": room_id, "ts": time.time()}

    def pending(self, device_id: str) -> dict | None:
        """PC 轮询：返回未消费的会话意图（保鲜期内）；无/过期返回 None"""
        intent = self._intents.get(device_id)
        if not intent:
            return None
        if time.time() - intent["ts"] > INTENT_FRESH_S:
            self._intents.pop(device_id, None)
            return None
        return {"device_id": device_id, "room_id": intent["room_id"], "ts": intent["ts"]}

    def _consume(self, device_id: str) -> dict:
        """取会话意图并消费（PC sign 成功后调用，防重复拉 sidecar）"""
        intent = self._intents.get(device_id)
        if not intent:
            raise UnknownDeviceError(f"device_id={device_id} 无有效会话意图，请先调用 session 接口")
        if time.time() - intent["ts"] > INTENT_FRESH_S:
            self._intents.pop(device_id, None)
            raise UnknownDeviceError(f"device_id={device_id} 会话意图已过期，请重新发起会话")
        self._intents.pop(device_id, None)
        return intent

    # ---------- 签发 ----------
    def _ensure_configured(self) -> None:
        if not self.cfg.is_configured():
            raise ConfigMissingError("TRTC 凭据未配置（TRTC_SDKAPPID / TRTC_SECRETKEY 缺失）")

    def _check_whitelist(self, device_id: str) -> None:
        if self.cfg.device_whitelist and device_id not in self.cfg.device_whitelist:
            raise UnknownDeviceError(f"device_id={device_id} 不在白名单")

    @staticmethod
    def _validate_device_id(device_id: str) -> None:
        if not isinstance(device_id, str) or not _DEVICE_ID_RE.match(device_id):
            raise InvalidDeviceIdError("device_id 不合法：仅允许字母/数字/下划线/连字符，1~64 字符")

    @staticmethod
    def _validate_user_id(user_id: str) -> None:
        if not isinstance(user_id, str) or not _USER_ID_RE.match(user_id):
            raise InvalidUserIdError("user_id 不合法：仅允许字母/数字/下划线/连字符，1~64 字符")

    def _sign(self, device_id: str, user_id: str) -> dict:
        """签名：room_id = prefix + device_id，userSig 签给 user_id，expire ≤600s"""
        room_id = f"{self.cfg.room_prefix}{device_id}"
        user_sig = gen_user_sig(
            sdk_app_id=self.cfg.sdk_app_id,
            secret_key=self.cfg.secret_key,
            user_id=user_id,
            expire_s=self.cfg.expire_s,
        )
        return {
            "room_id": room_id,
            "user_id": user_id,
            "user_sig": user_sig,
            "sdk_app_id": self.cfg.sdk_app_id,
            "scene": SCENE_AUDIO_CALL,
        }

    def issue(self, device_id: str) -> dict:
        """手机会话签发：校验 → 幂等复用房间 → 记录会话意图 → 签手机 userSig（userId=device_id）"""
        self._ensure_configured()
        self._validate_device_id(device_id)
        self._check_whitelist(device_id)
        room_id = f"{self.cfg.room_prefix}{device_id}"
        self._record_intent(device_id, room_id)
        data = self._sign(device_id, user_id=device_id)
        logger.info("issue session device=%s room=%s", device_id, room_id)
        return data

    def sign_for_sidecar(self, device_id: str, user_id: str = SIDECAR_USER_ID) -> dict:
        """PC sidecar 进房签发：消费会话意图 → 签 PC userSig（userId=jax-pc-sidecar，同一房间）"""
        self._ensure_configured()
        self._validate_device_id(device_id)
        self._validate_user_id(user_id)
        intent = self._consume(device_id)   # 无/过期 → UnknownDeviceError
        logger.info("sign sidecar device=%s user=%s room=%s", device_id, user_id, intent["room_id"])
        return self._sign(device_id, user_id=user_id)
