"""P0 范围契约：唤醒词与自动半双工不得进入 P0 成功路径（SPEC v1.1 O-003/O-015）。

断言三件事（全部机械可判定，来源 = 唯一契约 + 源码事实）：
1. SPEC 明确把「唤醒词」锁定为 P1 Beta、把「自动半双工 fallback」排除出 Phase 1.5 DoD，
   P0 功能表不含唤醒/半双工自动降级；P0 三入口 = Android 主页面/悬浮球/通知按钮。
2. backend voice 网关的 HalfDuplex 装配只出现在 cfg.path 配置分支（全双工为主路径），
   不存在「主链路失败 → 自动降级 half_duplex」的 except 路径。
3. Android P0 三入口（MainActivity/FloatingOverlay/前台通知 ACTION_TALK）不直接触发
   WakeWordEngine，代码树不含 half_duplex 引用。

反作弊：断言来自 SPEC 原文与源码结构，不允许以「现状即事实」改写为恒真断言。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "docs" / "commercial-upgrade-SPEC.md"
ROUTES_VOICE = ROOT / "backend" / "app" / "api" / "routes_voice.py"
MOBILE_ROOT = ROOT / "mobile-app" / "app" / "src" / "main" / "java" / "com" / "jax" / "voice"

WAKE_SNIPPET = "唤醒词为 P1 Beta"
P0_ENTRY_SNIPPET = "P0 只验收 Android 主页面手动入口、悬浮球和通知按钮"
HALF_DUPLEX_DOD_SNIPPET = "自动半双工 fallback 不进入 Phase 1.5 DoD"

FALLBACK_PATTERNS = re.compile(r"half_duplex|HalfDuplex|half-duplex", re.IGNORECASE)


def _spec_text() -> str:
    assert SPEC.is_file(), f"SPEC missing: {SPEC}"
    return SPEC.read_text(encoding="utf-8")


def _route_source() -> str:
    assert ROUTES_VOICE.is_file(), f"routes_voice.py missing: {ROUTES_VOICE}"
    return ROUTES_VOICE.read_text(encoding="utf-8")


def _mobile_files() -> list[Path]:
    return sorted(MOBILE_ROOT.rglob("*.kt"))


# ---- 1. SPEC 范围锁定 ----
def test_spec_locks_wake_word_and_half_duplex_out_of_p0() -> None:
    text = _spec_text()
    assert WAKE_SNIPPET in text, "SPEC 必须锁定唤醒词为 P1 Beta（O-003）"
    assert P0_ENTRY_SNIPPET in text, "SPEC 必须锁定 P0 三入口为主页面/悬浮球/通知按钮（O-003）"
    assert HALF_DUPLEX_DOD_SNIPPET in text, "SPEC 必须排除自动半双工 fallback 出 DoD（O-015）"
    # P0 功能表（第 2 节）行不得包含唤醒词/半双工自动降级
    for line in text.splitlines():
        if "\tP0\t" in line or line.startswith("| P0 "):
            assert "唤醒" not in line, f"P0 行不得包含唤醒词: {line.strip()}"
            assert "半双工" not in line, f"P0 行不得包含半双工自动降级: {line.strip()}"
    # P1 行必须显式承载两者（独立入口/独立能力，不得伪装为 P0）
    p1_lines = [l for l in text.splitlines() if "\tP1\t" in l or l.startswith("| P1 ")]
    assert any("唤醒词" in l for l in p1_lines), "唤醒词必须作为 P1 独立能力存在"
    assert any("半双工" in l for l in p1_lines), "半双工兼容模式必须作为 P1 独立入口存在"


# ---- 2. backend 无自动降级 ----
def test_backend_gateway_has_no_auto_fallback_to_half_duplex() -> None:
    tree = ast.parse(_route_source())
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build_voice_gateway")
    # 全双工（apm）必须是配置分支的主路径
    if_apm = next(
        (
            n
            for n in ast.walk(func)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Compare)
            and "path" in ast.dump(n.test)
        ),
        None,
    )
    assert if_apm is not None, "build_voice_gateway 必须存在 cfg.path 配置分支（全双工为主路径）"
    # HalfDuplex 装配不得出现在任何异常处理器内（禁止失败→自动降级）
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "HalfDuplex":
            parent_chain = _ancestors(func, node)
            assert not any(isinstance(a, ast.ExceptHandler) for a in parent_chain), (
                "HalfDuplex 装配不得出现在 except 处理器中（禁止主链路失败自动降级）"
            )
            assert any(isinstance(a, ast.If) for a in parent_chain), (
                "HalfDuplex 装配必须位于配置分支（else），而非无条件/异常路径"
            )
    # 装配函数不存在以 fallback 为名的降级开关
    assert "fallback" not in [a.arg for a in func.args.args + func.args.kwonlyargs]


def _ancestors(func: ast.AST, node: ast.AST) -> list[ast.AST]:
    for parent in ast.walk(func):
        for child in ast.iter_child_nodes(parent):
            if child is node:
                return [parent, *_ancestors(func, parent)]
    return []


# ---- 3. Android P0 三入口不触发唤醒词/无半双工 ----
def test_android_p0_entries_do_not_trigger_wake_or_half_duplex() -> None:
    sources = {p.name: p.read_text(encoding="utf-8") for p in _mobile_files()}
    assert sources, "Android voice 源码树为空"
    # 三入口文件必须存在（主页面/悬浮球/前台服务）
    for name in ("MainActivity.kt", "FloatingOverlay.kt", "VoiceForegroundService.kt"):
        assert name in sources, f"P0 入口缺失: {name}"
    # Task 8 统一命令层：入口经 VoiceEntry.startConversation 投递同一 ACTION_TALK 命令
    for name in ("MainActivity.kt", "FloatingOverlay.kt"):
        assert "VoiceEntry.startConversation" in sources[name], (
            f"{name} 必须经统一命令层发起（Task 8 三入口契约）"
        )
    assert "ACTION_TALK" in sources["VoiceEntry.kt"], "统一命令层必须构造 ACTION_TALK"
    # 服务端解析 source 后进入同一个 coordinator.start
    assert "VoiceEntry.resolveSource" in sources["VoiceForegroundService.kt"]
    # 三入口文件不得直接构造/调用唤醒词引擎
    for name in ("MainActivity.kt", "FloatingOverlay.kt"):
        assert "WakeWordEngine" not in sources[name], f"{name} 不得触发唤醒词（P1 边界）"
    # Android 代码树不得出现半双工自动降级引用
    for path, src in sources.items():
        assert not FALLBACK_PATTERNS.search(src), f"{path} 不得引用 half_duplex 自动降级"


def test_android_has_no_half_duplex_binary_or_source_artifact() -> None:
    for path in _mobile_files():
        raw = path.read_bytes()
        lowered = raw.lower()
        assert b"half_duplex" not in lowered and b"half-duplex" not in lowered, (
            f"禁止半双工自动降级痕迹: {path}"
        )
