"""飞书机器人 webhook Provider（主选，国内可达；O-002 企微→飞书）

飞书自定义机器人 webhook：POST https://open.feishu.cn/open-apis/bot/v2/hook/{token}
- 纯文本：{"msg_type":"text","content":{"text":"..."}}
- 富文本（带标题）：{"msg_type":"post","content":{"post":{"zh_cn":{...}}}}
成功响应 {"code":0,"msg":"success"}；code!=0 为业务错误（webhook 失效等，不重试）。

限频：飞书官方 ~100/min，仅成功后占配额（对齐 wecom 模式）。
webhook 不支持附件图片（与企微一致）：image 参数忽略。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from .base import PushResult


class FeishuProvider:
    name = "feishu"

    def __init__(self, webhook_url: str, rate_limit_per_minute: int = 100) -> None:
        if not webhook_url:
            raise ValueError("FEISHU_WEBHOOK_URL 未配置")
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

    def _payload(self, text: str, title: str | None) -> dict:
        """消息体：带标题用富文本 post（标题置顶），否则纯文本 text"""
        if title:
            return {
                "msg_type": "post",
                "content": {
                    "post": {
                        "zh_cn": {
                            "title": title,
                            "content": [[{"tag": "text", "text": text}]],
                        }
                    }
                },
            }
        return {"msg_type": "text", "content": {"text": text}}

    def push(
        self,
        text: str,
        image: Path | None = None,
        title: str | None = None,
    ) -> PushResult:
        if not self._allow():
            return PushResult(
                False, self.name, f"限频（每分钟上限 {self._rate_limit} 条）"
            )

        # 飞书 webhook 不支持附件图片（需自建应用 API 上传，见 O-014 TODO）
        payload = self._payload(text, title)
        try:
            resp = self._client.post(self._webhook, json=payload)
        except httpx.HTTPError as e:
            # 网络类错误：可重试
            return PushResult(False, self.name, f"feishu http error: {e}", retryable=True)

        if resp.status_code >= 500:
            # 5xx 服务端瞬时错误：可重试
            return PushResult(
                False, self.name, f"feishu http {resp.status_code}", retryable=True
            )
        if resp.status_code >= 400:
            # 4xx 业务错误（webhook URL 非法等）：不重试
            return PushResult(
                False, self.name, f"feishu http {resp.status_code}", retryable=False
            )

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            # 非 JSON 响应（如网关 HTML 页）：业务级异常，不重试，但不外抛
            return PushResult(False, self.name, f"feishu 响应非 JSON: {e}", retryable=False)

        if data.get("code") == 0:
            self._record_send()  # 仅成功后占限频配额
            return PushResult(True, self.name)
        # 业务错误：code != 0（如 webhook 失效/限流拒绝）→ 不重试
        return PushResult(False, self.name, f"feishu err: {data}", retryable=False)
