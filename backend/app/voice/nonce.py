"""nonce 防重放服务：主体绑定 + 哈希存储 + 原子消费（SPEC §9.1 / AC-03）

不同主体使用相同 nonce 字符串互不串扰；nonce 明文不落库。
"""
from __future__ import annotations

from .auth import CredentialPrincipal
from .storage import VoiceStore


# 契约上限（高压 H10 发现：无上限时 200 字符 nonce 被接受入库）。
# 客户端使用 32 hex（uuid4）或更长随机串；128 覆盖全部合法形态并拒绝畸形放大。
MAX_NONCE_LENGTH = 128


class NonceService:
    def __init__(self, store: VoiceStore, ttl_seconds: int = 300) -> None:
        self._store = store
        self.ttl_seconds = ttl_seconds

    def consume(self, principal: CredentialPrincipal, nonce: str, now: float | None = None) -> bool:
        """原子消费：成功 True；重放/缺失/过期/超长 False"""
        if not nonce or len(nonce) < 16 or len(nonce) > MAX_NONCE_LENGTH:
            return False
        return self._store.consume_nonce(
            subject_id=principal.subject_id,
            nonce=nonce,
            ttl_seconds=self.ttl_seconds,
            now=now,
        )
