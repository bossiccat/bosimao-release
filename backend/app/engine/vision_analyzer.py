"""视觉分析器：截屏 → 提示词 → 结构化 JSON 判定（progress/stuck/off_track/摘要）"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..core.state import AgentState
from .llama_omni_client import LlamaOmniClient

logger = logging.getLogger(__name__)

# 默认提示词模板（外置可调：config/detection.yaml -> prompt_template）
DEFAULT_PROMPT = """你是 AI 编程智能体监控助手。观察这张截图（AI 编程工具的会话窗口），
判断其工作状态，只输出 JSON：
{"state": "progress" | "stuck" | "off_track" | "unknown", "summary": "不超过20字的中文摘要"}
判定规则：
- progress: 界面显示代码生成、运行、文件修改等推进动作
- stuck: 长时间无变化、等待输入、错误提示无进展
- off_track: 输出的内容与编程任务明显无关（如闲聊、乱码）
- unknown: 看不清或无法判断
"""


@dataclass
class VisionResult:
    state: AgentState
    summary: str
    raw: str

    def to_dict(self) -> dict:
        return {"state": self.state.value, "summary": self.summary}


def parse_vision_output(text: str) -> VisionResult:
    """解析模型输出为 VisionResult（容错：剥离 <think> 推理块 / markdown 代码块 / 提取首个 JSON）"""
    cleaned = text.strip()
    # POC-001 实测：decode 输出含 <think> 推理块，先剥离（防止块内 { } 干扰 JSON 提取）
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        # 尝试截取首个 {...} 片段
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            return VisionResult(AgentState.UNKNOWN, "解析失败", text)
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return VisionResult(AgentState.UNKNOWN, "解析失败", text)

    state_str = str(obj.get("state", "unknown")).strip().lower()
    state_map = {
        "progress": AgentState.PROGRESS,
        "stuck": AgentState.STUCK,
        "off_track": AgentState.OFF_TRACK,
    }
    state = state_map.get(state_str, AgentState.UNKNOWN)
    summary = str(obj.get("summary", ""))[:40]
    return VisionResult(state=state, summary=summary, raw=text)


class VisionAnalyzer:
    def __init__(
        self,
        client: LlamaOmniClient,
        prompt_template: Path | None = None,
        max_width: int = 1280,
    ) -> None:
        self._client = client
        self._max_width = max_width
        self._prompt = (
            prompt_template.read_text(encoding="utf-8") if prompt_template and prompt_template.exists()
            else DEFAULT_PROMPT
        )

    def _prepare(self, screenshot: Path) -> Path:
        """送入模型前预处理：超宽截图降采样到 max_width（控制推理耗时）。

        降采样结果写到同目录（截图本身在 tmp/captures 下），不满足条件原样返回。
        """
        if self._max_width <= 0 or not screenshot.exists():
            return screenshot
        try:
            from PIL import Image

            with Image.open(screenshot) as img:
                if img.width <= self._max_width:
                    return screenshot
                ratio = self._max_width / img.width
                new_size = (self._max_width, max(1, int(img.height * ratio)))
                img = img.resize(new_size, Image.LANCZOS)
                dst = screenshot.with_name(
                    f"{screenshot.stem}_ds{self._max_width}{screenshot.suffix}"
                )
                img.save(dst, "PNG")
                return dst
        except Exception:  # noqa: BLE001
            logger.warning("截图降采样失败，使用原图: %s", screenshot)
            return screenshot

    async def analyze(self, screenshot: Path) -> VisionResult:
        prepared = self._prepare(screenshot)
        text = await self._client.vision_analyze(prepared, self._prompt)
        return parse_vision_output(text)
