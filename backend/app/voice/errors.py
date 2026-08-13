"""统一错误码与响应（SPEC §5 错误码锁定表）

变更记录（仅追加）：
- 2026-08-13 高压 H6 发现 revoke 外部终止未确认误用 50401（upstream_timeout，
  客户端会按网关超时误重试）。新增 50301 termination_unconfirmed（HTTP 503，
  服务暂时不可用、可安全重试），RevokeTerminationError 迁移至 50301；
  50401 保留为上游超时语义（未变）。
- 2026-08-13 ADR-021 新增 40301 privacy_disabled（HTTP 403，cloud 处理关闭时
  签发被拒，fail-closed）与 50302 privacy_action_failed（HTTP 503，运行时动作
  apply 失败且设置已回滚，可安全重试）。
"""
from __future__ import annotations

ERROR_MESSAGES: dict[int, str] = {
    40001: "invalid_device_or_user",
    40101: "auth_failed",
    40102: "nonce_replay",
    40103: "credential_revoked",
    40301: "privacy_disabled",
    40401: "device_not_found",
    40801: "handshake_timeout",
    40901: "state_conflict",
    41301: "queue_overflow",
    42901: "rate_limited",
    50300: "credential_unavailable",
    50301: "termination_unconfirmed",
    50302: "privacy_action_failed",
    50401: "upstream_timeout",
}

HTTP_STATUS: dict[int, int] = {
    40001: 400,
    40101: 401,
    40102: 401,
    40103: 401,
    40301: 403,
    40401: 404,
    40801: 408,
    40901: 409,
    41301: 413,
    42901: 429,
    50300: 503,
    50301: 503,
    50302: 503,
    50401: 504,
}


def error_payload(code: int, message: str | None = None) -> dict:
    """统一错误响应体：{code, data: null, message}"""
    return {"code": code, "data": None, "message": message or ERROR_MESSAGES[code]}


class VoiceError(Exception):
    """业务异常：携带锁定错误码，不携带任何敏感明文"""

    def __init__(self, code: int, message: str | None = None) -> None:
        super().__init__(ERROR_MESSAGES[code] if message is None else message)
        self.code = code
        self.message = ERROR_MESSAGES[code] if message is None else message
