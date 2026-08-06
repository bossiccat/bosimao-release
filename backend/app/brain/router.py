"""混合大脑路由决策表（backend-brain-spec §4.1 单一真源）

R1 视觉判定 → 本地 9B（截图不出本机，O-013）
R2 意图理解 → 本地 9B
R3 会话摘要 → 本地 9B（隐私第一：只上传摘要）
R4 任务拆解 → DeepSeek（熔断/失败 → 本地简化拆解 + 明示降级）
R5 指令生成 → DeepSeek（熔断/失败 → 本地通用指令 + 明示降级）
R6 语义评审 → DeepSeek（熔断 → 跳过本轮，不阻断视觉）
R7 推送提醒 → 本地规则（PushManager）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# 调用类型常量
R1_VISION = "vision"
R2_INTENT = "intent"
R3_SUMMARY = "summary"
R4_DECOMPOSE = "decompose"
R5_INSTRUCT = "instruct"
R6_REVIEW = "review"
R7_PUSH = "push"

# 引擎
LOCAL = "local"
DEEPSEEK = "deepseek"
SKIP = "skip"  # 仅 R6 熔断时使用：跳过本轮评审

# 决策表（单一真源）
_DECISION_TABLE: dict[str, str] = {
    R1_VISION: LOCAL,
    R2_INTENT: LOCAL,
    R3_SUMMARY: LOCAL,
    R4_DECOMPOSE: DEEPSEEK,
    R5_INSTRUCT: DEEPSEEK,
    R6_REVIEW: DEEPSEEK,
    R7_PUSH: LOCAL,
}


@dataclass
class RoutingDecision:
    call_type: str
    engine: str  # local | deepseek | skip
    fallback: bool = False  # True = 已降级（本地替代 or 跳过）
    reason: str = ""


class DeepSeekProbe(Protocol):
    """路由只需探测 DeepSeek 可用性（避免强耦合具体类）"""

    def circuit_open(self) -> bool: ...

    def key_configured(self) -> bool: ...


def route(call_type: str, deepseek: DeepSeekProbe | None = None) -> RoutingDecision:
    """返回路由决策；DeepSeek 不可用（熔断/未配 key）时按 R4/R5 降级本地、R6 跳过。"""
    engine = _DECISION_TABLE.get(call_type, LOCAL)
    if engine != DEEPSEEK:
        return RoutingDecision(call_type=call_type, engine=engine)

    unavailable = deepseek is not None and (
        deepseek.circuit_open() or not deepseek.key_configured()
    )
    if not unavailable:
        return RoutingDecision(call_type=call_type, engine=DEEPSEEK)

    if call_type == R6_REVIEW:
        return RoutingDecision(
            call_type=call_type,
            engine=SKIP,
            fallback=True,
            reason="DeepSeek 不可用，跳过本轮语义评审，沿用视觉判定",
        )
    return RoutingDecision(
        call_type=call_type,
        engine=LOCAL,
        fallback=True,
        reason="DeepSeek 不可用，本地简化降级",
    )


def degrade_event(decision: RoutingDecision) -> dict[str, Any]:
    """熔断/降级时的说明（供 emit EVT_BRAIN_DEGRADED / 40201 携带）"""
    return {
        "call_type": decision.call_type,
        "engine": decision.engine,
        "fallback": decision.fallback,
        "reason": decision.reason,
    }
