"""确认后注入（backend-brain-spec §7，O-012 默认 + O-013 受控）

主通道：聚焦校验（仅标题匹配，不读窗口内容）→ 剪贴板（ctypes Win32，
pyperclip 可用时优先）→ SendInput（Ctrl+V 延迟 150ms + Enter，ctypes user32）
备用通道：写指令文件 backend/data/instructions/{task_id}.md，用户手动粘贴
审计日志：仅存 60 字预览（N-3 留痕，不含指令全文）

安全边界（O-013）：仅指令文本；注入前需 confirm_token（pipeline 校验）；
不读 Codex 内部数据、不模拟 UI 之外操控。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import BrainConfig, MonitorsConfig, PROJECT_ROOT
from .sanitizer import preview as preview_text
from .schemas import BrainTask

logger = logging.getLogger(__name__)

# Win32 常量
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


@dataclass
class InjectFocusResult:
    ok: bool
    window_title: str = ""
    reason: str = ""


@dataclass
class InjectResult:
    ok: bool
    channel: str
    error: str | None = None


class Injector:
    def __init__(
        self,
        cfg: BrainConfig,
        monitors: MonitorsConfig,
        audit_path: Path | None = None,
        instructions_dir: Path | None = None,
    ) -> None:
        self._cfg = cfg
        self._target_app = cfg.inject.target_app
        self._title_rules = {
            m.app_id: m.window_title_regex for m in monitors.monitors
        }
        self._audit_path = audit_path or (PROJECT_ROOT / cfg.inject.audit_path)
        self._instructions_dir = instructions_dir or (PROJECT_ROOT / cfg.inject.instructions_dir)

    # ---------- 聚焦校验（仅标题匹配，不读内容，O-013） ----------
    async def validate_focus(self, target_app: str | None = None) -> InjectFocusResult:
        target = target_app or self._target_app
        regex = self._title_rules.get(target)
        if not regex:
            return InjectFocusResult(ok=False, reason=f"监控配置缺少 {target} 窗口标题规则")
        title = await asyncio.to_thread(_foreground_window_title)
        if title is None:
            return InjectFocusResult(ok=False, reason="无法读取前台窗口标题")
        if _title_matches(regex, title):
            return InjectFocusResult(ok=True, window_title=preview_text(title, 30))
        return InjectFocusResult(
            ok=False,
            window_title=preview_text(title, 30),
            reason=f"前台窗口标题不匹配 {target}（请将 {target} 输入框置于前台）",
        )

    # ---------- 主通道：剪贴板 + SendInput ----------
    async def inject(self, task: BrainTask) -> InjectResult:
        if not task.instruction:
            return InjectResult(ok=False, channel="sendinput", error="任务无指令可注入")
        text = task.instruction.instruction_text
        await asyncio.to_thread(_set_clipboard_text, text)
        await asyncio.sleep(self._cfg.inject.clipboard_delay_s)  # 剪贴板竞态规避（勿删）
        await asyncio.to_thread(_send_ctrl_v_enter, self._cfg.inject.enter_delay_s)
        logger.info("注入完成: task=%s channel=sendinput chars=%d", task.task_id, len(text))
        return InjectResult(ok=True, channel="sendinput")

    # ---------- 备用通道：写指令文件 ----------
    async def write_fallback_file(self, task: BrainTask) -> Path:
        if not task.instruction:
            raise ValueError("任务无指令可写")
        self._instructions_dir.mkdir(parents=True, exist_ok=True)
        path = self._instructions_dir / f"{task.task_id}.md"
        await asyncio.to_thread(path.write_text, task.instruction.instruction_text, "utf-8")
        logger.info("备用通道写入: %s", path)
        return path

    # ---------- 审计日志（60 字预览，N-3 留痕） ----------
    def audit(self, task: BrainTask, action: str, result: str) -> None:
        preview = preview_text(task.instruction.instruction_text if task.instruction else "", self._cfg.inject.audit_preview_chars)
        line = {
            "ts": int(time.time()),
            "task_id": task.task_id,
            "target": self._target_app,
            "action": action,  # inject|deny|fallback|expire
            "result": result,  # ok|fail|denied|timeout
            "instruction_preview": preview,
        }
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
                f.flush()
        except OSError as e:
            # 写入失败仅记 warning 不阻断（spec §7.4 损坏容忍）
            logger.warning("注入审计日志写入失败: %s", e)


# ---------- Win32 底层（模块级，测试可 monkeypatch） ----------


def _foreground_window_title() -> str | None:
    """读取前台窗口标题（仅标题，绝不读内容）。非 Windows 返回 None。"""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception as e:  # noqa: BLE001 - 非 Windows/无桌面会话
        logger.warning("读取前台窗口标题失败: %s", e)
        return None


def _title_matches(regex: str, title: str) -> bool:
    import re

    try:
        return re.search(regex, title) is not None
    except re.error:
        return False


def _set_clipboard_text(text: str) -> None:
    """写入剪贴板：pyperclip 可用则用，否则 ctypes Win32 SetClipboardData"""
    try:
        import pyperclip  # type: ignore[import-not-found]

        pyperclip.copy(text)
        return
    except Exception:  # noqa: BLE001 - pyperclip 缺失/失败 → ctypes 兜底
        pass
    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        data = text.encode("utf-16-le") + b"\x00\x00"
        if not user32.OpenClipboard(0):
            raise OSError("OpenClipboard 失败")
        try:
            user32.EmptyClipboard()
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                raise OSError("GlobalAlloc 失败")
            p = kernel32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            kernel32.GlobalUnlock(h)
            user32.SetClipboardData(CF_UNICODETEXT, h)
        finally:
            user32.CloseClipboard()
    except Exception as e:  # noqa: BLE001
        logger.warning("剪贴板写入失败: %s", e)
        raise


def _send_ctrl_v_enter(enter_delay_s: float = 0.1) -> None:
    """SendInput：Ctrl+V → 延迟 → Enter（仅键盘，UI 之外不操控）"""
    try:
        import ctypes
        from ctypes import wintypes

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUT(ctypes.Structure):
            class _I(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]

            _anonymous_ = ("i",)
            _fields_ = [("type", wintypes.DWORD), ("i", _I)]

        user32 = ctypes.windll.user32

        def _key(vk: int, up: bool = False) -> None:
            extra = ctypes.c_ulong(0)
            ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, ctypes.pointer(extra))
            inp = INPUT(type=INPUT_KEYBOARD, ki=ki)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

        _key(VK_CONTROL)
        _key(VK_V)
        _key(VK_V, up=True)
        _key(VK_CONTROL, up=True)
        time.sleep(enter_delay_s)
        _key(VK_RETURN)
        _key(VK_RETURN, up=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("SendInput 失败: %s", e)
        raise
