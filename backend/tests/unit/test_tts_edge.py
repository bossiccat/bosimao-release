"""edge-tts 单测（mock 网络：patch edge_tts.Communicate）

覆盖：合成返回音频字节 / 缓存命中（不重复网络调用）/ LRU 淘汰最近 10 条 / 空文本 / 网络失败降级。
"""
from __future__ import annotations

import pytest

from app.voice.tts_edge import TtsEdge, TtsUnavailable


class FakeCommunicate:
    """模拟 edge_tts.Communicate：stream() 产出固定音频块"""

    def __init__(self, text: str, voice: str, fail: bool = False) -> None:
        self.text = text
        self.voice = voice
        self.fail = fail

    async def stream(self):
        if self.fail:
            raise OSError("network down")
        yield {"type": "audio", "data": b"\xff\xfb" * 8}
        yield {"type": "audio", "data": b"\xff\xfb" * 8}
        yield {"type": "WordBoundary", "data": "ignore"}


@pytest.fixture
def patch_comm(monkeypatch):
    calls: list[tuple[str, str]] = []

    def _make(**kwargs):
        def factory(text: str, voice: str):
            calls.append((text, voice))
            return FakeCommunicate(text, voice, **kwargs)
        return factory

    monkeypatch.setattr("edge_tts.Communicate", _make())
    return calls


@pytest.mark.asyncio
async def test_synthesize_returns_audio(patch_comm):
    tts = TtsEdge()
    res = await tts.synthesize("你好")
    assert res.format == "mp3_24k"
    assert res.data == b"\xff\xfb" * 16
    assert res.cached is False
    assert len(patch_comm) == 1
    assert patch_comm[0][1] == "zh-CN-XiaoxiaoNeural"


@pytest.mark.asyncio
async def test_cache_hit_no_network(patch_comm):
    tts = TtsEdge(cache_size=10)
    r1 = await tts.synthesize("第一条")
    r2 = await tts.synthesize("第一条")
    assert r2.cached is True
    assert r2.data == r1.data
    assert len(patch_comm) == 1  # 网络只调一次


@pytest.mark.asyncio
async def test_lru_eviction(patch_comm):
    tts = TtsEdge(cache_size=2)
    await tts.synthesize("a")
    await tts.synthesize("b")
    await tts.synthesize("c")
    assert tts.cache_keys() == ["b", "c"]  # a 被淘汰
    assert len(patch_comm) == 3


@pytest.mark.asyncio
async def test_empty_text_short_circuit(patch_comm):
    tts = TtsEdge()
    res = await tts.synthesize("   ")
    assert res.data == b""
    assert len(patch_comm) == 0  # 不触发网络


@pytest.mark.asyncio
async def test_network_failure_raises(monkeypatch):
    def factory(text: str, voice: str):
        return FakeCommunicate(text, voice, fail=True)

    monkeypatch.setattr("edge_tts.Communicate", factory)
    tts = TtsEdge()
    with pytest.raises(TtsUnavailable):
        await tts.synthesize("网络失败场景")
