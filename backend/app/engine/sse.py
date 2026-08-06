"""llama.cpp-omni SSE 流逐行解析器（decode stream=true 响应）

实测（POC-001）：decode(stream=true) 返回 text/event-stream，逐块形如
    data: {"content":"...","stop":false,"is_listen":false,"end_of_turn":false}
    data: [DONE]
按 resp.text 纯文本读必然拿到原始 SSE 文本、JSON 判定必失败。

本模块只提供纯函数解析器 + 流式生成器，不绑定 httpx 之外的类型，便于单测。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

logger = logging.getLogger(__name__)


class SseProtocolError(Exception):
    """SSE 协议错误：畸形行 / 结构不符（客户端层不可重试）"""


@dataclass
class SseEvent:
    """单条 SSE 事件（解析产物）

    kind: "delta"(文本增量) | "done"([DONE] 终止) | "error"(模型错误)
    content: delta 文本增量 / error 错误描述
    stop / round_idx / is_listen / end_of_turn: decode 帧元数据（调试/流程用）
    """

    kind: str
    content: str = ""
    stop: bool = False
    round_idx: int = 0
    is_listen: bool = False
    end_of_turn: bool = False
    raw: str = ""


def parse_sse_payload(payload: str, raw: str = "") -> SseEvent | None:
    """解析 data payload 为事件；畸形 → SseProtocolError（不静默丢 token）。

    返回 None 表示跳过：合法 JSON 但非生成帧（实测服务端会发 kv_cache_length
    等元数据帧，缺 content/error 字段——须容忍，否则解析必失败）。
    """
    if payload == "[DONE]":
        return SseEvent(kind="done", raw=raw)
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError as e:
        raise SseProtocolError(f"畸形 SSE data（非 JSON）: {payload!r}") from e
    if not isinstance(obj, dict):
        raise SseProtocolError(f"畸形 SSE data（非对象）: {payload!r}")
    if "error" in obj:
        return SseEvent(kind="error", content=str(obj["error"]), raw=raw)
    if "content" in obj:
        return SseEvent(
            kind="delta",
            content=str(obj.get("content", "")),
            stop=bool(obj.get("stop", False)),
            round_idx=int(obj.get("round_idx", 0) or 0),
            is_listen=bool(obj.get("is_listen", False)),
            end_of_turn=bool(obj.get("end_of_turn", False)),
            raw=raw,
        )
    # 元数据帧（kv_cache_length 等）：跳过
    return None


def parse_sse_line(line: str) -> SseEvent | None:
    """解析单行 SSE。

    - 去行尾 \\r\\n 并 strip
    - 空行 / 注释行(以 : 开头) / 事件名行(非 data: 前缀) → None（跳过）
    - data: 前缀 → 提取 payload 转 parse_sse_payload
    """
    stripped = line.strip()
    if not stripped or stripped.startswith(":") or not stripped.startswith("data:"):
        return None
    payload = stripped[len("data:"):].lstrip()
    return parse_sse_payload(payload, raw=line)


def _payload_is_complete(payload: str) -> bool:
    """已收集的 data payload 是否为完整事件（合法 JSON 或 [DONE]）"""
    if payload == "[DONE]":
        return True
    try:
        json.loads(payload)
    except json.JSONDecodeError:
        return False
    return True


async def iter_sse_chunks(resp: httpx.Response) -> AsyncIterator[SseEvent]:
    """逐行消费 httpx 流式响应，产出 SSE 事件。

    - data: 前缀提取 payload
    - 多行 data 拼接：当前 payload 未完整（非合法 JSON/[DONE]）时续行以 \\n 拼接
    - 空行 = 事件分隔符，flush 未决 payload
    - [DONE] → done 事件
    - 畸形 payload → SseProtocolError（不静默跳过）
    - 底层网络错误（httpx.TransportError，含流空闲超时）原样透传
    """
    payload: str | None = None
    async for raw_line in resp.aiter_lines():
        stripped = raw_line.rstrip("\r").strip()
        if not stripped:
            # 事件分隔符：flush 未决的多行 data
            if payload is not None:
                ev = parse_sse_payload(payload)
                if ev is not None:
                    yield ev
                payload = None
            continue
        if stripped.startswith(":") or not stripped.startswith("data:"):
            continue
        data = stripped[len("data:"):].lstrip()
        if payload is None:
            payload = data
        elif _payload_is_complete(payload):
            # 上一个事件已完整，本行是独立新事件
            ev = parse_sse_payload(payload)
            if ev is not None:
                yield ev
            payload = data
        else:
            payload = payload + "\n" + data
    if payload is not None:
        ev = parse_sse_payload(payload)
        if ev is not None:
            yield ev
