"""WGC 帧文件清理单元测试（ADR-010 文件堆积对策，全 mock 不依赖真实捕获）

覆盖契约：
- 每窗口会话帧文件上限 MAX_FRAMES=200：超出删除最旧帧
- mock 帧回调制造 205 帧 → 磁盘仅保留 200 个 frame_*.png，最旧的被删
- cleanup() 删除该会话全部帧文件 → 目录无 frame_*.png（幂等）
- 删除最旧帧不影响 _last_frame_path（orchestrator 仍引用最近帧）
- SessionManager.stop_wgc / stop_all 停止路径调用 cleanup
"""
from __future__ import annotations

from pathlib import Path

import pytest

import app.capture.wgc_capture as wgc_module
from app.capture.session_manager import SessionManager
from app.capture.wgc_capture import MAX_FRAMES, WgcCapturer
from app.config import MonitorTarget


class FakeFrame:
    """模拟 windows_capture.Frame：save_as_image 落盘假数据"""

    def __init__(self, payload: bytes = b"fake-frame") -> None:
        self._payload = payload

    def save_as_image(self, path: str) -> None:
        Path(path).write_bytes(self._payload)


@pytest.fixture
def capturer(tmp_path, monkeypatch):
    # 不依赖真实 windows-capture 库（测试环境可无桌面会话）
    monkeypatch.setattr(wgc_module, "WindowsCapture", object)
    return WgcCapturer(window_title="test-app", output_dir=tmp_path)


class FakeWgc:
    """SessionManager 用的假 WgcCapturer（含 cleanup 对齐真实类）"""

    def __init__(
        self, window_title: str = "", output_dir: Path | None = None, on_frame=None
    ) -> None:
        self.stopped = False
        self.cleaned = False

    def start(self) -> None:
        pass

    def snapshot(self) -> tuple[Path | None, float]:
        return None, 0.0

    def stop(self) -> None:
        self.stopped = True

    def cleanup(self) -> None:
        self.cleaned = True


@pytest.fixture
def session_manager(tmp_path, monkeypatch):
    import app.capture.session_manager as sm

    targets = [
        MonitorTarget(app_id="codex", app_name="Codex", process_name="codex.exe")
    ]
    monkeypatch.setattr(sm, "WgcCapturer", FakeWgc)
    return SessionManager(targets, tmp_path)


class TestFrameLimit:
    def test_keeps_latest_200_of_205(self, capturer, tmp_path):
        for _ in range(205):
            capturer._handle_frame(FakeFrame())

        frames = sorted(tmp_path.glob("frame_*.png"))
        assert len(frames) == MAX_FRAMES, "205 帧后磁盘应仅保留 200 个帧文件"
        assert (tmp_path / "frame_1.png").exists() is False, "最旧帧应被删除"
        assert (tmp_path / "frame_5.png").exists() is False, "前 5 帧全部删除"
        assert (tmp_path / "frame_6.png").exists(), "第 6 帧是保留的起点"
        assert (tmp_path / "frame_205.png").exists(), "最新帧必须保留"
        numbers = sorted(int(p.stem.split("_")[1]) for p in frames)
        assert numbers == list(range(6, 206)), "保留的应为最近 200 帧 frame_6..frame_205"

    def test_last_frame_path_unaffected_by_prune(self, capturer, tmp_path):
        for _ in range(205):
            capturer._handle_frame(FakeFrame())

        assert capturer._last_frame_path == tmp_path / "frame_205.png", (
            "删除最旧帧不得影响最近帧引用（orchestrator 仍可消费）"
        )

    def test_under_limit_no_deletion(self, capturer, tmp_path):
        for _ in range(50):
            capturer._handle_frame(FakeFrame())

        assert len(list(tmp_path.glob("frame_*.png"))) == 50
        assert (tmp_path / "frame_1.png").exists(), "未超限不得删除"

    def test_callback_receives_latest_path(self, capturer, tmp_path):
        received: list[Path] = []
        capturer._on_frame = received.append
        for _ in range(205):
            capturer._handle_frame(FakeFrame())

        assert received[-1] == tmp_path / "frame_205.png", "回调收到最新帧路径"


class TestCleanup:
    def test_cleanup_empties_dir(self, capturer, tmp_path):
        for _ in range(20):
            capturer._handle_frame(FakeFrame())
        assert len(list(tmp_path.glob("frame_*.png"))) == 20

        capturer.cleanup()

        assert list(tmp_path.glob("frame_*.png")) == [], "cleanup 后目录应无帧文件"

    def test_cleanup_idempotent_and_safe(self, capturer, tmp_path):
        capturer.cleanup()  # 无帧也安全
        capturer._handle_frame(FakeFrame())
        capturer.cleanup()
        assert list(tmp_path.glob("frame_*.png")) == []


class TestSessionStopPath:
    def test_stop_wgc_calls_cleanup(self, session_manager):
        sm = session_manager
        session = sm.get("codex")
        assert session is not None
        wgc = FakeWgc()
        session.wgc = wgc

        assert sm.stop_wgc("codex") is True
        assert wgc.stopped is True, "stop 路径应调用 wgc.stop()"
        assert wgc.cleaned is True, "stop 路径应调用 wgc.cleanup()"
        assert session.wgc is None, "停止后会话不应再持有 wgc"
        assert session.mode == "none"

    def test_stop_wgc_noop_without_wgc(self, session_manager):
        assert session_manager.stop_wgc("codex") is False
        assert session_manager.stop_wgc("ghost") is False

    def test_stop_all_cleans_every_session(self, session_manager):
        sm = session_manager
        for app_id in ("codex",):
            session = sm.get(app_id)
            assert session is not None
            session.wgc = FakeWgc()

        sm.stop_all()

        for app_id in ("codex",):
            assert sm.get(app_id).wgc is None, "stop_all 后所有会话不再持有 wgc"
