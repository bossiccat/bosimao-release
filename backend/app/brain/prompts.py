"""Prompt 加载（config/prompts/*.md；文件缺失时回退内置默认，保证测试/冷启动健壮）"""
from __future__ import annotations

from pathlib import Path

from ..config import PROJECT_ROOT


def load_prompt(rel_path: str, fallback: str) -> str:
    path = PROJECT_ROOT / rel_path
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return fallback


INTENT_EXTRACT_FALLBACK = (
    "你是意图理解器。从用户文本提取意图，只输出 JSON："
    '{"intent_type":"refactor|implement|fix_bug|add_feature|optimize|test|explain|other",'
    '"target_app":"codex|trae|hermes|workbuddy|other","confidence":0-1,'
    '"clarifying_questions":[]}'
)

DECOMPOSE_FALLBACK = (
    "你是软件工程任务拆解器。把用户意图拆成 3-8 步可执行子任务，"
    '输出 JSON 匹配 {"subtasks":[{"id":"T1","goal":"...","acceptance":["..."],'
    '"rollback_hint":"...","depends_on":[]}]}。只基于提供的摘要，不得臆造代码或路径。'
)

INSTRUCT_FALLBACK = (
    "你是 Codex 指令优化器。把子任务清单组装成一段对 Codex 最有效的指令文本："
    "明确约束、目标、验收标准、失败回滚。用中文，直接可粘贴。"
)
