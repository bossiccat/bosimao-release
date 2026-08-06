"""意图理解 + 会话摘要（本地 9B，R2/R3）

- extract：本地 9B 提取意图（低置信度追问 ≤2 轮由 pipeline 控制）；
  本地不可用 → 直接透传用户原文为意图，标记 low_confidence（R2 降级）
- build_summary：本地 9B 生成脱敏摘要（R3）；本地不可用 → 抛 50301（不降级，隐私第一）

对 LlamaOmniClient 仅依赖 `chat(prompt, max_tokens)`（mock 友好）。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

from ..config import BrainConfig
from .errors import service_unready
from .prompts import INTENT_EXTRACT_FALLBACK, load_prompt
from .sanitizer import sanitize, truncate_head_tail
from .schemas import IntentExtract

logger = logging.getLogger(__name__)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class LocalChatClient(Protocol):
    async def chat(self, prompt: str, max_tokens: int = 512) -> str: ...


class IntentService:
    def __init__(self, client: LocalChatClient | None, cfg: BrainConfig) -> None:
        self._client = client
        self._cfg = cfg

    # ---------- R2：意图提取 ----------
    async def extract(self, text: str, target_app: str | None = None) -> IntentExtract:
        if self._client is None:
            return self._passthrough(text, target_app)
        prompt = self._build_extract_prompt(text, target_app)
        try:
            raw = await self._client.chat(prompt, max_tokens=self._cfg.intent.local_max_tokens)
        except Exception as e:  # noqa: BLE001 - 本地不可用按 R2 降级
            logger.warning("本地 9B 意图提取失败，透传原文: %s", e)
            return self._passthrough(text, target_app)
        return self._parse_extract(raw, target_app, text)

    def _build_extract_prompt(self, text: str, target_app: str | None) -> str:
        base = load_prompt(self._cfg.intent.extract_prompt, INTENT_EXTRACT_FALLBACK)
        hint = f"\n用户目标应用提示（可覆盖判定）：{target_app}" if target_app else ""
        return f"{base}\n\n用户文本：{text}{hint}"

    def _parse_extract(self, raw: str, target_app: str | None, original: str) -> IntentExtract:
        obj = self._extract_json(raw)
        if obj is None:
            return self._passthrough(original, target_app)
        intent_type = obj.get("intent_type") or "other"
        if intent_type not in (
            "refactor", "implement", "fix_bug", "add_feature", "optimize", "test", "explain", "other"
        ):
            intent_type = "other"
        try:
            confidence = float(obj.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        questions = [str(q) for q in (obj.get("clarifying_questions") or [])][:2]
        return IntentExtract(
            intent_type=intent_type,
            target_app=str(obj.get("target_app") or target_app or "codex"),
            confidence=max(0.0, min(1.0, confidence)),
            clarifying_questions=questions,
            sanitized_summary=original,
        )

    def _passthrough(self, text: str, target_app: str | None) -> IntentExtract:
        return IntentExtract(
            intent_type="other",
            target_app=target_app or "codex",
            confidence=0.3,  # low_confidence 标记（R2 降级）
            clarifying_questions=[],
            sanitized_summary=text,
        )

    # ---------- R3：脱敏摘要（本地生成，隐私第一） ----------
    async def build_summary(self, text: str) -> str:
        if self._client is None:
            raise service_unready("本地 9B 不可用，无法生成脱敏摘要（R3 不降级）")
        prompt = (
            "你是会话摘要器。把以下用户意图压缩为 ≤200 字摘要，"
            "剔除文件路径、代码、密钥、联系方式等敏感信息，只输出摘要正文：\n\n" + text
        )
        try:
            raw = await self._client.chat(prompt, max_tokens=self._cfg.intent.local_max_tokens)
        except Exception as e:  # noqa: BLE001
            logger.warning("本地 9B 摘要失败: %s", e)
            raise service_unready(f"本地 9B 摘要失败：{e}") from e
        # 双保险：本地生成后、上传 DeepSeek 前各一次 sanitize
        clean = sanitize(raw)
        return truncate_head_tail(clean, self._cfg.intent.summary_max_chars)

    # ---------- 工具 ----------
    @staticmethod
    def _extract_json(raw: str) -> dict[str, Any] | None:
        m = _JSON_OBJECT_RE.search(raw or "")
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None
