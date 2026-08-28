"""ApmBridge 独立验证 CLI（原 apm_bridge.py 尾部拆出——单文件行数门禁）

用法：
    python -m backend.app.voice.apm_verify --wav tmp/poc_b3_ask_16k.wav --out tmp/bridge_out.wav
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time

from .apm_bridge import ApmBridge


async def _verify(wav_path: str, out_path: str) -> None:
    import wave

    audio_out: list[bytes] = []
    text_out: list[str] = []
    first_out_t: float | None = None
    t0 = time.perf_counter()

    async def on_audio(pcm: bytes) -> None:
        nonlocal first_out_t
        if first_out_t is None:
            first_out_t = time.perf_counter()
            print(f"首音频 @{(first_out_t-t0)*1000:.0f}ms, {len(pcm)}B")
        audio_out.append(pcm)

    async def on_text(t: str) -> None:
        text_out.append(t)
        print(f"  [text @{(time.perf_counter()-t0)*1000:.0f}ms] {t!r}")

    bridge = ApmBridge(on_audio_out=on_audio, on_text=on_text)
    await bridge.start()
    print(f"会话就绪 @{(time.perf_counter()-t0)*1000:.0f}ms")

    w = wave.open(wav_path)
    pcm = w.readframes(w.getnframes())
    # 按 40ms 帧喂（模拟手机 40ms 采集帧）
    frame = 1600  # 40ms @16k = 1600 样本 = 3200B
    for i in range(0, len(pcm), frame * 2):
        await bridge.feed_pcm(pcm[i : i + frame * 2])
        await asyncio.sleep(0.04)
    # 尾部 3s 静音（VAD 判定说完）
    silence = b"\x00\x00" * 16000 * 3
    await bridge.feed_pcm(silence)
    # 等回复（最多 25s）
    deadline = time.perf_counter() + 25
    while time.perf_counter() < deadline and not audio_out:
        await asyncio.sleep(0.2)
    await bridge.close()

    print(f"文本: {''.join(text_out)[:200]!r}")
    print(f"音频块: {len(audio_out)}, 总字节: {sum(len(b) for b in audio_out)}")
    if audio_out and out_path:
        with wave.open(out_path, "wb") as wo:
            wo.setnchannels(1)
            wo.setsampwidth(2)
            wo.setframerate(16000)
            wo.writeframes(b"".join(audio_out))
        print(f"已保存: {out_path}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="ApmBridge 独立验证")
    parser.add_argument("--wav", required=True, help="16k s16 mono WAV 输入")
    parser.add_argument("--out", default="", help="输出 WAV（下行音频拼接）")
    args = parser.parse_args()
    asyncio.run(_verify(args.wav, args.out))


if __name__ == "__main__":
    main()
