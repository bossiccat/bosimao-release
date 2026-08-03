"""提醒服务：四级渐进打扰调度（状态点→微动→浮起脉冲→语音+推送）"""
from __future__ import annotations

import logging

from ..config import ReminderConfig
from ..core.events import EVT_ALERT, EventBus
from ..push.manager import PushManager

logger = logging.getLogger(__name__)


class ReminderService:
    def __init__(
        self,
        cfg: ReminderConfig,
        bus: EventBus,
        push: PushManager | None = None,
    ) -> None:
        self._cfg = cfg
        self._bus = bus
        self._push = push

    async def on_detection(self, data: dict) -> None:
        """订阅检测事件：按 alert_level 递进打扰"""
        level = int(data.get("alert_level", 0))
        app_id = data.get("app_id", "?")
        state = data.get("state", "unknown")
        summary = data.get("summary", "")
        suggestion = data.get("suggestion", "")

        if level == 0:
            return  # 正常，不动声色

        # 事件广播给 UI（桌宠动效由前端按 level 渲染）
        await self._bus.emit(
            EVT_ALERT,
            {
                "app_id": app_id,
                "level": level,
                "state": state,
                "summary": summary,
                "suggestion": suggestion,
            },
        )

        # 4 级：语音播报 + 手机推送（仅卡住/跑偏，非恢复通知）
        if level >= 4 and self._cfg.level_4_voice_push:
            if self._cfg.push_alert_enabled and self._push is not None:
                text = f"[{summary}] {suggestion or ''}"
                result = self._push.push(text=text, title=f"贾克斯 · {app_id}")
                logger.info("4级提醒推送: %s ok=%s", app_id, result.ok)

        logger.info("alert level=%d app=%s state=%s", level, app_id, state)
