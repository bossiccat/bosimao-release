"""SessionManager 单元测试（全部 mock，不依赖真实窗口/捕获）

覆盖契约：
- locate_all 窗口未找到 → mode="none"，last_error="窗口未找到"
- locate_all 窗口找到 → 未授权 mode="pending-auth"；授权后 start_wgc → mode="wgc"
- start_wgc 未授权 / 窗口缺失 → 返回 False 且不创建 WgcCapturer
- snapshot 新帧 → 返回路径、mode="wgc"，不触达 DXGI
- snapshot 无新帧（旧帧/未出帧）→ 降级 DXGI，mode="dxgi"
- snapshot WGC 旧帧超 30s（窗口丢失）→ mode="lost" 返回 None
- locate_all 定位异常 → last_error 记录、不抛出
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.capture.session_manager as session_manager_module
from app.capture.session_manager import CaptureSession, SessionManager
from app.config import MonitorTarget
from app.capture.window_finder import WindowInfo


class FakeWgcClass:
    """mock WgcCapturer 类（start_wgc 走类构造）"""

    instances: list["FakeWgcClass"] = []

    def __init__(
        self, window_title: str = "", output_dir: Path | None = None, on_frame=None
    ) -> None:
        self.window_title = window_title
        self.output_dir = output_dir
        self.started = False
        self.last_frame: Path | None = None
        self.last_frame_at: float = 0.0
        FakeWgcClass.instances.append(self)

    def start(self) -> None:
        self.started = True

    def snapshot(self) -> tuple[Path | None, float]:
        return self.last_frame, self.last_frame_at


class FakeDxgiClass:
    """mock DxgiFallback 类（snapshot 无新帧时创建）"""

    instances: list["FakeDxgiClass"] = []

    def __init__(self, output_dir: Path, max_width: int = 1280) -> None:
        self.output_dir = output_dir
        self.max_width = max_width
        self.capture_calls = 0
        self.last_rect = None
        self.capture_result: Path | None = Path(output_dir) / "dxgi_1.png"
        FakeDxgiClass.instances.append(self)

    def capture_once(self, rect=None) -> Path | None:
        self.capture_calls += 1
        self.last_rect = rect
        return self.capture_result


class Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


@pytest.fixture(autouse=True)
def _clean_fakes():
    FakeWgcClass.instances.clear()
    FakeDxgiClass.instances.clear()
    yield
    FakeWgcClass.instances.clear()
    FakeDxgiClass.instances.clear()


@pytest.fixture
def manager(tmp_path, monkeypatch):
    targets = [
        MonitorTarget(app_id="codex", app_name="Codex", process_name="codex.exe"),
        MonitorTarget(app_id="trae", app_name="Trae", process_name="trae.exe"),
    ]
    monkeypatch.setattr(session_manager_module, "WgcCapturer", FakeWgcClass)
    monkeypatch.setattr(session_manager_module, "DxgiFallback", FakeDxgiClass)
    return SessionManager(targets, tmp_path)


def make_window(title="Codex") -> WindowInfo:
    return WindowInfo(hwnd=1, title=title, pid=10, process_name="codex.exe")


class TestLocateAll:
    def test_window_not_found_sets_none_mode(self, manager, monkeypatch):
        monkeypatch.setattr(
            session_manager_module, "find_window", lambda *a, **k: None
        )
        manager.locate_all()
        s = manager.get("codex")
        assert s is not None and s.mode == "none"
        assert s.last_error == "窗口未找到"
        assert s.window is None

    def test_window_found_unauthorized_pending_auth(self, manager, monkeypatch):
        monkeypatch.setattr(
            session_manager_module, "find_window", lambda *a, **k: make_window()
        )
        manager.locate_all()
        s = manager.get("codex")
        assert s is not None and s.mode == "pending-auth", "未授权应为 pending-auth"
        assert s.window is not None

    def test_window_found_authorized_wgc_mode(self, manager, monkeypatch):
        monkeypatch.setattr(
            session_manager_module, "find_window", lambda *a, **k: make_window()
        )
        manager.mark_authorized("codex")
        manager.locate_all()
        s = manager.get("codex")
        assert s is not None and s.mode == "wgc", "已授权找到窗口应为 wgc"

    def test_locate_exception_records_error(self, manager, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("finder down")

        monkeypatch.setattr(session_manager_module, "find_window", boom)
        manager.locate_all()  # 不得抛出
        s = manager.get("codex")
        assert s is not None and s.last_error == "finder down"


class TestAuthorizeFlow:
    def test_state_transition_pending_auth_to_authorized_to_wgc(self, manager, monkeypatch):
        monkeypatch.setattr(
            session_manager_module, "find_window", lambda *a, **k: make_window()
        )
        manager.locate_all()
        s = manager.get("codex")
        assert s is not None and s.mode == "pending-auth"

        manager.mark_authorized("codex")
        assert s.authorized is True

        ok = manager.start_wgc("codex")
        assert ok is True
        assert s.mode == "wgc"
        assert FakeWgcClass.instances, "start_wgc 应创建 WgcCapturer"
        wgc = FakeWgcClass.instances[-1]
        assert wgc.started is True, "WgcCapturer.start 应被调用"
        assert wgc.window_title == "Codex"

    def test_start_wgc_requires_authorized(self, manager, monkeypatch):
        monkeypatch.setattr(
            session_manager_module, "find_window", lambda *a, **k: make_window()
        )
        manager.locate_all()
        assert manager.start_wgc("codex") is False
        assert FakeWgcClass.instances == [], "未授权不得创建 WgcCapturer"

    def test_start_wgc_requires_window(self, manager, monkeypatch):
        monkeypatch.setattr(
            session_manager_module, "find_window", lambda *a, **k: None
        )
        manager.locate_all()
        manager.mark_authorized("codex")
        assert manager.start_wgc("codex") is False
        assert FakeWgcClass.instances == []

    def test_start_wgc_unknown_app_returns_false(self, manager):
        assert manager.start_wgc("ghost") is False


class TestSnapshot:
    def test_new_frame_returns_wgc_path(self, manager, monkeypatch):
        clock = Clock(1_001)
        monkeypatch.setattr(session_manager_module.time, "time", clock)
        s = manager.get("codex")
        assert s is not None
        wgc = FakeWgcClass()
        wgc.last_frame = Path("frame_1.png")
        wgc.last_frame_at = 1_000
        s.wgc = wgc
        s.last_consumed_at = 0

        path = manager.snapshot("codex")

        assert path == Path("frame_1.png")
        assert s.mode == "wgc"
        assert FakeDxgiClass.instances == [], "有新帧不得触达 DXGI"

    def test_no_new_frame_falls_back_to_dxgi(self, manager, monkeypatch):
        clock = Clock(1_001)
        monkeypatch.setattr(session_manager_module.time, "time", clock)
        s = manager.get("codex")
        assert s is not None
        wgc = FakeWgcClass()
        wgc.last_frame = None
        wgc.last_frame_at = 0.0
        s.wgc = wgc
        s.last_consumed_at = 0

        path = manager.snapshot("codex")

        assert path is not None
        assert s.mode == "dxgi", "无新帧应降级 DXGI"
        assert FakeDxgiClass.instances, "应创建 DxgiFallback"
        assert FakeDxgiClass.instances[0].capture_calls == 1

    def test_no_new_frame_old_ts_falls_back_to_dxgi(self, manager, monkeypatch):
        """WGC 返回 (path, old_ts) 且 old_ts <= last_consumed_at → 旧帧（未超 30s），走 DXGI"""
        clock = Clock(1_010)  # stale=10s < LOST_TIMEOUT(30s) → 走 DXGI 而非 lost
        monkeypatch.setattr(session_manager_module.time, "time", clock)
        s = manager.get("codex")
        assert s is not None
        wgc = FakeWgcClass()
        wgc.last_frame = Path("old.png")
        wgc.last_frame_at = 1_000
        s.wgc = wgc
        s.last_consumed_at = 1_000  # 旧帧已消费

        path = manager.snapshot("codex")

        assert s.mode == "dxgi"
        assert path is not None

    def test_dxgi_passes_window_rect(self, manager, monkeypatch):
        clock = Clock(1_100)
        monkeypatch.setattr(session_manager_module.time, "time", clock)
        s = manager.get("codex")
        assert s is not None
        s.window = SimpleNamespace(rect=(0, 0, 800, 600))
        wgc = FakeWgcClass()
        wgc.last_frame_at = 0.0
        s.wgc = wgc

        manager.snapshot("codex")

        assert FakeDxgiClass.instances[0].last_rect == (0, 0, 800, 600), "DXGI 应收到窗口裁剪矩形"

    def test_wgc_stale_30s_sets_lost(self, manager, monkeypatch):
        """窗口丢失：WGC 旧帧超 30s 无新帧 → mode='lost' 返回 None"""
        clock = Clock(1_031)
        monkeypatch.setattr(session_manager_module.time, "time", clock)
        s = manager.get("codex")
        assert s is not None
        wgc = FakeWgcClass()
        wgc.last_frame = Path("old.png")
        wgc.last_frame_at = 1_000
        s.wgc = wgc
        s.last_consumed_at = 1_000

        path = manager.snapshot("codex")

        assert path is None
        assert s.mode == "lost", "旧帧超 30s 应置 lost"
        assert FakeDxgiClass.instances == [], "判定 lost 后不应再走 DXGI"

    def test_dxgi_failure_sets_lost(self, manager, monkeypatch):
        clock = Clock(1_001)
        monkeypatch.setattr(session_manager_module.time, "time", clock)
        s = manager.get("codex")
        assert s is not None
        wgc = FakeWgcClass()  # 无帧
        s.wgc = wgc

        # 让新建的 FakeDxgi 兜底失败
        monkeypatch.setattr(
            FakeDxgiClass, "capture_once", staticmethod(lambda rect=None: None)
        )

        path = manager.snapshot("codex")

        assert path is None
        assert s.mode == "lost", "DXGI 兜底失败应置 lost"

    def test_unknown_app_returns_none(self, manager):
        assert manager.snapshot("ghost") is None
