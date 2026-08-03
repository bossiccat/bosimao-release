"""ntfy Provider（备选，支持截图附件）"""
from __future__ import annotations

from pathlib import Path

import httpx

from .base import PushResult


class NtfyProvider:
    name = "ntfy"

    def __init__(self, server: str, topic: str, priority: str = "default") -> None:
        if not topic:
            raise ValueError("NTFY_TOPIC 未配置")
        self._url = f"{server.rstrip('/')}/{topic.strip('/')}"
        self._priority = priority
        self._client = httpx.Client(timeout=10.0)

    def push(
        self,
        text: str,
        image: Path | None = None,
        title: str | None = None,
    ) -> PushResult:
        headers = {"Priority": self._priority}
        if title:
            headers["Title"] = title
        try:
            if image and image.exists():
                with image.open("rb") as f:
                    resp = self._client.post(
                        self._url, content=f.read(), headers={**headers, "Filename": image.name}
                    )
            else:
                resp = self._client.post(self._url, content=text.encode("utf-8"), headers=headers)
        except httpx.HTTPError as e:
            # 网络类错误：可重试
            return PushResult(False, self.name, f"ntfy error: {e}", retryable=True)

        if resp.status_code in (200, 201):
            return PushResult(True, self.name)
        if resp.status_code >= 500:
            # 5xx 服务端瞬时错误：可重试
            return PushResult(False, self.name, f"ntfy http {resp.status_code}", retryable=True)
        # 4xx 业务错误（如 topic 非法）：不重试
        return PushResult(False, self.name, f"ntfy http {resp.status_code}", retryable=False)
