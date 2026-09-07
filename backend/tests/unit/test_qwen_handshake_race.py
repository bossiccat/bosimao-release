"""Qwen 握手竞态（RED 先行）

真机实锤（2026-09-07 14:21-14:23）：「停止监听 → 再点立即对话」后 92s 内音频全部
送达（up rms 高、drops=0）但云端零事件（无 speech_started/response/error），随后
自发恢复。dead-rebuild 假设已排除（server.py 每个 hello 都新建 PeerVoiceSession）。

根因候选：connect_qwen 收到 session.created 即返回。session.created 是服务器
**应用 session.update 之前**的默认配置宣告；session.updated 才代表 smart_turn
VAD 已生效。旧实现从未确认配置生效就开始灌音频 → 若 session.update 被丢弃/拒绝，
会话跑在默认 VAD 上，正是「音频照发、VAD 永不触发」。

修复契约：
1. 只认 session.updated（session.created 与其他前置事件只消费、不作为就绪依据）
2. session.updated 里回传的 session id 才可用
3. 超时 fail-open（保持旧行为不回归），但必须打 ERROR 露出问题，不再静默
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

import app.voice.qwen_realtime_bridge as qb


class ScriptedWs:
    """send(session.update) 后按脚本依次投递 recv 消息，用于复现握手竞态"""

    def __init__(self, script_after_update: list[str]) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self._script = script_after_update

    async def send(self, raw) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["type"] == "session.update":
            for item in self._script:
                self.incoming.put_nowait(item)

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_connect_qwen_waits_for_session_updated(monkeypatch):
    """session.created 不算就绪：必须继续等 session.updated（配置生效确认）"""
    ws = ScriptedWs([
        # 服务器先发默认配置宣告（此时 smart_turn 尚未生效）
        json.dumps({"type": "session.created", "session": {"id": "default-sid"}}),
        # 然后才是我们的 session.update 被应用的确认
        json.dumps({"type": "session.updated",
                    "session": {"id": "real-sid",
                                "turn_detection": {"type": "smart_turn"}}}),
    ])

    async def fake_connect_ws(*a, **k):
        return ws

    monkeypatch.setattr(qb, "connect_ws", fake_connect_ws)

    got_ws, sid = await qb.connect_qwen("url", "token", "prompt", [])

    assert got_ws is ws
    assert sid == "real-sid", (
        f"必须取 session.updated 里的 id，不得用 session.created 的默认 id，实测 {sid!r}"
    )
    assert ws.incoming.empty(), (
        "返回前必须已消费 session.updated；若队列里还剩 session.updated，"
        "说明在 session.created 就提前返回了（竞态未修）"
    )


@pytest.mark.asyncio
async def test_connect_qwen_times_out_without_confirmation(monkeypatch, caplog):
    """session.updated 一直不来：超时 fail-open 返回，但必须 ERROR 露出，不得静默"""
    ws = ScriptedWs([
        json.dumps({"type": "session.created", "session": {"id": "default-sid"}}),
        # 故意不投递 session.updated
    ])

    async def fake_connect_ws(*a, **k):
        return ws

    monkeypatch.setattr(qb, "connect_ws", fake_connect_ws)

    with caplog.at_level(logging.ERROR, logger="app.voice.qwen_realtime_bridge"):
        got_ws, sid = await qb.connect_qwen(
            "url", "token", "prompt", [], config_confirm_timeout_s=0.1
        )

    assert got_ws is ws, "超时必须 fail-open 返回连接（不挂死、不回归旧行为）"
    assert any("NOT confirmed" in r.getMessage() for r in caplog.records), (
        "超时必须打 ERROR 日志把「配置未确认」暴露出来，静默继续正是本次真机 92s 空洞难以归因的原因"
    )
