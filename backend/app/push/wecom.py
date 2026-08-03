"""企业微信机器人 webhook Provider（主选，国内可达）"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from .base import PushResult


class WecomProvider:
    name = "wecom"

    def __init__(self, webhook_url: str, rate_limit_per_minute: int = 15) -> None:
        if not webhook_url:
            raise ValueError("WECOM_WEBHOOK_URL 未配置")
        self._webhook = webhook_url
        self._client = httpx.Client(timeout=10.0)
        # 简易限频：记录每次发送时间戳（仅成功后占配额）
        self._timestamps: list[float] = []
        self._rate_limit = rate_limit_per_minute

    def _allow(self) -> bool:
        """检查是否在限频内（不消耗配额）"""
        now = time.time()
        self._timestamps = [t for t in self._timestamps if now - t < 60]
        return len(self._timestamps) < self._rate_limit

    def _record_send(self) -> None:
        self._timestamps.append(time.time())

    def push(
        self,
        text: str,
        image: Path | None = None,
        title: str | None = None,
    ) -> PushResult:
        if not self._allow():
            return PushResult(False, self.name, "限频（每分钟上限 %d 条）" % self._rate_limit)

        # 企业微信 webhook 仅支持 markdown 文本（不支持附件图）
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"### {title or '贾克斯提醒'}\n{text}",
            },
        }
        try:
            resp = self._client.post(self._webhook, json=payload)
        except httpx.HTTPError as e:
            # 网络类错误：可重试
            return PushResult(False, self.name, f"wecom http error: {e}", retryable=True)

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            # 非 JSON 响应（如网关 HTML 页）：业务级异常，不重试，但不外抛
            return PushResult(False, self.name, f"wecom 响应非 JSON: {e}", retryable=False)

        if resp.status_code == 200 and data.get("errcode") == 0:
            self._record_send()  # 仅成功后占限频配额
            return PushResult(True, self.name)
        # 业务错误：errcode != 0（如 webhook 失效）→ 不重试
        return PushResult(False, self.name, f"wecom err: {data}", retryable=False)
