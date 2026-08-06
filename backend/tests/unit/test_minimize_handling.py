"""Trae 最小化崩溃防御单元测试（POC-002 保留项 3，全部 mock）

覆盖契约（任务 2）：
- 检测窗口最小化（IsIconic mock）→ 主动停 WGC（避免原生崩溃）
- 最小化期间走 DXGI 兜底（snapshot → dxgi，不再触达 WGC）
- 恢复可见 → 重建 WGC 会话（调用序列：stop_wgc → locate → start_wgc）
- WGC 崩溃（is_running=False）→ 清理崩溃会话 → 重建
- status-only 降级会话：不截屏分析
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import app.capture.session_manager as sm_module
from app.capture.session_manager import SessionManager
from app.capture.window_finder import WindowInfo
from app.config import AppConfig, MonitorTarget
from app.core.events import EventBus
from app.core.orchestrator import Orchestrator
from app.core.state import AgentState, state as global_state
from app.engine.vision_analyzer import VisionResult


class FakeWgc:
    instances: list["FakeWgc"] = []

    def __init__(
        self, window_title: str = "", output_dir: Path | None = None, on_frame=None
    ) -> None:
        self.window_title = window_title
        self.output_dir = output_dir
        self.started = False
        self.running = False
        self.cleaned = False
        self.last_frame: Path | None = None
        self.last_frame_at: float = 0.0
        FakeWgc.instances.append(self)

    def start(self) -> None:
        self.started = True
        self.running = True
        # 模拟重建后立即出首帧（真实 WGC 恢复后 ~0.5s 内续帧）
        self.last_frame = Path("frame_1.png")
        self.last_frame_at = time.time()

    def stop(self) -> None:
        self.running = False

    def is_running(self) -> bool:
        return self.running

    def snapshot(self) -> tuple[Path | None, float]:
        return self.last_frame, self.last_frame_at

    def cleanup(self) -> None:
        self.cleaned = True


class FakeDxgi:
    instances: list["FakeDxgi"] = []

    def __init__(self, output_dir: Path, max_width: int = 1280) -> None:
        self.output_dir = output_dir
        self.capture_calls = 0
        FakeDxgi.instances.append(self)

    def capture_once(self, rect=None) -> Path | None:
        self.capture_calls += 1
        return Path(self.output_dir) / "dxgi_1.png"


class FakeAnalyzer:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    async def analyze(self, screenshot: Path) -> VisionResult:
        self.calls.append(screenshot)
        return VisionResult(state=AgentState.PROGRESS, summary="mock", raw="")


def make_window(title="TRAE Work CN [管理员]") -> WindowInfo:
    return WindowInfo(hwnd=100, title=title, pid=25040, process_name="trae.exe")


@pytest.fixture(autouse=True)
def _clean():
    FakeWgc.instances.clear()
    FakeDxgi.instances.clear()
    global_state._sessions.clear()
    yield
    FakeWgc.instances.clear()
    FakeDxgi.instances.clear()
    global_state._sessions.clear()


@pytest.fixture
def manager(tmp_path, monkeypatch):
    targets = [
        MonitorTarget(app_id="trae", app_name="Trae", process_name="trae.exe"),
    ]
    monkeypatch.setattr(sm_module, "WgcCapturer", FakeWgc)
    monkeypatch.setattr(sm_module, "DxgiFallback", FakeDxgi)
    monkeypatch.setattr(sm_module, "find_window", lambda *a, **k: make_window())
    return SessionManager(
        targets, tmp_path, auth_file=tmp_path / "authorized_windows.json"
    )


@pytest.fixture
def orch(manager):
    cfg = AppConfig()
    orch = Orchestrator(
        cfg, EventBus(), client=None, analyzer=FakeAnalyzer(), push=None, reminder=None
    )
    orch._sessions = manager  # type: ignore[assignment]
    return orch


class TestSessionManagerMinimize:
    def test_handle_minimized_stops_wgc_and_sets_dxgi(self, manager):
        s = manager.get("trae")
        assert s is not None
        s.window = make_window()
        s.authorized = True
        s.mode = "wgc"
        wgc = FakeWgc()
        wgc.start()
        s.wgc = wgc

        stopped = manager.handle_minimized("trae")

        assert stopped is True
        assert s.wgc is None, "最小化必须停 WGC"
        assert wgc.running is False
        assert wgc.cleaned is True, "停 WGC 应清理帧文件"
        assert s.mode == "dxgi", "最小化后应切 DXGI 兜底"
        assert "最小化" in s.last_error

    def test_handle_minimized_idempotent(self, manager):
        s = manager.get("trae")
        assert s is not None
        s.window = make_window()
        assert manager.handle_minimized("trae") is False, "无 WGC 会话时幂等返回 False"

    def test_is_minimized_uses_iconic(self, manager, monkeypatch):
        s = manager.get("trae")
        assert s is not None
        s.window = make_window()
        monkeypatch.setattr(sm_module, "is_window_minimized", lambda hwnd: True)
        assert manager.is_minimized("trae") is True
        monkeypatch.setattr(sm_module, "is_window_minimized", lambda hwnd: False)
        assert manager.is_minimized("trae") is False

    def test_handle_restored_rebuilds_wgc(self, manager):
        s = manager.get("trae")
        assert s is not None
        s.window = make_window()
        s.authorized = True
        s.mode = "dxgi"

        ok = manager.handle_restored("trae")

        assert ok is True
        assert s.wgc is not None, "恢复后应重建 WGC 会话"
        assert s.wgc.started is True
        assert s.mode == "wgc"
        assert FakeWgc.instances, "重建应创建新的 WgcCapturer"

    def test_handle_restored_refuses_status_only(self, manager):
        s = manager.get("trae")
        assert s is not None
        s.window = make_window()
        s.authorized = True
        s.mode = "status-only"
        assert manager.handle_restored("trae") is False, "拒绝降级窗口不得自动重建"


class TestOrchestratorMinimize:
    @pytest.mark.asyncio
    async def test_minimized_stops_wgc_and_dxgi_fallback(self, orch, manager):
        s = manager.get("trae")
        assert s is not None
        s.window = make_window()
        s.authorized = True
        s.mode = "wgc"
        wgc = FakeWgc()
        wgc.start()
        s.wgc = wgc
        snap = global_state.get_or_create("trae", "Trae")
        snap.last_frame_at = 0  # 强制轮询

        # 最小化：IsIconic=True
        import app.capture.session_manager as sm

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(sm, "is_window_minimized", lambda hwnd: True)
        try:
            await orch._tick_one("trae")
        finally:
            monkeypatch.undo()

        # 调用序列断言：停 WGC → DXGI 兜底
        assert s.wgc is None, "最小化必须停 WGC（避免崩溃）"
        assert FakeDxgi.instances, "最小化期间应走 DXGI 兜底"
        assert FakeDxgi.instances[-1].capture_calls >= 1
        assert s.mode == "dxgi"
        assert orch._analyzer.calls, "DXGI 出图后应继续分析"
        assert snap.capture_mode == "dxgi"

    @pytest.mark.asyncio
    async def test_restored_rebuilds_wgc(self, orch, manager):
        s = manager.get("trae")
        assert s is not None
        s.window = make_window()
        s.authorized = True
        s.mode = "dxgi"
        s.wgc = None  # 已停（最小化期间）
        snap = global_state.get_or_create("trae", "Trae")
        snap.last_frame_at = 0

        # 恢复：IsIconic=False
        await orch._tick_one("trae")

        assert s.wgc is not None, "恢复后应重建 WGC 会话"
        assert s.wgc.started is True
        assert s.mode == "wgc"
        assert FakeWgc.instances, "重建应创建新的 WgcCapturer"

    @pytest.mark.asyncio
    async def test_wgc_crash_rebuilds(self, orch, manager):
        s = manager.get("trae")
        assert s is not None
        s.window = make_window()
        s.authorized = True
        s.mode = "wgc"
        crashed = FakeWgc()  # 从未 start → running=False（模拟 on_closed/进程 exit）
        s.wgc = crashed
        snap = global_state.get_or_create("trae", "Trae")
        snap.last_frame_at = 0

        await orch._tick_one("trae")

        assert crashed.cleaned is True, "崩溃会话应被清理"
        assert s.wgc is not None and s.wgc is not crashed, "崩溃后应重建新 WGC 会话"
        assert s.wgc.started is True
        assert s.mode == "wgc"

    @pytest.mark.asyncio
    async def test_status_only_skips_capture(self, orch, manager):
        s = manager.get("trae")
        assert s is not None
        s.window = make_window()
        s.authorized = False
        s.mode = "status-only"
        s.wgc = None
        snap = global_state.get_or_create("trae", "Trae")
        snap.last_frame_at = 0

        bus_events: list[dict] = []

        async def record(data: dict) -> None:
            bus_events.append(data)

        orch._bus.subscribe("session_updated", record)

        await orch._tick_one("trae")

        # status-only：不截屏/不分析，仅上报状态
        assert orch._analyzer.calls == [], "status-only 不得截屏分析"
        assert FakeDxgi.instances == [], "status-only 不得触达 DXGI"
        assert snap.state == AgentState.UNKNOWN
        assert snap.capture_mode == "status-only"
        assert snap.last_summary == "未授权，仅状态监控"
        assert bus_events, "status-only 状态应广播 session_updated"

    @pytest.mark.asyncio
    async def test_crash_rebuild_throttled(self, orch, manager):
        """崩溃重建冷却：刚重建过 → 崩溃会话被清理但不立即重建（防线程堆积）"""
        s = manager.get("trae")
        assert s is not None
        s.window = make_window()
        s.authorized = True
        s.mode = "wgc"
        s.last_rebuild_at = time.time()  # 刚重建过（冷却期内）
        crashed = FakeWgc()  # running=False（模拟 on_closed/进程 exit）
        s.wgc = crashed
        snap = global_state.get_or_create("trae", "Trae")
        snap.last_frame_at = 0

        before = len(FakeWgc.instances)
        await orch._tick_one("trae")

        assert crashed.cleaned is True, "崩溃会话应被清理"
        assert s.wgc is None, "冷却期内不得重建新 WGC 会话"
        assert len(FakeWgc.instances) == before, "不得 spawn 新 WGC 线程"
        assert s.mode == "dxgi", "冷却期内保持 DXGI 兜底"
