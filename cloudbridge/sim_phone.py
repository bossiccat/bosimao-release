"""云端手机模拟：不依赖真机，用真实 TRTC 链路跑一次完整语音往返并量化结果。

为什么要有（用户明确要求）
--------------------------
「没做模拟验证就要人做真机测试，顺序是反的」。真机受制于 ADB 通道与设备占用，
而这条链路的绝大部分环节（控制面签发 → TRTC 会合 → sidecar 对端 → 模型 → 下行回传）
**都可以在云端用真实 SDK 自动跑通并测量**：`sidecar/phone.js` 就是一个手机模拟器
（`--role=phone`），它会签发会话、进同一个 TRTC 房间、按 20ms 节拍推一段 wav 上行、
接收回复音频并统计首包延迟与帧数。

本模块只做两件事，都是可单测的纯函数式逻辑：
1. `ensure_prompt_wav`：在容器内生成一段**真实中文语音**提示（edge-tts → ffmpeg 转 16k
   mono s16 wav）。提示内容固定，保证每次模拟可比。
2. `parse_phone_log`：从手机模拟器的输出里解析出可量化指标，供状态端点回报。

指标口径（必须写清，否则数字会被误读）
--------------------------------------
- `utterance_ms`：上行帧数 × 20ms —— **就是被推上去的 wav 时长本身**。
  phone.js:114 的 `stats.upFrames += 1` 只在 wav 分帧循环里；:123-129 后补的
  2s 尾部静音**一帧都不计**。实测自洽：wav=99840B ⇒ 3.12s，日志同时报
  `wav 推完（156帧）` 与 `上行 156帧`，156 × 20ms = 3120ms 恰等于 wav 时长；
- `speech_ms`：与 `utterance_ms` 同值 —— 已计入的帧全是 wav 帧，没有静音帧可减
  （旧实现减掉一个固定 2000ms，把 3.12s 报成 1.12s）；
- `first_reply_ms`：手机模拟器自报的「自上行开始到首个回复帧」；
- `reply_after_speech_ms`：`first_reply_ms - utterance_ms` —— **这才是与「用户说完到听见」
  可比的量**（用户说完 == wav 推完 == utterance_ms），商业化目标区间见 docs/audits 里的
  TTFB 口径（p95 < 700ms）。
"""
from __future__ import annotations

import math
import re
import subprocess
import wave
from dataclasses import dataclass, field
from pathlib import Path

FRAME_MS = 20

# 有效能量门限：**必须**与 sidecar/phone.js:272 的 SPEECH_RMS 一致，否则两端口径不同。
SPEECH_RMS = 60.0
_SPEECH_FRAME_SAMPLES = 320  # 20ms @16k mono s16

DEFAULT_PROMPT_TEXT = "你好，请用一句话介绍你自己。"
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

_FIRST_REPLY = re.compile(r"首包回复 @(\d+)ms")
_FRAMES = re.compile(r"上行 (\d+)帧 / 回复 (\d+)帧")
_SAVED = re.compile(r"回复已保存: (\S+)（(\d+)B）")
_SPEECH = re.compile(r"有效语音 (\d+)帧/(\d+)B")
_BARGE_IN_START = re.compile(r"打断上行开始")
_BARGE_IN_STOP = re.compile(r"打断停止\(旧回复结束\) @(?:(\d+)ms|n/a)")
_NO_REPLY = re.compile(r"hold=(\d+)s 内未收到回复")
_ENTER = re.compile(r"进房成功 (\d+)ms")
_ENTER_FAIL = re.compile(r"进房失败 (-?\d+)")
_SIGN_FAIL = re.compile(r"PHONE_SESSION_SIGN_FAILED")
# `[PHONE] ` 这种带方括号的前缀是**真实落盘形态**：sidecar/logger.js:19 统一拼成
# `[ISO时间] [scope] msg`。裸 `PHONE ` 只出现在历史日志与部分合成夹具里，两种都要认，
# 否则解析恒为 None（实测 e2e-summary.json 的 remote_ready_ms 就是这样变 null 的）。
_PHONE_TAG = r"\[?PHONE\]?"
# 带业务码的签发失败：phone.js 在 /session 被拒时打印服务端错误码（可能为负）。
_SIGN_FAIL_CODE = re.compile(rf"{_PHONE_TAG} 签发失败 code=(-?\d+)")
_RUNTIME_FATAL = re.compile(r"PHONE_RUNTIME_FATAL")
_WAV_PUSHED = re.compile(r"wav=(\S+) (\d+)B")
# 模拟器等待对端（sidecar）进房的握手：就绪则记录耗时，超时则继续上行并留一条 note。
_REMOTE_READY = re.compile(rf"{_PHONE_TAG} 远端就绪 @(\d+)ms")
_REMOTE_TIMEOUT = re.compile(rf"{_PHONE_TAG} 远端未就绪（(\d+)ms 超时，继续上行）")


@dataclass
class PhoneSimMetrics:
    """手机模拟器的可量化产出。state 取值：pending/joined/no_reply/replied/failed。"""

    state: str = "pending"
    first_reply_ms: int | None = None
    up_frames: int = 0
    reply_frames: int = 0
    reply_bytes: int = 0
    speech_reply_frames: int = 0
    speech_reply_bytes: int = 0
    # 打断质量：插话 → 模型不再出声的延迟（三项体验里此前 0 数据的一项）
    # ⚠️ 交叉校验口径，**非权威**。权威口径在桥侧：rtc_bridge 日志
    #    `[lat] barge_in stop old_reply=<id> frames=<n> stop_ms=<int>`（同一进程
    #    time.monotonic() 相减 + reply_id 锚定旧回复末帧）。本字段来自手机模拟器
    #    onPlayAudioFrame 的能量静音判定：拿不到 reply_id，会把新回复算成「还在说」
    #    （高估），也会被句间停顿误判（可能 None）。
    barge_in_attempted: bool = False
    barge_in_stop_ms: int | None = None
    reply_path: str = ""
    enter_room_ms: int | None = None
    # 等待对端（sidecar）进房握手的耗时；未就绪时为 None（超时会记一条 note）。
    remote_ready_ms: int | None = None
    failure: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def utterance_ms(self) -> int:
        """上行帧数 × 20ms —— 就是被推上去的 wav 时长，**不含** phone.js 补的 2s 尾部静音。

        phone.js:114 的 `stats.upFrames += 1` 只在 wav 分帧循环里；:123-129 的 2s 静音
        循环只发帧、不计数。156 帧 × 20ms = 3120ms 恰好等于该轮 wav 的 99840B ÷ 2 ÷ 16000。
        """
        return self.up_frames * FRAME_MS

    @property
    def speech_ms(self) -> int:
        """与 `utterance_ms` 同值 —— 已计入的帧全是 wav 帧，没有静音帧可减。

        ⚠️ 口径修正（2026-09-16，真实产物三方互证）：此前这里写
        `max(0, utterance_ms - TAIL_SILENCE_MS)`，前提是「upFrames 含 phone.js 补的
        2s 尾部静音」—— **该前提是错的**。实测 outputs/deploy-backup-20260911：
        `sidecar-phone.log` 报 `wav 推完（156帧），补 2s 静音` 与 `上行 156帧`，
        wav 自身 99840B ⇒ 3.12s = 156×20ms，而 e2e-summary.json 里
        `utterance_ms=3120 / speech_ms=1120` —— 静音一帧未计，这个减法把 3.12s 报成 1.12s。
        """
        return self.utterance_ms

    @property
    def reply_after_speech_ms(self) -> int | None:
        """**用户说完 → 手机听到第一帧回复**（与商业 TTFB 口径可比）。

        口径：`first_reply_ms` 自**上行第一帧**起计（phone.js:108 的 `upStartTs`），
        而用户说完的那一刻就是 wav 推完那一刻，即 `utterance_ms`。故这里减
        `utterance_ms` 得到的就是「说完 → 听见」。实测 4577 - 3120 = 1457ms。

        ⚠️ 旧注释把理由写成「必须减 utterance_ms 而不是 speech_ms，因为 utterance 含
        刻意补的 2s 尾部静音把 2 秒算成模型延迟」—— 那个理由建立在**错误前提**上
        （静音从未被计入 utterance_ms）。结论（减 utterance_ms）恰好仍然正确，
        因为 utterance_ms 现在正是「用户说完」的时刻，而 speech_ms 与它同值。
        """
        if self.first_reply_ms is None:
            return None
        return max(0, self.first_reply_ms - self.utterance_ms)

    def to_dict(self) -> dict:
        payload = {
            "state": self.state,
            "first_reply_ms": self.first_reply_ms,
            "reply_after_speech_ms": self.reply_after_speech_ms,
            "up_frames": self.up_frames,
            "utterance_ms": self.utterance_ms,
            "speech_ms": self.speech_ms,
            "reply_frames": self.reply_frames,
            "reply_bytes": self.reply_bytes,
            "speech_reply_frames": self.speech_reply_frames,
            "speech_reply_bytes": self.speech_reply_bytes,
            "barge_in_attempted": self.barge_in_attempted,
            "barge_in_stop_ms": self.barge_in_stop_ms,
            "enter_room_ms": self.enter_room_ms,
            "reply_path": self.reply_path,
            # 字段稳定：即便未就绪也输出 None，便于下游固定解析而不必判 key 存在。
            "remote_ready_ms": self.remote_ready_ms,
            "ok": self.state == "replied" and self.reply_bytes > 0,
        }
        if self.failure:
            payload["failure"] = self.failure
        if self.notes:
            payload["notes"] = self.notes
        return payload


def parse_phone_log(lines: list[str]) -> PhoneSimMetrics:
    """从手机模拟器输出解析指标。只依赖它自己打印的稳定文案。"""
    metrics = PhoneSimMetrics()
    for line in lines:
        if _SIGN_FAIL.search(line):
            metrics.state = "failed"
            metrics.failure = "PHONE_SESSION_SIGN_FAILED"
        m = _SIGN_FAIL_CODE.search(line)
        if m:
            # 保留服务端错误码，才能从状态端点区分"被 privacy 门禁拒"与"被限流/凭证拒"。
            metrics.state = "failed"
            metrics.failure = f"PHONE_SESSION_SIGN_FAILED:{m.group(1)}"
        if _RUNTIME_FATAL.search(line):
            metrics.state = "failed"
            metrics.failure = "PHONE_RUNTIME_FATAL"
        m = _ENTER.search(line)
        if m:
            metrics.state = "joined"
            metrics.enter_room_ms = int(m.group(1))
        m = _ENTER_FAIL.search(line)
        if m:
            metrics.state = "failed"
            metrics.failure = f"enter_room_{m.group(1)}"
        m = _REMOTE_READY.search(line)
        if m:
            metrics.remote_ready_ms = int(m.group(1))
        if _REMOTE_TIMEOUT.search(line):
            metrics.notes.append("remote_not_ready")
        m = _WAV_PUSHED.search(line)
        if m:
            metrics.notes.append(f"prompt={m.group(1)} ({m.group(2)}B)")
        m = _FIRST_REPLY.search(line)
        if m:
            metrics.first_reply_ms = int(m.group(1))
        m = _FRAMES.search(line)
        if m:
            metrics.up_frames = int(m.group(1))
            metrics.reply_frames = int(m.group(2))
            if metrics.reply_frames > 0:
                metrics.state = "replied"
        m = _SPEECH.search(line)
        if m:
            metrics.speech_reply_frames = int(m.group(1))
            metrics.speech_reply_bytes = int(m.group(2))
        if _BARGE_IN_START.search(line):
            metrics.barge_in_attempted = True
        m = _BARGE_IN_STOP.search(line)
        if m:
            metrics.barge_in_attempted = True
            metrics.barge_in_stop_ms = int(m.group(1)) if m.group(1) else None
        m = _SAVED.search(line)
        if m:
            metrics.reply_path = m.group(1)
            metrics.reply_bytes = int(m.group(2))
        if _NO_REPLY.search(line) and metrics.reply_frames == 0:
            metrics.state = "no_reply"
    return metrics


def measure_speech_seconds(path: str | Path) -> tuple[float, float]:
    """量一段 16k mono s16 wav 的 (总时长, 有效语音时长)，口径与手机侧**完全一致**。

    为什么必须同口径（2026-09-13 测量脚手架污染实锤）
    -------------------------------------------------
    旧 `measure-reply-rate.py` 拿「去静音的语音帧数 × 20ms」当分子、拿 edge-tts
    **wav 总时长**当分母。实测 edge-tts 自身 43% 是静音帧（1.92s 总长里只有 1.10s
    语音）⇒ 即使模型语速完全正常，ratio 也会算出 ≈0.57，脚本必然误判「模型说得快」。
    要让「模型产出短」与「我们的管道丢音频」可分，必须先把两边放到**同一口径**。

    规则**逐行对齐** `sidecar/phone.js:186-197`（`SPEECH_RMS` = phone.js:272）：
      · 16k mono s16（其它格式直接报错，不做静默重采样——口径不同就不该出数）；
      · 每 20ms（320 样本 = 640 字节）一帧，末帧不足按实际样本数算；
      · 帧内每 4 个样本取 1 求 RMS：`acc=Σv²`，`rms=sqrt(acc/ceil(n/4))`；
      · 帧 RMS ≥ 60 记一个语音帧。
    返回 `帧数 × 20ms`（保留 20ms 量化，确保与手机侧 `speech_reply_frames×20ms`
    同口径）。返回 `(总时长秒, 语音时长秒)`。
    """
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != 16000 or w.getsampwidth() != 2:
            raise ValueError(
                "measure_speech_seconds 只接受 16k mono s16 wav（口径须与 sidecar/phone.js 一致），"
                f"实得 ch={w.getnchannels()} rate={w.getframerate()} width={w.getsampwidth()}"
            )
        pcm = w.readframes(w.getnframes())

    frame_bytes = _SPEECH_FRAME_SAMPLES * 2
    total_frames = 0
    speech_frames = 0
    for off in range(0, len(pcm), frame_bytes):
        frame = pcm[off:off + frame_bytes]
        n = len(frame) >> 1
        if n == 0:
            continue
        total_frames += 1
        acc = 0
        for i in range(0, n, 4):
            v = int.from_bytes(frame[i * 2:i * 2 + 2], "little", signed=True)
            acc += v * v
        rms = math.sqrt(acc / max(1, (n + 3) // 4))  # ceil(n/4)，与 phone.js 同
        if rms >= SPEECH_RMS:
            speech_frames += 1
    return total_frames * FRAME_MS / 1000.0, speech_frames * FRAME_MS / 1000.0


def ensure_prompt_wav(
    out_path: Path,
    *,
    text: str = DEFAULT_PROMPT_TEXT,
    voice: str = DEFAULT_VOICE,
    timeout_s: float = 90.0,
) -> Path:
    """在容器内生成 16k mono s16 PCM 提示音（edge-tts 合成 → ffmpeg 转码）。

    生成真实中文语音而不是音调：模型只会对语音产生有意义的回复，用音调测不出链路真伪。
    已存在则直接复用（幂等），避免每次重启都联网合成。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.is_file() and out_path.stat().st_size > 44:  # 比 wav 头大即视为可用
        return out_path

    mp3_path = out_path.with_suffix(".mp3")
    subprocess.run(  # noqa: S603 - 固定 argv，无外部输入
        ["edge-tts", "--voice", voice, "--text", text, "--write-media", str(mp3_path)],
        check=True, capture_output=True, timeout=timeout_s,
    )
    subprocess.run(  # noqa: S603
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3_path),
            "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", str(out_path),
        ],
        check=True, capture_output=True, timeout=timeout_s,
    )
    if not out_path.is_file() or out_path.stat().st_size <= 44:
        raise RuntimeError("prompt wav was not produced")
    return out_path
