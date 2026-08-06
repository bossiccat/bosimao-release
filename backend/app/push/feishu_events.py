"""飞书机器人事件订阅处理（P2 骨架，O-014 手机语音对话预留）

- URL 验证：飞书下发 {"type":"url_verification","challenge":"..."} → 回显 challenge
- 消息事件：header.event_type == im.message.receive_v1 → 解析 text/audio →
  广播 EVT_FEISHU_MSG（audio 含 file_key 占位）
- 语音完整链路（下载音频 → ASR → 大脑 → TTS → 上传回传）标 TODO，
  依赖用户创建飞书自建应用（App ID/Secret）后实施（O-014 Resolves when）

返回空 dict {} = 成功 ack（飞书 3s 内须 200，否则重试）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..core.events import EVT_FEISHU_MSG, EventBus

logger = logging.getLogger(__name__)

FEISHU_EVENT_MSG_RECEIVE = "im.message.receive_v1"
FEISHU_MSG_TYPE_TEXT = "text"
FEISHU_MSG_TYPE_AUDIO = "audio"


class FeishuCallbackError(Exception):
    """回调处理错误（非法请求/验签失败）"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class FeishuCallbackService:
    """事件订阅回调处理：challenge 验证 + 消息事件解析 → EventBus 广播"""

    def __init__(self, bus: EventBus, verification_token: str = "") -> None:
        self._bus = bus
        self._verification_token = verification_token

    async def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理回调请求，返回飞书要求的响应体"""
        if payload.get("type") == "url_verification":
            return self._handle_challenge(payload)
        await self._handle_event(payload)
        return {}

    # ---------- URL 验证 ----------
    def _handle_challenge(self, payload: dict[str, Any]) -> dict[str, Any]:
        challenge = payload.get("challenge")
        if not challenge:
            raise FeishuCallbackError("challenge 缺失")
        self._verify_token(payload.get("token"))
        return {"challenge": challenge}

    def _verify_token(self, token: Any) -> None:
        """配置了 verification_token 时校验；未配置则跳过（骨架阶段）"""
        if self._verification_token and token != self._verification_token:
            raise FeishuCallbackError("verification token 不匹配", status_code=403)

    # ---------- 消息事件 ----------
    async def _handle_event(self, payload: dict[str, Any]) -> None:
        header = payload.get("header") or {}
        self._verify_token(header.get("token"))
        if header.get("event_type") != FEISHU_EVENT_MSG_RECEIVE:
            logger.debug("忽略非消息事件: %s", header.get("event_type"))
            return
        data = self._parse_message(payload.get("event") or {})
        if data is None:
            return
        await self._bus.emit(EVT_FEISHU_MSG, data)
        logger.info("feishu msg 广播: type=%s chat=%s", data["message_type"], data["chat_id"])

    def _parse_message(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """解析 im.message.receive_v1 的 event 段；非 text/audio 返回 None"""
        message = event.get("message") or {}
        msg_type = message.get("message_type", "")
        if msg_type not in (FEISHU_MSG_TYPE_TEXT, FEISHU_MSG_TYPE_AUDIO):
            logger.debug("忽略非 text/audio 消息: %s", msg_type)
            return None
        content = message.get("content") or ""
        text, file_key = "", ""
        if msg_type == FEISHU_MSG_TYPE_AUDIO:
            # TODO(O-014): 下载音频（file_key）→ ASR → 大脑 → TTS → 上传回传
            file_key = self._extract_file_key(content)
        else:
            text = self._extract_text(content)
        return {
            "message_id": message.get("message_id", ""),
            "chat_id": message.get("chat_id", ""),
            "chat_type": message.get("chat_type", ""),
            "message_type": msg_type,
            "text": text,
            "file_key": file_key,
            "sender": event.get("sender") or {},
            "event_id": event.get("event_id", ""),
        }

    @staticmethod
    def _extract_text(content: str) -> str:
        try:
            return json.loads(content).get("text", "")
        except (json.JSONDecodeError, TypeError):
            return content

    @staticmethod
    def _extract_file_key(content: str) -> str:
        try:
            return json.loads(content).get("file_key", "")
        except (json.JSONDecodeError, TypeError):
            return ""
