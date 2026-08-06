"""sherpa-onnx 流式 STT 封装（mobile-voice-spec §8.3 路径 B，ADR-003 兜底链）

中文模型：sherpa-onnx streaming zipformer zh-14M（wenetspeech，流式 transducer）。
模型未下载/不可用 → SttModelUnavailable（网关返回明确错误，不崩溃）。
模型下载：scripts/download_sherpa_models.py（ModelScope/GitHub 双源）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .audio import pcm16_to_float32

logger = logging.getLogger(__name__)


class SttModelUnavailable(RuntimeError):
    """sherpa-onnx 模型未下载或运行时不可用"""


@dataclass
class SttResult:
    text: str
    ok: bool = True
    engine: str = "sherpa-onnx"
    model_status: str = "ok"


def _pick(paths: list[Path]) -> str | None:
    """选非 int8 版本（精度优先），无则回退 int8"""
    non_int8 = [p for p in paths if ".int8." not in p.name]
    cand = (non_int8 or paths)
    return str(cand[0]) if cand else None


class SttSherpa:
    """sherpa-onnx 流式 STT（OnlineRecognizer.from_transducer）

    懒加载：首次 transcribe 才 import + 初始化 recognizer。
    模型目录不存在/文件不齐 → SttModelUnavailable，提示运行下载脚本。
    """

    def __init__(self, model_dir: str, sample_rate: int = 16000) -> None:
        self._model_dir = Path(model_dir)
        self._sample_rate = sample_rate
        self._recognizer = None

    # ---------- 探测 ----------
    def available(self) -> bool:
        return self._locate() is not None

    def model_status(self) -> str:
        if self._locate() is None:
            return "missing"
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            return "runtime_missing"
        return "ok"

    def _locate(self) -> tuple[Path, str, str, str] | None:
        """找模型目录：含 tokens.txt + encoder/decoder/joiner onnx（流式 transducer）"""
        if not self._model_dir.exists():
            return None
        candidates = [
            self._model_dir,
            *[p for p in self._model_dir.iterdir() if p.is_dir()],
        ]
        for cand in candidates:
            tokens = cand / "tokens.txt"
            if not tokens.exists():
                continue
            enc = _pick(sorted(cand.glob("encoder*.onnx")))
            dec = _pick(sorted(cand.glob("decoder*.onnx")))
            joi = _pick(sorted(cand.glob("joiner*.onnx")))
            if enc and dec and joi:
                return cand, enc, dec, joi
        return None

    # ---------- 识别 ----------
    def _ensure_ready(self):
        located = self._locate()
        if located is None:
            raise SttModelUnavailable(
                "sherpa-onnx 中文模型未下载，请运行 scripts/download_sherpa_models.py "
                f"（目标目录：{self._model_dir}）"
            )
        model_dir, enc, dec, joi = located
        try:
            import sherpa_onnx
        except ImportError as e:  # pragma: no cover - 依赖装好即不触发
            raise SttModelUnavailable(f"sherpa-onnx 未安装: {e}") from e
        if self._recognizer is None:
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(model_dir / "tokens.txt"),
                encoder=enc,
                decoder=dec,
                joiner=joi,
                num_threads=2,
                sample_rate=self._sample_rate,
                feature_dim=80,
                enable_endpoint_detection=False,
                decoding_method="greedy_search",
            )
            logger.info("sherpa-onnx recognizer ready: %s", model_dir)
        return self._recognizer

    def transcribe(self, pcm: bytes) -> SttResult:
        """PCM16 16k 单声道 → 文本。

        streaming zipformer 必须分段喂（0.5s/段）+ 边喂边解码——
        一次性喂完再 decode 会返回空结果（实测 zh-14M 模型，2026-08-05 定位）。
        """
        if not pcm:
            return SttResult(text="", ok=False, model_status=self.model_status())
        recognizer = self._ensure_ready()
        samples = pcm16_to_float32(pcm)
        try:
            stream = recognizer.create_stream()
            # 分段喂：每次 0.5s（8000 样本）后立即解码（streaming 模型必需）
            chunk = self._sample_rate // 2
            for i in range(0, len(samples), chunk):
                stream.accept_waveform(sample_rate=self._sample_rate, waveform=samples[i:i + chunk])
                recognizer.decode_stream(stream)
            stream.input_finished()
            recognizer.decode_stream(stream)
            text = recognizer.get_result(stream).strip()
        except Exception as e:  # noqa: BLE001 - 识别失败降级为可读错误
            logger.warning("sherpa-onnx transcribe failed: %s", e)
            return SttResult(text="", ok=False, model_status="error")
        return SttResult(text=text, ok=True, model_status="ok")
