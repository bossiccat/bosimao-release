"""PoC-003: silero-vad 门控 + 全双工语音覆盖检查（风险③）
报告要求（docs/poc/POC-003-voice-duplex.md）：
  1. 原生全双工端到端延迟 ≤ 1.5s（P50）       —— 需 Comni 桌面版实测（录屏），脚本侧未实现
  2. 打断响应 < 500ms，可连续打断 3 次不崩溃     —— 需 Comni 桌面版实测，脚本侧未实现
  3. VAD 门控：误触发 ≤5%/10min；检测延迟 <300ms —— 检测延迟已实现；误触发需受控静默测试，未实现
  4. 麦克风权限弹窗可完成                        —— 交互项

未实现项一律以 TODO + 退出码 2 标记"未实现"，禁止假 PASS。
用法:
  python scripts/poc_003_voice.py [--duration 120]        # 运行 VAD 检测延迟测量
  python scripts/poc_003_voice.py --coverage              # 仅打印覆盖检查（快，不录音）
退出码: 0=全部实现且通过, 1=参数非法/已实现项不通过, 2=存在未实现项
"""
from __future__ import annotations

import argparse
import sys
import time

COVERAGE = [
    ("全双工端到端延迟 <=1.5s(P50)", "TODO", "需 Comni 桌面版/官方 Demo 录屏测说话结束→首字延迟"),
    ("打断响应 <500ms / 连续打断3次", "TODO", "需 Comni 桌面版实测'压扁'而非排队"),
    ("VAD 检测延迟 <300ms", "IMPLEMENTED", "本脚本 --duration 运行可测"),
    ("VAD 误触发 <=5%/10min", "TODO", "需受控静默测试 + 人工计数，未实现"),
    ("麦克风权限弹窗", "MANUAL", "Windows 首次授权弹窗"),
]


def print_coverage() -> list[str]:
    print("\n=== PoC-003 覆盖检查（对照 POC-003-voice-duplex.md）===")
    todos = []
    for name, status, note in COVERAGE:
        flag = {"TODO": "[未实现]", "IMPLEMENTED": "[已实现]", "MANUAL": "[手工]"}[status]
        print(f"  {flag} {name}  — {note}")
        if status == "TODO":
            todos.append(name)
    return todos


def main() -> int:
    ap = argparse.ArgumentParser(description="PoC-003 silero-vad 门控 + 全双工覆盖检查")
    ap.add_argument("--duration", type=int, default=120, help="VAD 测试时长(秒)")
    ap.add_argument("--coverage", action="store_true", help="仅打印覆盖检查并退出")
    args = ap.parse_args()

    if args.duration <= 0:
        print("[错误] --duration 必须为正整数", file=sys.stderr)
        return 1

    todos = print_coverage()

    if args.coverage:
        print(f"\n结论: {'存在未实现项，退出码 2（未实现）' if todos else '全部实现'}")
        return 2 if todos else 0

    # VAD 检测延迟测量（已实现部分）
    try:
        import numpy as np
        import sounddevice as sd
        from silero_vad import load_silero_vad, VADIterator
    except ImportError as e:
        print(f"\n[错误] 依赖缺失（先运行 setup_env.ps1）: {e}", file=sys.stderr)
        return 2

    SAMPLE_RATE = 16000
    CHUNK = 512
    print("\n加载 silero-vad...")
    model = load_silero_vad()
    vad = VADIterator(model, threshold=0.5, min_silence_duration_ms=300)

    print(f"开始录音测试 {args.duration}s（请正常说话几次，再静默几次）")
    speech_events = []
    last_event = time.time()
    start = time.time()

    def callback(indata, frames, t, status):
        audio = np.frombuffer(indata, dtype=np.float32).reshape(-1)
        speech_dict = vad(audio)
        nonlocal last_event
        now = time.time()
        if speech_dict and "start" in speech_dict:
            latency = (now - last_event) * 1000
            speech_events.append({"type": "start", "t": now - start, "latency_ms": latency})
            print(f"  [检测] 人声开始 t={now-start:.1f}s 距上次事件 {latency:.0f}ms")
        elif speech_dict and "end" in speech_dict:
            print(f"  [检测] 人声结束 t={now-start:.1f}s")
        last_event = now

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=CHUNK, callback=callback):
        sd.sleep(args.duration * 1000)

    starts = [e for e in speech_events if e["type"] == "start"]
    latencies = [e["latency_ms"] for e in starts if e["latency_ms"] > 50]  # 排除首事件
    print(f"\n=== PoC-003 (VAD) 结果 ===")
    print(f"  人声开始事件: {len(starts)} 次")
    print(f"  检测延迟(排除首个): {latencies if latencies else 'n/a'}")
    vad_pass = None
    if latencies:
        avg = sum(latencies) / len(latencies)
        vad_pass = avg < 300
        print(f"  平均延迟 {avg:.0f}ms -> {'PASS (<300ms)' if vad_pass else 'FAIL'}")

    # 门禁：存在未实现项 → 退出码 2，绝不假 PASS
    print(f"\n结论: 未实现项 {len(todos)} 个 -> 退出码 2（未实现），"
          f"已实现 VAD 检测延迟 {'通过' if vad_pass else '未测/不通过'}。"
          "全双工/打断/误触发需 Comni 桌面版实测后再判定。")
    return 2


if __name__ == "__main__":
    sys.exit(main())
