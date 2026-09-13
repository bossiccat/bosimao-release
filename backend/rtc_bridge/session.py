"""PeerVoiceSession —— 1 房间 = 1 sidecar WS 连接 + 1 ApmBridge（PC-INTEGRATION §4.3）

职责：
- 上行：sidecar 收的手机音频（16k s16）→ EndDetectFeeder（停顿补静音）→ ApmBridge.feed_pcm
- 下行：ApmBridge.on_audio_out（16k s16）→ DownlinkShaper（拆帧 + 节拍）→ WS 下发 sidecar
- 生命周期：远端进入 → 重置说完判定/整形器；远端离开 → 释放 APM 会话（懒初始化保持）
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Any, Awaitable, Callable

from app.brain.voice_intent_router import VoiceIntentRouter
from app.voice.apm_bridge import ApmBridge
from app.voice.qwen_realtime_bridge import QwenRealtimeBridge
from app.voice.end_detect import EndDetectFeeder, pcm_rms

from .bounded_audio_queue import BoundedAudioQueue
from .frame_meta import DownFrame
from .pcm_dump import PcmDumpSink
from .shaper import DownlinkShaper

logger = logging.getLogger(__name__)

# WS 消息类型（与 sidecar/bridge.js 对齐）
MSG_UP_AUDIO = "up_audio"
MSG_DOWN_AUDIO = "down_audio"
MSG_PEER_STATE = "peer_state"
MSG_CTRL = "ctrl"

# GPT-Live 式待命/唤醒文本标记（system prompt 约定 AI 在回复末尾追加）
STANDBY_MARKER = "[STANDBY]"
ACTIVE_MARKER = "[ACTIVE]"

# 默认上行预算（AC-10；压力测试后可调）
DEFAULT_UP_MAX_FRAMES = 100
DEFAULT_UP_MAX_BYTES = 100 * 640
DEFAULT_UP_MAX_FRAME_AGE_MS = 1000

# 默认下行预算：必须按「能装下一整段回复」配置（模型突发式下发 + 实时 50 帧/s 出队，
# 早到的帧是待播内容而非陈旧数据）。1500 帧 × 20ms = 30s。
# ⚠️ 必须与 config.py 的 down_* 默认值保持一致（契约测试守护，防两处再次漂移）。
DEFAULT_DOWN_MAX_FRAMES = 1500
DEFAULT_DOWN_MAX_BYTES = 1500 * 640
DEFAULT_DOWN_MAX_FRAME_AGE_MS = 30000

# P0-2（internal-latency-budget §1.1 U6）：说完判定补静音 pad 按引擎区分——
# qwen smart_turn 自带说完判定，2s pad 冗余且让 pad 后开口的用户先被云端
# 消化 2s 静音（speech_started 推迟最多 ~2s）→ 缩到 400ms；apm 无云端
# 说完判定，保持 2s
QWEN_END_PAD_S = 0.4
APM_END_PAD_S = 2.0

# F6/F7：下行静默超过该时长后到来的音频判定为「新一轮回复」，分配新 reply_id。
# 取值需大于 shaper 单帧节拍（20ms）与常规抖动，小于两轮回复的自然间隔。
DEFAULT_NEW_REPLY_GAP_S = 0.5


class PeerVoiceSession:
    """单设备语音会话（手机 ↔ sidecar ↔ rtc_bridge ↔ apm_bridge ↔ MiniCPM-o）"""

    def __init__(
        self,
        device_id: str,
        room_id: str,
        send_msg: Callable[[dict], Awaitable[None]],
        apm_api_url: str,
        apm_system_prompt: str,
        apm_token: str = "",
        voice_engine: str = "apm",
        qwen_api_url: str = "",
        qwen_token: str = "",
        qwen_system_prompt: str = "",
        on_agent_tool: Callable[[str, dict, str], Awaitable[str]] | None = None,
        down_frame_ms: int = 20,
        sample_rate: int = 16000,
        *,
        up_max_frames: int = DEFAULT_UP_MAX_FRAMES,
        up_max_bytes: int = DEFAULT_UP_MAX_BYTES,
        up_max_frame_age_ms: int = DEFAULT_UP_MAX_FRAME_AGE_MS,
        down_max_frames: int = DEFAULT_DOWN_MAX_FRAMES,
        down_max_bytes: int = DEFAULT_DOWN_MAX_BYTES,
        down_max_frame_age_ms: int = DEFAULT_DOWN_MAX_FRAME_AGE_MS,
        on_voice_intent: Callable[[str], Awaitable[None]] | None = None,
        on_apm_cancelled: Callable[[bool], Awaitable[None]] | None = None,
        session_id: str = "",
        new_reply_gap_s: float = DEFAULT_NEW_REPLY_GAP_S,
        barge_grace_s: float = 0.5,
        barge_sustain_frames: int = 3,
        local_barge_in: bool = True,
    ) -> None:
        self.device_id = device_id
        self.room_id = room_id
        # F6/F7：hello 里的 session_id 注入后用于铸造跨进程 reply_id
        self._session_id = session_id
        self._new_reply_gap_s = new_reply_gap_s
        # 决策归位（docs/plans/2026-09-07 M2）：local_barge_in=False 时退役本地
        # 能量 barge-in 与播放期上行门控——打断判定交给云端 smart_turn（声学+语义
        # 双重检测，非语义声音不触发），服务端 speech_started 事件驱使下行冲刷。
        # kill-switch：RTC_BRIDGE_LOCAL_BARGE_IN=false 切换，默认 true 保持现状。
        self._local_barge_in = local_barge_in
        self._send_msg = send_msg
        # APM 会话被取消关闭后的回调（apm_cancelled_closed 上报钩子；可空）
        self._on_apm_cancelled = on_apm_cancelled
        self._apm_api_url = apm_api_url
        self._apm_system_prompt = apm_system_prompt
        self._apm_token = apm_token
        self._voice_engine = voice_engine
        self._qwen_api_url = qwen_api_url
        self._qwen_token = qwen_token
        self._qwen_system_prompt = qwen_system_prompt
        self._on_agent_tool = on_agent_tool
        self._apm_rebuilds = 0
        # P0-2：说完判定 pad 按引擎区分（qwen=400ms / apm=2s）
        self._pad_s = QWEN_END_PAD_S if self._voice_engine == "qwen" else APM_END_PAD_S
        self._build_apm()
        self.feeder = EndDetectFeeder(feed=self.apm.feed_pcm, sample_rate=sample_rate,
                                      pad_s=self._pad_s)
        self.shaper = DownlinkShaper(
            send_frame=self._send_frame,
            frame_ms=down_frame_ms,
            sample_rate=sample_rate,
            max_frames=down_max_frames,
            max_bytes=down_max_bytes,
            max_frame_age_ms=down_max_frame_age_ms,
        )
        # 上行：有界队列（AC-10：帧数/字节/帧龄三约束，丢旧保新）
        self._up_q = BoundedAudioQueue(
            max_frames=up_max_frames,
            max_bytes=up_max_bytes,
            max_frame_age_ms=up_max_frame_age_ms,
        )
        self._up_wake = asyncio.Event()
        self._consumer: asyncio.Task | None = None
        self._peer_entered = False
        self._peer_user_id = ""
        self._closed = False
        self._started = False
        # 服务端 barge-in（v0.6.7 P0）：用户开口打断 AI 播报
        # 2026-09-07 防误杀加固：真机实锤起播 21ms 即被回声瞬态开窗掐死回复
        # （logs/rtc_bridge_app.log 15:00:41.931→41.952），加宽限期 + 持续语音确认
        self._barge_in = False          # 打断窗口标志（用户说话中）
        self._last_down_ts = 0.0        # 最近一次下行音频时间（AI 说话中判定）
        self._down_speaking = False     # AI 播报中（down 持续流动）
        self._down_speaking_since = 0.0  # 本轮播报起始时刻（宽限期判定基准）
        self._barge_run = 0             # 连续高能量帧计数（持续语音确认）
        self._barge_grace_s = barge_grace_s
        self._barge_sustain_frames = max(1, barge_sustain_frames)
        self._barge_drops = 0           # 打断丢弃的下行帧计数
        self._last_down_check = 0.0     # 上行 RMS 检查限流
        self._last_up_rms_log = 0.0     # 上行 RMS 调试日志限流（2s 一次，排查"千问无响应"）
        self._last_feed_fail_log = 0.0  # 喂帧失败日志限流（≥2s 一条，防断链时刷爆日志）
        # F6/F7 逐帧关联状态
        self._reply_id = ""         # 当前 reply 身份（空=尚未开始）
        self._reply_seq = 0         # reply 计数器（会话内单调递增）
        self._chunk_seq = 0         # 当前 reply 内的云端 chunk 序号
        self._last_down_mono = 0.0  # 上一帧下行的 monotonic 时刻（reply 边界判定）
        # GPT-Live 式待命/唤醒状态机
        # _standby=True 时禁止模型输出（下行音频/文本均丢弃）；上行仍由当前 RTC 会话接收。
        # 注意：真正的唤醒门禁仍需手机侧 KWS/重进房；后端不能把云端模型当 KWS。
        # _standby_pending=True 时等待当前 AI 回复播完再进入 standby（让用户听到 "退下" 确认语）
        self._standby = False           # 待命态：AI 静默，下行丢弃
        self._standby_pending = False   # 待命待激活：AI 回复含 [STANDBY] 标记，播完后切 standby
        # 标记尾音丢弃窗口：_on_text 检测到 [STANDBY]/[ACTIVE] 后，模型会把标记
        # 文本本身也合成成音频（真机实锤 2026-08-26：用户听到结尾念英文）。
        # 文本侧 strip 只保护 Brain 路由；音频侧靠本窗口把标记之后的 TTS 尾音全部丢弃，
        # 直到播报静默结束（_check_down_speaking_over）才关窗，不误伤下一轮回复。
        self._marker_tail_drop = False  # 标记尾音丢弃中：下行音频丢弃
        # 语音意图路由器：累积 AI 文本 delta，AI 说完后 flush 到 Brain（解决"伪智能体"）
        self._router: VoiceIntentRouter | None = None
        if on_voice_intent is not None:
            self._router = VoiceIntentRouter(on_route=on_voice_intent)
        # 指标
        self.stats = {
            "up_frames": 0,
            "up_bytes": 0,
            "down_frames": 0,
            "down_bytes": 0,
            "last_peer_ts": 0.0,
            "apm_session_state": "idle",
            "reconnects": 0,
            "up_queue_depth": 0,
            "down_queue_depth": 0,
            "queue_high_watermark": 0,
            "queue_drops": 0,
            "backpressure_events": 0,
            "standby": False,           # GPT-Live 待命态（True=AI 静默）
            "standby_drops": 0,         # standby 期间丢弃的下行帧计数
            "marker_tail_drops": 0,     # 标记尾音丢弃计数（[STANDBY]/[ACTIVE] 尾音）
            "up_gated_playback": 0,     # 播放期上行门控丢弃计数（回声不喂云端）
        }
        self.last_activity_ts = time.time()
        # P0 取证（2026-09-06）：JAX_DOWN_PCM_DUMP=<prefix> 开启时把下行/上行
        # PCM 原样落盘，人耳复核最后一跳；默认关闭零开销
        dump_prefix = os.environ.get("JAX_DOWN_PCM_DUMP", "").strip()
        self._pcm_dump = PcmDumpSink(dump_prefix) if dump_prefix else None
        if self._pcm_dump is not None:
            logger.info("[lat] pcm dump enabled prefix=%s", dump_prefix)

    def _build_apm(self) -> ApmBridge:
        """创建 ApmBridge 并绑回调；feed_pcm 由 feeder 持有（重建时重绑）"""
        if self._voice_engine == "qwen":
            self.apm = QwenRealtimeBridge(
                on_audio_out=self._on_audio_out,
                on_text=self._on_text,
                on_tool_call=self._on_agent_tool,
                api_url=self._qwen_api_url,
                token=self._qwen_token,
                system_prompt=self._qwen_system_prompt,
                on_error=self._on_qwen_error,
                on_user_speech=self._on_server_user_speech,
            )
        else:
            self.apm = ApmBridge(
                on_audio_out=self._on_audio_out,
                on_text=self._on_text,
                on_state=self._on_state,
                on_error=self._on_apm_error,
                api_url=self._apm_api_url,
                system_prompt=self._apm_system_prompt,
                token=self._apm_token,
            )
        return self.apm

    async def start(self) -> None:
        """启动上行消费协程 + 下行整形器（不进房，APM 保持懒初始化）"""
        if self._started:
            return
        self._started = True
        self.shaper.start()
        self._consumer = asyncio.create_task(self._consume_up())
        logger.info("rtc session started device=%s room=%s", self.device_id, self.room_id)

    # ---------- 上行 ----------
    async def on_up_audio(self, pcm: bytes) -> None:
        """sidecar 推来的手机 16k s16 → 有界入队（不阻塞 WS 回调）"""
        if self._closed:
            return
        self.last_activity_ts = time.time()
        self.stats["up_frames"] += 1
        self.stats["up_bytes"] += len(pcm)
        # 服务端 barge-in：AI 播报中用户开口（高能量帧）→ 清空下行队列 + 打断窗口
        # 2026-09-07 加固（真机实锤 21ms 误杀）：
        # ① 宽限期：起播后 _barge_grace_s 内不开窗——AEC 残差回声瞬态集中在起播瞬间；
        # ② 持续语音：连续 _barge_sustain_frames 帧高能量才开窗——单帧尖峰（咔哒/截断
        #    噪声）不触发；安静帧打断序列即归零。
        # 真实打断代价：反应延迟增加 (sustain-1)×20ms ≈ 40ms，远小于行业 300ms 标准。
        now = time.time()
        # 本地能量 barge-in（默认路径）。M2 kill-switch（RTC_BRIDGE_LOCAL_BARGE_IN=false）
        # 关闭后整块退役：打断判定归位云端 smart_turn（声学+语义），本地不再用被
        # 扬声器回声污染的信号做决策——这是 run1 实锤「回复 0.5-1.5s 被误杀」的根治。
        if self._local_barge_in:
            if (self._down_speaking and not self._barge_in
                    and pcm_rms(pcm) > 800.0):
                if now - self._down_speaking_since >= self._barge_grace_s:
                    self._barge_run += 1
                    if self._barge_run >= self._barge_sustain_frames:
                        self._barge_in = True
                        self._barge_run = 0
                        self._apm_barge_drops_reset()
                        self.shaper.reset()   # 清空未推送的下行帧（正在播的 20ms 帧自然播完）
                        # 打断冲刷必须**下沉到 sidecar**：清 rtc_bridge 队列还不够 ——
                        # sidecar 的 DownlinkPacer 还会积压最多 50 帧(1s) 待播音频。
                        # 不下发这条指令，用户插话后旧回复会继续播完那 1 秒
                        # （实测打断延迟 1.75s 的主因，其中约 1s 由此而来）。
                        await self._send_msg({"type": MSG_CTRL, "action": "flush_downlink"})
                        # 同样要让**模型**停：实测云端不会自己取消（旧 response 继续下发 +3.18s）。
                        await self._cancel_model_response()
                        if self._router is not None:
                            self._router.clear()   # 丢弃被中断的 AI 文本（不路由到 Brain）
                        # P0-5/F4：丢弃窗口开窗打点（判定"下行丢弃窗口误开"假设 C）
                        logger.info("[lat] barge_in open mono=%.3f", now)
                        logger.info("barge-in: user speech during AI playback, downlink flushed")
                else:
                    self._barge_run = 0   # 宽限期内高能量=回声瞬态，不计入持续序列
            else:
                self._barge_run = 0
        if self._barge_in:
            self._last_down_check = now
        if now - self._last_up_rms_log >= 2.0:
            self._last_up_rms_log = now
            logger.info("up rms=%.0f frames=%d", pcm_rms(pcm), self.stats["up_frames"])
        self._up_q.push(pcm)
        self._sync_queue_metrics()
        self._up_wake.set()

    def _sync_standby_stats(self) -> None:
        """同步 standby 状态到 stats（供 health /metrics 读取）"""
        self.stats["standby"] = self._standby

    def _apm_barge_drops_reset(self) -> None:
        self._barge_drops = 0

    async def _cancel_model_response(self) -> None:
        """打断时**主动请求模型取消正在进行的 response**（fail-soft）。

        为什么必须显式做：本文件此前的注释假定「云端 smart_turn 会自己 response.cancel
        并停发音频」，但实测**否掉了该假设** —— 插话后旧 response 仍下发到自然结束
        （+3.18s），而两条打断路径都只清我们这一侧、从未告诉模型停下，导致打断延迟
        恒在 1.5–1.8s（与用本地能量还是云端 VAD 判定几乎无关）。

        引擎不支持该方法时静默跳过（例如 ApmBridge），不阻断打断路径。
        """
        cancel = getattr(self.apm, "cancel_response", None)
        if cancel is None:
            return
        try:
            ok = await cancel()
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel_model_response failed: %s", type(exc).__name__)
            return
        logger.info("[lat] response.cancel sent=%s（打断时主动停发）", ok)

    async def _on_server_user_speech(self) -> None:
        """云端 speech_started 事件（服务端 VAD 判定用户开口）。

        M2 决策归位路径：打断由云端 smart_turn 判定（声学+语义，非语义声音如
        回声瞬态/「嗯」「啊」不会触发），此处只负责执行——AI 播报中收到该事件
        即冲刷下行（被打断的回复不再继续推送）+ 清理路由器（被中断文本不进
        Brain）。这是可信赖的打断源：判定用的信号从未经过本地扬声器回声路径。
        """
        if self._down_speaking:
            self.stats["server_barge_in"] = self.stats.get("server_barge_in", 0) + 1
            self._apm_barge_drops_reset()
            self.shaper.reset()
            # 打断冲刷必须**下沉到 sidecar**，与本地能量路径（on_up_audio 的 barge-in
            # 分支）保持对称：清 rtc_bridge 队列还不够 —— sidecar 的 DownlinkPacer
            # 还会积压最多 50 帧(1s) 待播音频。此前云端 VAD 路径只做 shaper.reset()，
            # 于是云端判定打断时旧音频仍把这 1 秒播完（与实测打断延迟 ~1.15s 量级吻合）。
            await self._send_msg({"type": MSG_CTRL, "action": "flush_downlink"})
            # 清我们这侧还不够 —— 必须让模型也停（实测云端不会自己取消）。
            await self._cancel_model_response()
            # 复用本地窗口机制：打断后到播放实际停止之间的残留 delta/文本
            # 走既有丢弃路径（_on_audio_out / _on_text 的 _barge_in 检查），
            # _check_down_speaking_over 会在下行静默后自动关窗。
            self._barge_in = True
            self._barge_run = 0
            if self._router is not None:
                self._router.clear()
            logger.info(
                "[lat] server_barge_in mono=%.3f (cloud smart_turn detected user speech)",
                time.monotonic(),
            )
            logger.info("server barge-in: cloud smart_turn detected user speech, downlink flushed")

    async def _consume_up(self) -> None:
        while not self._closed:
            await self._check_down_speaking_over()
            entry = self._up_q.pop()
            if entry is None:
                self._up_wake.clear()
                try:
                    await asyncio.wait_for(self._up_wake.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass  # 周期唤醒：维持 barge-in 窗口超时判定
                continue
            self._sync_queue_metrics()
            # 播放期上行门控（2026-09-07）：AI 播报中不喂云端——扬声器回声会
            # 被 smart_turn commit 成用户输入，产生 ttfb=0-16ms 垃圾 response。
            # 真实打断由本地能量 barge-in 检测（宽限期+持续确认），开窗后本门
            # 立即放开，用户语音即刻可达云端。
            # M2 kill-switch：local_barge_in=False 时本门退役——上行恒流喂云端，
            # 打断由云端 smart_turn 语义判定（它自己会 response.cancel 并停发音频）。
            if (self._local_barge_in and self._down_speaking
                    and not self._barge_in):
                self.stats["up_gated_playback"] = self.stats.get("up_gated_playback", 0) + 1
                continue
            if self._pcm_dump is not None:
                # P0 取证：上行原始帧（实际送上云端；队列丢弃另有计数）落盘
                self._pcm_dump.write_up(entry.payload)
            try:
                await self.feeder.feed(entry.payload)
            except Exception as e:  # noqa: BLE001
                # P0（2026-09-06 真机实锤）：云端断链时该日志每帧一条刷爆
                # （单日 81302 条）——节流到 ≥2s 一条
                now = time.time()
                if now - self._last_feed_fail_log >= 2.0:
                    self._last_feed_fail_log = now
                    logger.warning("feed apm failed: %s", e)

    def _sync_queue_metrics(self) -> None:
        up = self._up_q.metrics()
        down = self.shaper.metrics()
        self.stats["up_queue_depth"] = up["queue_depth"]
        self.stats["down_queue_depth"] = down["queue_depth"]
        self.stats["queue_high_watermark"] = max(up["queue_high_watermark"],
                                                 down["queue_high_watermark"])
        self.stats["queue_drops"] = up["queue_drops"] + down["queue_drops"]
        self.stats["backpressure_events"] = up["backpressure_events"] + down["backpressure_events"]

    # ---------- 下行 ----------
    async def _on_audio_out(self, pcm: bytes) -> None:
        """ApmBridge 下行回调 → 整形器

        Standby 态：AI 音频全部丢弃（GPT-Live "退下"语义——AI 静默）。
        Barge-in 窗口：AI 旧回复的后续帧丢弃（模型已被用户新语音打断）。
        """
        if self._closed:
            return
        if self._standby:
            self.stats["standby_drops"] += 1
            return  # Standby：AI 不发声，丢弃所有下行音频
        # 标记尾音丢弃窗口：[STANDBY]/[ACTIVE] 标记文本被模型念出的尾音，全部丢弃。
        # 注意：被丢弃的尾音仍维持 _down_speaking 活跃（刷新 _last_down_ts）——
        # 否则若标记文本先于音频到达、全部尾音被丢弃，_down_speaking 从未置 True，
        # 丢弃窗口永远不会被 _check_down_speaking_over 关闭，下一轮回复会被误杀。
        if self._marker_tail_drop:
            self.stats["marker_tail_drops"] += 1
            now = time.time()
            self._last_down_ts = now
            if not self._down_speaking:
                self._down_speaking_since = now
                self._barge_run = 0
            self._down_speaking = True
            return
        now = time.time()
        # barge-in 打断窗口：AI 旧回复的后续帧全部丢弃（模型已被用户新语音打断）
        if self._barge_in:
            self._barge_drops += 1
            self.stats["down_dropped_barge"] = self._barge_drops
            return
        self._last_down_ts = now
        if not self._down_speaking:
            self._down_speaking_since = now
            self._barge_run = 0
        self._down_speaking = True
        self.stats["down_frames"] += 1
        self.stats["down_bytes"] += len(pcm)
        self._mint_reply_if_needed()
        await self.shaper.push(pcm, src_seq=self._chunk_seq)
        self._chunk_seq += 1

    def _mint_reply_if_needed(self) -> None:
        """F6/F7：reply 边界判定与身份铸造

        reply 身份在 bridge 本地铸造，不依赖云端 response id——目的是给
        「bridge → sidecar → SDK → 手机」这条跨进程链路一个共享身份，
        云端侧另有 response.created 日志，可按时间 join。

        判定：首次下行，或距上一帧下行超过 new_reply_gap_s（说明中间静默过，
        属于新一轮回复）→ 分配新 reply_id，frame_seq 与 chunk 序号归零。
        """
        now_mono = time.monotonic()
        if (not self._reply_id
                or (now_mono - self._last_down_mono) > self._new_reply_gap_s):
            self._reply_seq += 1
            self._reply_id = f"{self._session_id or 'anon'}:{self._reply_seq}"
            self._chunk_seq = 0
            self.shaper.begin_reply(self._reply_id)
            logger.info("[lat] down reply begin reply=%s session=%s mono=%.3f",
                        self._reply_id, self._session_id or "-", now_mono)
        self._last_down_mono = now_mono

    async def _check_down_speaking_over(self) -> None:
        """AI 播报结束判定：下行静默 >600ms → 退出播报态，结束打断窗口

        正常完成（无 barge-in）：
          - 若 _standby_pending → 激活 standby（用户听到 "退下" 确认语后 AI 静默）
          - flush router → AI 完整文本路由到 Brain
        Barge-in 关窗：
          - 清空 router → 被中断的文本已清空
          - 取消 standby_pending（用户打断 = 撤销 "退下" 意图）
        """
        now = time.time()
        if self._down_speaking and now - self._last_down_ts > 0.6:
            self._down_speaking = False
            self._marker_tail_drop = False  # 播报结束：关闭标记尾音丢弃窗口
            # P0-5/F4：丢弃窗口关窗打点 + 各窗口丢帧计数（假设 C 判定）
            logger.info("[lat] discard window closed mono=%.3f barge_drops=%d "
                        "marker_tail_drops=%d standby_drops=%d",
                        time.monotonic(), self._barge_drops,
                        self.stats.get("marker_tail_drops", 0),
                        self.stats.get("standby_drops", 0))
            if self._barge_in:
                self._barge_in = False
                self._standby_pending = False   # 用户打断 = 撤销 "退下" 意图
                logger.info("barge-in window closed (AI playback stopped), dropped=%d", self._barge_drops)
                if self._router is not None:
                    self._router.clear()
            else:
                # AI 正常说完：检查 router 缓冲中是否有 split 标记（跨 delta 的情况）
                if self._router is not None:
                    buf = self._router.buffered_text
                    if STANDBY_MARKER in buf or ACTIVE_MARKER in buf:
                        self._apply_markers_from_buffer(buf)
                    if self._standby_pending:
                        self._standby = True
                        self._standby_pending = False
                        # P0-5/F4：standby 激活（下行全丢窗口开启）
                        logger.info("[lat] standby activated mono=%.3f", time.monotonic())
                        logger.info("standby activated after AI utterance completed")
                    # P0（2026-09-06 21:06 实锤）：flush 会走 brain API /intent
                    # （超时 5~8s），同步 await 会停摆 _consume_up 上行循环 →
                    # 用户语音 6~8s 不上行 + 下行音频积压被整形器砍断。
                    # 改 create_task：flush 绝不阻塞上行循环（buffer 先清后路由，
                    # asyncio 单线程，并发安全；barge-in 的 clear 分支保持同步）。
                    asyncio.create_task(self._router.flush())

    def _apply_markers_from_buffer(self, buf: str) -> None:
        """检查 router 累积文本中的标记（处理标记被跨 delta 拆分的情况），
        从 buffer 中清除标记后重新 feed。"""
        had_marker = False
        if STANDBY_MARKER in buf or ACTIVE_MARKER in buf:
            self._marker_tail_drop = True  # 跨 delta 拆分标记：同样打开尾音丢弃窗口
        if STANDBY_MARKER in buf:
            self._standby_pending = True
            had_marker = True
            logger.info("STANDBY marker found in router buffer (split across deltas)")
        if ACTIVE_MARKER in buf:
            self._standby = False
            self._standby_pending = False
            had_marker = True
            logger.info("ACTIVE marker found in router buffer (split across deltas)")
        if had_marker:
            stripped = buf.replace(STANDBY_MARKER, "").replace(ACTIVE_MARKER, "").strip()
            self._router.clear()
            if stripped:
                # 重新 feed 去标记后的文本（同步调用 feed，不 await——clear 后 buffer 为空）
                # 用 create_task 避免 _on_text 链式 await 死锁
                asyncio.create_task(self._router.feed(stripped))

    async def _send_frame(self, frame: "DownFrame") -> None:
        if self._pcm_dump is not None:
            # P0 取证：下行帧在 shaper 节拍后、实际发往 sidecar 前落盘——
            # dump 里的洞 = 真实送达的洞
            self._pcm_dump.write_down(frame.payload)
        msg = {
            "type": MSG_DOWN_AUDIO,
            "pcm_b64": base64.b64encode(frame.payload).decode("ascii"),
        }
        # F6/F7 追溯字段（附加字段，旧 sidecar 忽略未知 key 即可正常工作）
        msg.update(frame.trace_fields())
        await self._send_msg(msg)

    async def _on_text(self, text: str) -> None:
        """APM 文本 delta → 标记检测 + router 累积

        [STANDBY] → _standby_pending=True（播完当前回复后切 standby）
        [ACTIVE] → _standby=False（立即解除 standby，让后续 AI 音频正常下行）
        标记从文本中 strip，不进入 router / Brain。
        """
        logger.info("apm text: %s", text[:120])
        if self._barge_in:
            return  # Barge-in 中：丢弃 AI 文本（用户已打断，旧回复不路由到 Brain）
        # GPT-Live 标记检测（单个 delta 内匹配；跨 delta 由 flush 前 buffer 检查兜底）
        if STANDBY_MARKER in text or ACTIVE_MARKER in text:
            # 标记文本会被模型合成成音频（"退下了 [STANDBY]" 结尾念英文）——
            # 打开尾音丢弃窗口，标记之后的 TTS 音频全部丢弃，播报结束才关窗
            self._marker_tail_drop = True
            # P0-5/F4：marker_tail 丢弃窗口开窗打点
            logger.info("[lat] marker_tail_drop open mono=%.3f", time.monotonic())
        if STANDBY_MARKER in text:
            self._standby_pending = True
            logger.info("STANDBY marker detected, standby pending (after current utterance)")
        if ACTIVE_MARKER in text:
            self._standby = False
            self._standby_pending = False
            logger.info("ACTIVE marker detected, exiting standby mode")
        # 待命期间：仅允许 [ACTIVE] 唤醒门禁通过；其余模型输出全部丢弃，
        # 不进入下行、不进入 Brain，避免"退下后仍响应/仍触发任务"。
        if self._standby:
            return
        # strip 标记后 feed router（标记不进 Brain）
        text = text.replace(STANDBY_MARKER, "").replace(ACTIVE_MARKER, "")
        if self._router is not None and text:
            await self._router.feed(text)

    async def _on_qwen_error(self, message: str) -> None:
        """QwenRealtimeBridge 致命错误（重连放弃等）→ 结构化 WARNING + ctrl 上报

        手机端可感知"云端引擎断开"，不再静默（P0 2026-09-06：云端 180s idle
        超时后旧实现零重连零上报，用户体感"说了不理我"）。
        """
        logger.warning("cloud_engine_down reason=%s device=%s", message, self.device_id)
        await self._on_apm_error("qwen_reconnect_gave_up", message)

    async def _on_state(self, state: str) -> None:
        self.stats["apm_session_state"] = state

    async def _on_apm_error(self, code: str, message: str) -> None:
        """ApmBridge 致命错误（重连放弃等）→ ctrl 通知 sidecar（手机端可感知，不静默）"""
        logger.error("apm fatal device=%s code=%s: %s", self.device_id, code, message)
        if self._closed:
            return
        try:
            await self._send_msg({"type": MSG_CTRL, "action": "apm_error",
                                  "reason": code, "detail": message})
        except Exception as e:  # noqa: BLE001
            logger.warning("send apm_error ctrl failed: %s", e)

    # ---------- 远端状态 ----------
    async def on_peer_enter(self, user_id: str) -> None:
        """手机（远端）加入：重置说完判定与整形器，防跨会话状态污染（relay 教训）

        A9（2026-08-16 审计实锤）：peer leave 时 apm.close() 已置 _closed=True——旧实例
        不可复用；TRTC 断线重连（SDK 内置，手机无感知）后远端重进房，若不重建，
        feed 全被 `if self._closed: return` 吞掉 → 永久静音。此处为新会话重建 ApmBridge
        （新实例懒初始化，首个音频块到达才建云会话，与 session started 语义一致）。
        分层边界：本层管"手机端重进房"；ApmBridge 内部调度器（A8）管"模型云端断线"。
        """
        self._peer_entered = True
        self._peer_user_id = user_id
        self.stats["last_peer_ts"] = time.time()
        apm = self.apm
        if getattr(apm, "closed", False) or getattr(apm, "_closed", False) or getattr(apm, "dead", False):
            self._apm_rebuilds += 1
            self.stats["reconnects"] += 1
            logger.info("apm bridge rebuild #%d (peer re-enter) device=%s",
                        self._apm_rebuilds, self.device_id)
            self._build_apm()
            self.feeder = EndDetectFeeder(feed=self.apm.feed_pcm,
                                          sample_rate=self.feeder._sample_rate,
                                          pad_s=self._pad_s)
            self._up_q.flush()   # 清掉断连期间堆积的旧帧，防跨会话串音
        self.feeder.reset()
        self.shaper.reset()
        # barge-in 状态重置（新会话干净起步）
        self._barge_in = False
        self._down_speaking = False
        self._last_down_ts = 0.0
        # GPT-Live 待命/唤醒状态重置（新会话默认 ACTIVE；唤醒门禁由手机侧 KWS/重进房完成）
        self._standby = False
        self._standby_pending = False
        self._marker_tail_drop = False
        if self._router is not None:
            self._router.clear()   # 新会话清空旧文本（防跨会话串路由）
        logger.info("peer enter device=%s peer=%s", self.device_id, user_id)

    async def on_peer_leave(self, user_id: str) -> None:
        """手机（远端）离开：释放 APM 会话，回待命"""
        self._peer_entered = False
        self._peer_user_id = ""
        logger.info("peer leave device=%s peer=%s", self.device_id, user_id)
        closed_cleanly = True
        try:
            await self.apm.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("apm close on peer leave failed: %s", e)
            closed_cleanly = False
        self.stats["apm_session_state"] = "closed"
        await self._notify_apm_cancelled(closed_cleanly)

    # ---------- 关闭 ----------
    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        closed_cleanly = True
        self._standby = False
        self._standby_pending = False
        if self._router is not None:
            self._router.clear()
        if self._pcm_dump is not None:
            # P0 取证：会话结束冲刷缓冲 + 落 meta.json
            await self._pcm_dump.close()
        try:
            await self.apm.close()
        except Exception:  # noqa: BLE001
            closed_cleanly = False
        await self._notify_apm_cancelled(closed_cleanly)
        await self.shaper.stop()
        if self._consumer is not None:
            self._consumer.cancel()
            try:
                await self._consumer
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        logger.info("rtc session closed device=%s", self.device_id)

    async def _notify_apm_cancelled(self, closed_cleanly: bool) -> None:
        """APM 取消关闭后回调上报钩子：仅当 APM 曾真实激活；失败不冒泡"""
        if self._on_apm_cancelled is None or not getattr(self.apm, "started", False):
            return
        try:
            await self._on_apm_cancelled(closed_cleanly)
        except Exception:  # noqa: BLE001
            logger.debug("apm cancelled callback failed", exc_info=True)
