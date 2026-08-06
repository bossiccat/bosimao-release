"""edge-tts 集成（mobile-voice-spec §8.3：回复文本 → TTS 音频字节）

- 输出：mp3（edge-tts 默认 24kHz）；下行格式标记 mp3_24k，手机端解码播放
- 缓存：最近 cache_size 条（默认 10），命中免网络
- 网络/运行时失败 → TtsUnavailable（网关返回明确错误）
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class TtsUnavailable(RuntimeError):
    """edge-tts 网络/运行时不可用"""


@dataclass
class TtsResult:
    format: str = "mp3_24k"
    data: bytes = b""
    cached: bool = False
    voice: str = "zh-CN-XiaoxiaoNeural"


class TtsEdge:
    """edge-tts 文本合成（async；edge_tts.Communicate 可 mock）"""

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", cache_size: int = 10) -> None:
        self._voice = voice
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_size = max(1, cache_size)

    async def synthesize(self, text: str) -> TtsResult:
        text = (text or "").strip()
        if not text:
            return TtsResult(voice=self._voice)
        if text in self._cache:
            self._cache.move_to_end(text)
            return TtsResult(data=self._cache[text], cached=True, voice=self._voice)
        data = await self._synth_network(text)
        self._cache[text] = data
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return TtsResult(data=data, cached=False, voice=self._voice)

    async def _synth_network(self, text: str) -> bytes:
        try:
            import edge_tts
        except ImportError as e:  # pragma: no cover - 依赖装好即不触发
            raise TtsUnavailable(f"edge-tts 未安装: {e}") from e
        try:
            chunks: list[bytes] = []
            communicate = edge_tts.Communicate(text, self._voice)
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    chunks.append(chunk["data"])
            if not chunks:
                raise TtsUnavailable("edge-tts 返回空音频")
            return b"".join(chunks)
        except TtsUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 - 网络失败聚合
            logger.warning("edge-tts synthesize failed: %s", e)
            raise TtsUnavailable(f"edge-tts 合成失败: {e}") from e

    def cache_keys(self) -> list[str]:
        return list(self._cache.keys())
