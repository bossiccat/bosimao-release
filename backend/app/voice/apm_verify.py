"""Standalone command-line verification for the APM bridge."""
from __future__ import annotations

import asyncio
import time
import wave

from .apm_bridge import ApmBridge


async def verify(wav_path: str, out_path: str) -> None:
    audio_out: list[bytes] = []
    text_out: list[str] = []
    first_out_t: float | None = None
    started_at = time.perf_counter()

    async def on_audio(pcm: bytes) -> None:
        nonlocal first_out_t
        if first_out_t is None:
            first_out_t = time.perf_counter()
            print(f"首音频 @{(first_out_t-started_at)*1000:.0f}ms, {len(pcm)}B")
        audio_out.append(pcm)

    async def on_text(text: str) -> None:
        text_out.append(text)
        print(f"  [text @{(time.perf_counter()-started_at)*1000:.0f}ms] {text!r}")

    bridge = ApmBridge(on_audio_out=on_audio, on_text=on_text)
    await bridge.start()
    print(f"会话就绪 @{(time.perf_counter()-started_at)*1000:.0f}ms")
    with wave.open(wav_path) as wav_file:
        pcm = wav_file.readframes(wav_file.getnframes())
    frame_bytes = 1600 * 2  # 40ms @16k s16 mono
    for offset in range(0, len(pcm), frame_bytes):
        await bridge.feed_pcm(pcm[offset : offset + frame_bytes])
        await asyncio.sleep(0.04)
    await bridge.feed_pcm(b"\x00\x00" * 16000 * 3)
    deadline = time.perf_counter() + 25
    while time.perf_counter() < deadline and not audio_out:
        await asyncio.sleep(0.2)
    await bridge.close()

    print(f"文本: {''.join(text_out)[:200]!r}")
    print(f"音频块: {len(audio_out)}, 总字节: {sum(len(chunk) for chunk in audio_out)}")
    if audio_out and out_path:
        with wave.open(out_path, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"".join(audio_out))
        print(f"已保存: {out_path}")


def main() -> None:
    import argparse
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="ApmBridge 独立验证")
    parser.add_argument("--wav", required=True, help="16k s16 mono WAV 输入")
    parser.add_argument("--out", default="", help="输出 WAV（下行音频拼接）")
    args = parser.parse_args()
    asyncio.run(verify(args.wav, args.out))
