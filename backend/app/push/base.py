"""推送抽象接口：PushService（Provider 插件契约）"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class PushResult:
    ok: bool
    provider: str
    error: str | None = None
    retryable: bool = False  # True=网络类错误（可重试）；False=业务错误（重试无意义）


class PushService(Protocol):
    """所有推送 Provider 必须实现的协议"""

    name: str

    def push(
        self,
        text: str,
        image: Path | None = None,
        title: str | None = None,
    ) -> PushResult: ...
