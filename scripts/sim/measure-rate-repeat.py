"""重复测量：同文本语速比 × N 轮 + 打断延迟 1 轮（云端模拟器，无需真机）。

为什么要重复
------------
单次样本不足以判「修复有效」—— 2026-09-13 下行队列修复后只跑了一轮，比值 0.945x。
这里跑多轮看**波动**，并顺带在冲刷对齐之后重测打断延迟。

两件事不能在同一个设置下测
--------------------------
① 语速/完整度：必须 **关掉** 打断注入（`SIM_BARGE_IN_DISABLE=1`）；否则回复在首帧后
   800ms 就被插话截断，量到的是「拼接体」而不是回复本身。
② 打断延迟：必须 **打开** 注入。两者分开跑，绝不同时。

口径
----
- 模型侧时长：桥日志 `[lat] model audio done ... seconds=X`（24k mono s16 真值）
- 手机侧时长：METRICS 的 `speech_reply_frames × 20ms` 与 `reply_frames × 20ms`
- 参照：edge-tts 读**同一段回复文本**，用 `sim_phone.measure_speech_seconds` 同口径
- 判据：语音口径比值 ≥0.85 视为「音频没被丢」；<0.85 要查丢帧点

用法（必须整条命令内跑完，本平台会收割跨调用子进程）：
    ./.venv/Scripts/python.exe tmp/measure-rate-repeat.py [轮数] [--barge]
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
D = ROOT / "outputs" / "deploy-backup-20260911"
PY = sys.executable
sys.path.insert(0, str(ROOT / "cloudbridge"))

import sim_phone  # noqa: E402

TEXT_RE = re.compile(r"apm text: (.*)$")
MODEL_RE = re.compile(r"model audio done total_bytes=(\d+) seconds=([\d.]+)")
METRICS_RE = re.compile(r"METRICS:\s*(\{.*?\n\})", re.S)


def reply_text() -> str:
    """取最后一条 response.created 之后的所有 `apm text:` 拼接。"""
    log = (D / "local-rtc-bridge.log").read_text(encoding="utf-8", errors="replace").splitlines()
    turn = None
    for i, line in enumerate(log):
        if "response.created" in line:
            turn = i
    if turn is None:
        return ""
    return "".join(TEXT_RE.search(l).group(1).strip() for l in log[turn:] if TEXT_RE.search(l))


def model_seconds() -> float | None:
    log = (D / "local-rtc-bridge.log").read_text(encoding="utf-8", errors="replace")
    hits = MODEL_RE.findall(log)
    return float(hits[-1][1]) if hits else None


def age_drop_lines() -> list[str]:
    log = (D / "local-rtc-bridge.log").read_text(encoding="utf-8", errors="replace")
    return [l for l in log.splitlines() if "audio age-drop" in l]


def barge_stop_lines() -> list[str]:
    """桥侧权威口径原文：`[lat] barge_in stop old_reply=... frames=... stop_ms=...`

    全部在同一 rtc_bridge 进程内用 time.monotonic() 相减，按 reply_id 锚定旧回复末帧。
    手机侧 barge_in_stop_ms 仅作交叉校验（onPlayAudioFrame 拿不到 reply_id）。
    """
    log = (D / "local-rtc-bridge.log").read_text(encoding="utf-8", errors="replace")
    return [l.strip() for l in log.splitlines() if "barge_in stop" in l]


def metrics() -> dict:
    p = D / "e2e-phone.out.txt"
    if not p.is_file():
        return {}
    m = METRICS_RE.search(p.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def run_once(idx: int, *, barge: bool) -> dict:
    env = dict(os.environ)
    if not barge:
        env["SIM_BARGE_IN_DISABLE"] = "1"
    else:
        env.pop("SIM_BARGE_IN_DISABLE", None)
    env["SIM_PROMPT_WAV_NAME"] = f"sim-prompt-run{idx}.wav"
    print(f"\n===== 第 {idx} 轮（{'打断测量' if barge else '完整回复/语速'}）=====", flush=True)
    with (D / f"repeat-run{idx}.log").open("w", encoding="utf-8") as fh:
        subprocess.run([PY, str(ROOT / "scripts" / "sim" / "run-sim-e2e.py")],
                       cwd=str(ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT,
                       timeout=420)

    m = metrics()
    rec: dict = {
        "run": idx,
        "mode": "barge" if barge else "clean",
        "state": m.get("state"),
        "reply_frames": m.get("reply_frames"),
        "speech_reply_frames": m.get("speech_reply_frames"),
        "first_reply_ms": m.get("first_reply_ms"),
        "barge_in_attempted": m.get("barge_in_attempted"),
        "barge_in_stop_ms": m.get("barge_in_stop_ms"),
        "model_seconds": model_seconds(),
        "age_drop": len(age_drop_lines()),
        # 桥侧权威口径原文（同进程 monotonic，按 reply_id）——手机侧仅为交叉校验
        "barge_stop": barge_stop_lines(),
    }
    bm = json.loads((D / "e2e-summary.json").read_text(encoding="utf-8")).get("bridge_metrics") or {}
    rec["queue_drops"] = bm.get("queue_drops")
    rec["queue_drops_up"] = bm.get("queue_drops_up")
    rec["queue_drops_down"] = bm.get("queue_drops_down")
    rec["up_gated_playback"] = bm.get("up_gated_playback")
    rec["backpressure_events"] = bm.get("backpressure_events")
    # 队列无关的另一条丢弃通路：播放期上行门控。不单列就会与"队列丢帧"混为一谈。
    rec["age_drop_lines"] = [l.split("] ")[-1] for l in age_drop_lines()]

    if not barge:
        text = reply_text()
        rec["text_chars"] = len(text)
        if len(text) >= 4 and "\ufffd" not in text:
            reply_wav = D / f"snap-reply-run{idx}.wav"
            reply_wav.write_bytes((D / "sim-reply.wav").read_bytes())
            # ⚠️ 参照 wav 必须**按文本内容**命名：`ensure_prompt_wav` 是幂等的（文件已存在即复用），
            # 若按轮次编号命名，两次运行 idx 都从 1 开始 ⇒ 第二轮会拿**上一批的、别的句子**的音频
            # 当分母，算出假的语速比（2026-09-13 实测踩到：tts_speech_s 与上一批逐位相同）。
            tag = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
            tts = sim_phone.ensure_prompt_wav(D / f"tts-ref-{tag}.wav", text=text)
            rt, rs = sim_phone.measure_speech_seconds(reply_wav)
            tt, ts = sim_phone.measure_speech_seconds(tts)
            rec |= {"reply_total_s": round(rt, 2), "reply_speech_s": round(rs, 2),
                    "tts_total_s": round(tt, 2), "tts_speech_s": round(ts, 2),
                    "ratio_speech": round(rs / ts, 3) if ts else None,
                    "ratio_total": round(rt / tt, 3) if tt else None}
        else:
            rec["text_guard"] = "脏文本，拒绝合成"
    return rec


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rounds = int(args[0]) if args else 2
    barge = "--barge" in sys.argv
    out: list[dict] = []
    for i in range(1, rounds + 1):
        try:
            out.append(run_once(i, barge=barge))
        except Exception as exc:  # noqa: BLE001
            out.append({"run": i, "error": f"{type(exc).__name__}: {exc}"})
    (D / f"repeat-{'barge' if barge else 'clean'}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n================ 汇总 ================")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
