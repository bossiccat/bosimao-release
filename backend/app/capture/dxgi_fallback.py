"""DXGI 兜底捕获（窗口最小化/空帧时按显示器整屏或窗口区域裁剪）"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from .window_finder import WindowRect

logger = logging.getLogger(__name__)

try:
    from windows_capture import WindowsCapture, Frame, InternalCaptureControl
except ImportError:
    WindowsCapture = None  # type: ignore[assignment]


class DxgiFallback:
    """整屏/窗口区域捕获兜底（wgc 空帧时使用）。

    支持按窗口 rect 裁剪（只取目标窗口区域）并按 max_width 降采样，
    避免把无关桌面内容送入视觉模型。
    """

    def __init__(self, output_dir: Path, max_width: int = 1280) -> None:
        self._output_dir = output_dir
        self._max_width = max_width
        self._last_frame: Path | None = None
        self._frame_count = 0

    def _capture_screen(self, path: Path) -> bool:
        """同步捕获一帧整屏到 path（阻塞式，约 50-100ms）"""
        if WindowsCapture is None:
            return False
        self._output_dir.mkdir(parents=True, exist_ok=True)
        try:
            capture = WindowsCapture(
                monitor_index=1,  # 主显示器整屏（2.0.0 为 1-based，0 会抛异常）
                cursor_capture=None,
                draw_border=None,
            )

            @capture.event
            def on_frame_arrived(frame: Frame, capture_control: InternalCaptureControl):
                frame.save_as_image(str(path))
                capture_control.stop()

            @capture.event
            def on_closed(capture_control: InternalCaptureControl):
                # 2.0.0 强制要求 on_closed，否则 start() 抛异常
                logger.info("dxgi screen capture closed")

            capture.start()  # 拿到第一帧即 stop
            return True
        except Exception:  # noqa: BLE001
            logger.exception("dxgi capture failed")
            return False

    def _crop_and_downsample(self, src: Path, dst: Path, box: tuple[int, int, int, int]) -> None:
        """裁剪 + 降采样到 max_width（PIL）"""
        try:
            with Image.open(src) as img:
                w, h = img.size
                # 窗口坐标可能超出屏幕（多显示器/负坐标）→ 夹取到图内
                box = (
                    max(0, box[0]),
                    max(0, box[1]),
                    min(w, box[2]),
                    min(h, box[3]),
                )
                if box[2] - box[0] <= 0 or box[3] - box[1] <= 0:
                    raise ValueError(f"空裁剪区域: {box} (img={w}x{h})")
                cropped = img.crop(box)
                if cropped.width > self._max_width:
                    ratio = self._max_width / cropped.width
                    new_size = (self._max_width, max(1, int(cropped.height * ratio)))
                    cropped = cropped.resize(new_size, Image.LANCZOS)
                cropped.save(dst, "PNG")
        except Exception:  # noqa: BLE001
            logger.exception("dxgi crop failed: %s", src)
            # 兜底：降采样原图（不裁剪），保证调用方有图可用
            self._downsample(src, dst)

    def _downsample(self, src: Path, dst: Path) -> None:
        try:
            with Image.open(src) as img:
                if img.width > self._max_width:
                    ratio = self._max_width / img.width
                    new_size = (self._max_width, max(1, int(img.height * ratio)))
                    img = img.resize(new_size, Image.LANCZOS)
                img.save(dst, "PNG")
        except Exception:  # noqa: BLE001
            logger.exception("dxgi downsample failed: %s", src)
            dst.write_bytes(src.read_bytes())

    def capture_once(self, rect: WindowRect | None = None) -> Path | None:
        """同步捕获一帧（整屏），按窗口 rect 裁剪 + 降采样。

        rect 为 None 或非法时只做降采样（整屏送入模型）。
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._frame_count += 1
        raw_path = self._output_dir / f"dxgi_raw_{self._frame_count}.png"
        if not self._capture_screen(raw_path):
            return None

        out_path = self._output_dir / f"dxgi_{self._frame_count}.png"
        if rect is not None and rect.width > 0 and rect.height > 0:
            box = (rect.left, rect.top, rect.left + rect.width, rect.top + rect.height)
            self._crop_and_downsample(raw_path, out_path, box)
        else:
            self._downsample(raw_path, out_path)
        try:
            raw_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001  沙盒/回收站不可用时保留原图
            logger.debug("raw cleanup skipped: %s", raw_path)
        self._last_frame = out_path
        return out_path
