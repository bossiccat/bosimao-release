"""路径 B 半双工链路（mobile-voice-spec §8.3，M2 落地）

音频 PCM → sherpa-onnx STT → 触发词分类 → brain intent（本地调用）/ 本地回答
→ 回复文本 → edge-tts → 音频字节。

依赖注入：stt / tts / brain_handler 均可 mock（单测友好）；brain 不可用时降级为
本地占位回答并在结果里标注 route=local（对接 brain intent API 逻辑见 BrainVoiceHandler）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from ..brain.intent_service import IntentService
from .audio import is_valid_pcm16
from .stt_sherpa import SttModelUnavailable, SttSherpa
from .tts_edge import TtsEdge, TtsResult

logger = logging.getLogger(__name__)

BrainHandler = Callable[[str], Awaitable[str]]


@dataclass
class HalfDuplexResult:
    text: str = ""
    reply_text: str = ""
    route: str = "local"                 # brain|local
    tts_format: str = "mp3_24k"
    audio_bytes: bytes = b""
    ok: bool = True
    error_code: str = ""                 # 空=成功；stt_unavailable / tts_unavailable / empty
    model_status: str = "ok"
    meta: dict = field(default_factory=dict)


class SttLike(Protocol):
    def transcribe(self, pcm: bytes) -> object: ...
    def model_status(self) -> str: ...


class BrainVoiceHandler:
    """唤醒词 → 大脑 hook（需求 ④）：复用 brain.intent_service（本地 9B 提取意图）

    语音仅作表达层：STT 文本 → IntentService.extract → 拆解结果文本（供 TTS 播报）。
    本地 9B 不可用时透传原文并标记 low_confidence（与 R2 降级一致）。
    """

    def __init__(self, intent_svc: IntentService | None) -> None:
        self._intent_svc = intent_svc

    async def handle(self, text: str) -> str:
        if self._intent_svc is None:
            return f"已收到任务：{text}。大脑拆解服务未就绪，请稍后再试。"
        try:
            intent = await self._intent_svc.extract(text)
        except Exception as e:  # noqa: BLE001 - 大脑异常不阻断语音回答
            logger.warning("brain intent extract failed: %s", e)
            return f"已收到任务：{text}。大脑暂时不可用，已记录。"
        if intent.clarifying_questions:
            return "我需要确认一下：" + "；".join(intent.clarifying_questions[:2])
        return (
            f"收到，{intent.intent_type} 任务已受理（置信度 {intent.confidence:.0%}）。"
            "拆解结果稍后生成，请留意桌宠确认卡。"
        )


class HalfDuplex:
    """半双工链路编排（音频 → 文本 → 回答 → TTS）"""

    def __init__(
        self,
        stt: SttLike | None = None,
        tts: TtsEdge | None = None,
        brain_handler: BrainHandler | None = None,
        trigger_words: list[str] | None = None,
    ) -> None:
        self._stt = stt or SttSherpa("")   # 模型目录由 config 覆盖
        self._tts = tts or TtsEdge()
        self._brain = brain_handler or BrainVoiceHandler(None).handle
        self._trigger = set(trigger_words or ["帮我", "拆解", "重构", "实现", "修", "写", "优化", "测试"])

    def is_brain_intent(self, text: str) -> bool:
        """轻量触发词分类（voice.yaml → path=auto）"""
        return any(word in text for word in self._trigger)

    async def process(self, pcm: bytes) -> HalfDuplexResult:
        if not is_valid_pcm16(pcm):
            return HalfDuplexResult(ok=False, error_code="empty", model_status=self._stt.model_status())
        # 1) STT
        try:
            stt_res = self._stt.transcribe(pcm)
        except SttModelUnavailable as e:
            return HalfDuplexResult(ok=False, error_code="stt_unavailable",
                                    model_status="missing", meta={"message": str(e)})
        text = getattr(stt_res, "text", "")
        if not text:
            return HalfDuplexResult(ok=False, error_code="empty", text="", model_status=stt_res.model_status)
        # 2) 回答（brain / local）
        if self.is_brain_intent(text):
            try:
                reply = await self._brain(text)
                route = "brain"
            except Exception as e:  # noqa: BLE001 - 大脑失败降级本地回答
                logger.warning("brain handler failed, fallback local: %s", e)
                reply = f"收到：{text}"
                route = "local"
        else:
            reply = f"好的，你说的是：{text}"
            route = "local"
        # 3) TTS
        try:
            tts_res: TtsResult = await self._tts.synthesize(reply)
        except Exception as e:  # noqa: BLE001 - TTS 失败降级：仍回文本，标注 tts_unavailable
            logger.warning("tts failed: %s", e)
            return HalfDuplexResult(text=text, reply_text=reply, route=route,
                                    ok=False, error_code="tts_unavailable",
                                    model_status=stt_res.model_status)
        return HalfDuplexResult(
            text=text, reply_text=reply, route=route,
            tts_format=tts_res.format, audio_bytes=tts_res.data,
            ok=True, model_status=stt_res.model_status,
            meta={"tts_cached": tts_res.cached},
        )
