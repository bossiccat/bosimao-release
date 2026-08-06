"""Injector 单测（mock Win32 底层：剪贴板/SendInput/前台窗口标题）

覆盖：聚焦校验（标题匹配/不匹配/无标题/无规则）/ 注入序列（剪贴板+SendInput）/
备用文件通道 / 审计 60 字预览 / 审计写失败容忍。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.brain.injector import Injector
from app.brain.schemas import BrainTask, InstructionDraft, IntentExtract
from app.config import BrainConfig, InjectConfig, MonitorTarget, MonitorsConfig


def _cfg() -> BrainConfig:
    return BrainConfig(
        inject=InjectConfig(
            clipboard_delay_s=0.0,
            enter_delay_s=0.0,
            audit_preview_chars=60,
            target_app="codex",
        )
    )


def _monitors() -> MonitorsConfig:
    return MonitorsConfig(
        monitors=[
            MonitorTarget(
                app_id="codex",
                app_name="OpenAI Codex",
                process_name="ChatGPT.exe",
                window_title_regex="(?i)chatgpt",
            )
        ]
    )


def _task(instruction: str = "请重构数据层，拆分接口与实现，失败回滚。") -> BrainTask:
    now = time.time()
    return BrainTask(
        task_id="BT-TEST-001",
        status="awaiting_confirm",
        intent=IntentExtract(),
        instruction=InstructionDraft(
            task_id="BT-TEST-001", instruction_text=instruction, preview=""
        ),
        created_at=now,
        updated_at=now,
        confirm_token="tok-123",
    )


class TestValidateFocus:
    @pytest.mark.asyncio
    async def test_title_matches(self, tmp_path, monkeypatch):
        inj = Injector(_cfg(), _monitors(), audit_path=tmp_path / "a.jsonl")
        monkeypatch.setattr("app.brain.injector._foreground_window_title", lambda: "ChatGPT - Codex")
        res = await inj.validate_focus("codex")
        assert res.ok is True
        assert "ChatGPT" in res.window_title

    @pytest.mark.asyncio
    async def test_title_mismatch(self, tmp_path, monkeypatch):
        inj = Injector(_cfg(), _monitors(), audit_path=tmp_path / "a.jsonl")
        monkeypatch.setattr("app.brain.injector._foreground_window_title", lambda: "计算器 Calculator")
        res = await inj.validate_focus("codex")
        assert res.ok is False
        assert "不匹配" in res.reason

    @pytest.mark.asyncio
    async def test_no_foreground_title(self, tmp_path, monkeypatch):
        inj = Injector(_cfg(), _monitors(), audit_path=tmp_path / "a.jsonl")
        monkeypatch.setattr("app.brain.injector._foreground_window_title", lambda: None)
        res = await inj.validate_focus("codex")
        assert res.ok is False

    @pytest.mark.asyncio
    async def test_no_title_rule(self, tmp_path, monkeypatch):
        inj = Injector(_cfg(), _monitors(), audit_path=tmp_path / "a.jsonl")
        res = await inj.validate_focus("trae")  # 未配置 trae 标题规则
        assert res.ok is False
        assert "监控配置缺少" in res.reason


class TestInject:
    @pytest.mark.asyncio
    async def test_inject_sets_clipboard_then_sends_keys(self, tmp_path, monkeypatch):
        inj = Injector(_cfg(), _monitors(), audit_path=tmp_path / "a.jsonl")
        captured: dict = {}

        def fake_set(text: str) -> None:
            captured["clipboard"] = text

        def fake_send(enter_delay_s: float) -> None:
            captured["enter_delay"] = enter_delay_s

        monkeypatch.setattr("app.brain.injector._set_clipboard_text", fake_set)
        monkeypatch.setattr("app.brain.injector._send_ctrl_v_enter", fake_send)

        task = _task("这是要注入的指令文本，仅此而已。")
        result = await inj.inject(task)
        assert result.ok is True
        assert result.channel == "sendinput"
        assert captured["clipboard"] == task.instruction.instruction_text
        assert captured["enter_delay"] == 0.0

    @pytest.mark.asyncio
    async def test_inject_without_instruction_fails(self, tmp_path, monkeypatch):
        inj = Injector(_cfg(), _monitors(), audit_path=tmp_path / "a.jsonl")
        task = _task()
        task.instruction = None
        result = await inj.inject(task)
        assert result.ok is False
        assert result.channel == "sendinput"
        assert "无指令" in (result.error or "")


class TestFallbackFile:
    @pytest.mark.asyncio
    async def test_write_fallback_file_only_instruction(self, tmp_path):
        inj = Injector(_cfg(), _monitors(), instructions_dir=tmp_path / "instructions")
        task = _task("指令文本内容 12345")
        path = await inj.write_fallback_file(task)
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert content == "指令文本内容 12345"  # 仅指令文本


class TestAudit:
    def test_audit_stores_60_char_preview(self, tmp_path):
        inj = Injector(_cfg(), _monitors(), audit_path=tmp_path / "audit.jsonl")
        long_instruction = "A" * 200
        inj.audit(_task(long_instruction), "inject", "ok")
        lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
        entry = json.loads(lines[0])
        assert entry["task_id"] == "BT-TEST-001"
        assert entry["action"] == "inject"
        assert len(entry["instruction_preview"]) <= 60
        assert long_instruction not in entry["instruction_preview"]  # 不含指令全文

    def test_audit_appends_multiple(self, tmp_path):
        inj = Injector(_cfg(), _monitors(), audit_path=tmp_path / "audit.jsonl")
        inj.audit(_task(), "deny", "denied")
        inj.audit(_task(), "fallback", "ok")
        lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

    def test_audit_write_failure_tolerated(self, tmp_path):
        # audit_path 指向目录 → 打开失败（OSError）→ 仅 warning 不抛
        inj = Injector(_cfg(), _monitors(), audit_path=tmp_path)
        inj.audit(_task(), "inject", "ok")  # 不抛异常
