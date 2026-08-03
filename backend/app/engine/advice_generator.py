"""优化建议生成器：基于最近 3 条判定历史输出任务优化建议（SPEC §4.1）

规则（无空泛文案 P0-3）：
- stuck 连续（最近 3 条中 >=2 条 STUCK 且末条 STUCK）→ 建议检查输入/死循环
- off_track（最近 3 条含 OFF_TRACK）→ 建议回看任务目标
- 交替 progress/stuck（P/S/P 或 S/P/S）→ 建议简化步骤
- 其他模式 → 无建议（返回空串，不输出空泛文案）

模板外置：config/prompts/advice_template.md（{app_name} {pattern} {advice} 占位）
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..config import PROJECT_ROOT
from ..core.state import AgentState
from .vision_analyzer import VisionResult

_TEMPLATE_PATH = PROJECT_ROOT / "config" / "prompts" / "advice_template.md"
DEFAULT_TEMPLATE = "{app_name} 出现「{pattern}」：{advice}"


def _load_template() -> str:
    try:
        if _TEMPLATE_PATH.exists():
            text = _TEMPLATE_PATH.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError:
        pass
    return DEFAULT_TEMPLATE


_TEMPLATE = _load_template()


def _detect_pattern(history: Sequence[VisionResult]) -> tuple[str, str] | None:
    """返回 (pattern, advice)；无可建议模式返回 None"""
    if not history:
        return None
    recent = [r.state for r in history[-3:]]

    # 1) 交替 progress/stuck：P/S/P 或 S/P/S（更具体，优先）
    seq = [s for s in recent if s in (AgentState.PROGRESS, AgentState.STUCK)]
    if len(seq) >= 3 and len(set(seq)) == 2 and seq[0] == seq[2] != seq[1]:
        return (
            "oscillating",
            "进展与卡住反复交替，建议把任务拆成更小的步骤逐步推进",
        )

    # 2) stuck 连续
    if recent[-1] == AgentState.STUCK and recent.count(AgentState.STUCK) >= 2:
        return (
            "stuck",
            "连续多帧无进展，建议检查是否在等待输入或陷入死循环",
        )

    # 3) off_track
    if AgentState.OFF_TRACK in recent:
        return (
            "off_track",
            "可能偏离任务目标，建议回看任务目标并校正方向",
        )

    return None


def generate(history: Sequence[VisionResult], app_name: str) -> str:
    """根据最近 3 条判定历史生成建议；无建议返回空串（禁止空泛文案）"""
    pattern = _detect_pattern(history)
    if pattern is None:
        return ""
    key, advice = pattern
    return _TEMPLATE.format(app_name=app_name, pattern=key, advice=advice)
