"""ApmBridge — MiniCPM-o Realtime API 云端全双工引擎（M3 路径 A 云版，spec §8.2）

桥接：VoiceSession（手机 WS） ↔ MiniCPM-o Realtime API（wss://minicpmo45.modelbest.cn/v1/realtime?mode=audio）

- 上行：手机 16k s16 PCM 帧 → 累积 1s 块 → float32 base64 → input.append（官方 chunk-ms=1000 节奏）
- 下行：API audio delta（24k f32 base64）→ 重采样 16k s16 PCM → 回调 on_audio_out（走现有二进制音频帧）
- 全双工：无 VAD 轮次控制，随时打断（force_listen=false，模型原生 barge-in）
- 鉴权：当前匿名可用（无需 key）；预留 Authorization 注入点
- 代理：必须绕过系统代理（本机 Clash 127.0.0.1:7890 未运行会劫持连接——2026-08-05 实测）

用法（独立验证，见 apm_verify.py）：
    python -m backend.app.voice.apm_verify --wav tmp/poc_b3_ask_16k.wav --out tmp/bridge_out.wav

断线自愈（A8，2026-08-16 审计实锤）：旧逻辑重连失败一次即置 _ws=None，
后续 feed 被 `if self._ws is None: return` 短路 → 永不重连 → 用户无限静音。
现改为：
- _recv_loop 退出（连接断/服务端关会话）→ 标记 down → 指数退避重连（1s→60s，
  连续 10 次失败放弃并上报 on_error）
- 重连成功 = 完整重握手（排队 → session.init → session.created，即会话级 re-sync）
- 放弃后（self.dead）feed 丢弃并计数，不再无限堆内存
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Awaitable, Callable

import numpy as np

from .apm_handshake import connect_and_handshake
from .apm_reconnect import ReconnectScheduler

logger = logging.getLogger(__name__)

# MiniCPM-o Realtime API 端点（官方文档 https://minicpmo45.modelbest.cn/docs）
DEFAULT_API_URL = "wss://minicpmo45.modelbest.cn/v1/realtime?mode=audio"
DEFAULT_SYSTEM_PROMPT = (
    "你是波斯猫，一个聪明务实的中文语音助手，性格友好、略带猫的俏皮。"
    "默认用口语化中文简洁回答（先给结论，一般两三句话）；"
    "用户问复杂问题时给出有信息量、有条理的回答，想深入再展开。"
    "不知道就诚实说不知道，不编造。"
    "\n\n"
    "【待命/唤醒规则】\n"
    "当用户说\"退下\"、\"你退下\"、\"波斯猫退下\"或类似的话让你离开时，"
    "你简短回应（如\"好的，我退下了\"），"
    "并在你回复文本的最末尾加上 [STANDBY] 标记。"
    "此后进入待命模式，不再回应任何用户的话——无论用户说什么，"
    "除非听到\"波斯猫\"三个字才恢复。"
    "当用户说\"波斯猫\"唤醒你时，你简短回应（如\"我在\"），"
    "并在回复文本的最末尾加上 [ACTIVE] 标记，然后恢复正常对话。"
)

# 上行块：200ms @16k s16 = 6400B
# 原 1s（32000B）导致固有 1s 延迟（用户反馈"反应速度不应该要那么久"）；
# API 的 input.append 无最小块约束，200ms 兼顾低延迟与不过载 WS（5 msg/s）
UPLINK_CHUNK_BYTES = 16000 * 2 // 5  # 200ms * 16bit
# API 输出：24k 单声道 float32
OUT_RATE = 24000
OUT_DTYPE = np.float32
# 下行转 16k s16（与现有上行/音频帧协议一致）
DOWN_RATE = 16000


def f32_to_s16_16k(audio_f32_24k: bytes) -> bytes:
    """24k f32 PCM → 16k s16 PCM（线性抽取 3:2 + int16 量化）"""
    arr = np.frombuffer(audio_f32_24k, dtype=np.float32)
    # 24k -> 16k：取每 3 样本的第 2 个（24k*2/3 = 16k）
    step = OUT_RATE / DOWN_RATE  # 1.5
    idx = (np.arange(int(len(arr) / step)) * step).astype(np.int64)
    idx = idx[idx < len(arr)]
    down = np.clip(arr[idx], -1.0, 1.0)
    return (down * 32767.0).astype(np.int16).tobytes()


class ApmBridge:
    """MiniCPM-o Realtime API 全双工桥接（单会话）"""

    def __init__(
        self,
        on_audio_out: Callable[[bytes], Awaitable[None]],
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_state: Callable[[str], Awaitable[None]] | None = None,
        api_url: str = DEFAULT_API_URL,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        token: str = "",
        on_error: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._on_audio_out = on_audio_out
        self._on_text = on_text
        self._on_state = on_state
        self._api_url = api_url
        self._system_prompt = system_prompt
        self._token = token
        self._on_error = on_error          # A8：连续重连失败放弃 → 上层上报（手机端可感知）
        self._ws: Any = None
        self._up_buf = bytearray()          # 16k s16 上行累积
        self._send_lock = asyncio.Lock()
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self._dead = False                  # A8：重连放弃后终态（feed 丢弃并计数）
        self._session_id = ""
        self._started = False               # 懒初始化：首个音频块到达才建会话（避免空闲连接被服务端回收）
        self._reconnect_lock = asyncio.Lock()
        # A8：指数退避重连（不再"失败一次即永久静默"）
        self.reconnects = 0                 # 成功重连次数（观测）
        self.dropped_frames = 0             # 断线窗口丢弃的上行帧计数
        self._scheduler = ReconnectScheduler(
            attempt=self._reconnect_attempt,
            on_give_up=self._on_reconnect_give_up,
        )

    @property
    def dead(self) -> bool:
        """重连放弃后的终态：须由上层重建实例（A9 peer enter 重建路径）恢复"""
        return self._dead

    @property
    def closed(self) -> bool:
        """显式 close()（peer leave）终态：同 dead，须上层重建实例恢复"""
        return self._closed

    def _report_error(self, code: str, message: str) -> None:
        """错误回调统一入口：异常不得击穿调用链；协程回调转 task 不阻塞"""
        if self._on_error is None:
            return
        try:
            cb = self._on_error(code, message)
            if asyncio.iscoroutine(cb):
                asyncio.get_running_loop().create_task(cb)
        except Exception as e:  # noqa: BLE001
            logger.warning("on_error callback failed: %s", e)

    async def _on_reconnect_give_up(self) -> None:
        """连续失败达上限：进入终态并上报（手机端感知"云端引擎断开"）"""
        self._dead = True
        self._report_error(
            "apm_reconnect_gave_up",
            f"云端引擎断开：连续 {self._scheduler.max_attempts} 次重连失败",
        )

    async def _reconnect_attempt(self) -> bool:
        """scheduler 单次尝试：成功=完整重握手（排队+session.init，即 re-sync）"""
        async with self._reconnect_lock:
            if self._closed or self._dead:
                return True  # 已关闭/已放弃：让循环退出
            ok = await self._reconnect_once()
            if ok:
                self.reconnects += 1
                if self._on_state is not None:
                    await self._on_state("reconnected")
            return ok

    @property
    def started(self) -> bool:
        """APM 实时会话是否已真实建立（懒初始化完成后为 True）"""
        return self._started

    async def start(self) -> None:
        """连接 API + 会话初始化 + 启动接收循环（阻塞直到就绪）"""
        self._ws, self._session_id = await connect_and_handshake(
            self._api_url, self._token, self._system_prompt,
        )
        # ws 作参数快照：任务首跑时 _ws 可能已被发送路径置 None（同一断链事件）
        self._recv_task = asyncio.create_task(self._recv_loop(self._ws))
        self._started = True

    async def feed_pcm(self, s16_bytes: bytes) -> None:
        """上行：手机 16k s16 PCM 帧 → 累积 1s 块发送"""
        if self._closed:
            return
        if self._dead:
            # A8：重连放弃终态——丢弃并计数（日志限流），不堆内存；由上层重建恢复
            self.dropped_frames += 1
            if self.dropped_frames == 1 or self.dropped_frames % 250 == 0:
                logger.warning("apm dead, drop uplink frame #%d (total bytes dropped)",
                               self.dropped_frames)
            return
        if not self._started:
            # 懒初始化：首个音频块到达才建会话（relay 常驻时不能提前建——空闲会被服务端回收，
            # 2026-08-06 现场：01:27 建连、01:37 手机配对，连接已死 → uplink send failed → 无回复）
            try:
                await self.start()
                self._started = True
            except Exception as e:  # noqa: BLE001
                # A8：首连失败不再抛死——交退避调度器重试，期间音频丢弃计数
                logger.warning("apm initial connect failed (%s), scheduling reconnect", e)
                self._ws = None
                self._started = True   # 防重复触发首连；后续由 scheduler 建连
                self._scheduler.schedule()
                self.dropped_frames += 1
                return
        if self._ws is None:
            # A8：断线窗口（scheduler 在退避中）——丢弃并计数，不堆积
            self.dropped_frames += 1
            if self.dropped_frames == 1 or self.dropped_frames % 250 == 0:
                logger.warning("apm link down, drop uplink frame #%d", self.dropped_frames)
            return
        self._up_buf.extend(s16_bytes)
        while len(self._up_buf) >= UPLINK_CHUNK_BYTES:
            chunk = bytes(self._up_buf[:UPLINK_CHUNK_BYTES])
            del self._up_buf[:UPLINK_CHUNK_BYTES]
            await self._send_chunk(chunk)

    async def _send_chunk(self, s16_chunk: bytes) -> None:
        """发一个 1s 块：s16 → f32 → base64 → input.append；发送失败触发重连调度"""
        f32 = np.frombuffer(s16_chunk, dtype=np.int16).astype(np.float32) / 32768.0
        b64 = base64.b64encode(f32.tobytes()).decode("ascii")
        async with self._send_lock:
            try:
                await self._ws.send(json.dumps({
                    "type": "input.append",
                    "input": {"audio": b64, "force_listen": False},
                }))
            except Exception as e:  # noqa: BLE001
                if self._closed:
                    return
                # A8：发送失败 = 链路断——标记断链并交退避调度器（不再只试一次）
                logger.warning("apm uplink send failed (%s), scheduling reconnect", e)
                self._ws = None
                self.dropped_frames += 1   # 本块上行丢弃计数
                self._scheduler.schedule()

    async def _teardown_link(self) -> None:
        """关旧 ws + 停 recv 循环（不置 _closed，供重连复用实例）"""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws = None
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._recv_task = None

    async def _reconnect_once(self) -> bool:
        """单次重连：清旧链路 → 完整重握手（排队 + session.init = 会话级 re-sync）"""
        try:
            await self._teardown_link()
            await self.start()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("apm reconnect attempt failed: %s", e)
            # 半开连接（握手中途失败）必须关闭，防泄漏
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:  # noqa: BLE001
                    pass
            self._ws = None
            return False

    async def _reconnect(self) -> None:
        """立即单次重连；失败自动转入退避调度循环"""
        async with self._reconnect_lock:
            if self._closed or self._dead:
                return
            ok = await self._reconnect_once()
        if not ok:
            self._scheduler.schedule()

    async def _recv_loop(self, ws) -> None:
        """下行：SSE/JSON 事件循环 → audio delta 转 16k s16 → on_audio_out

        A8：任何退出路径（连接断/服务端关会话/错误）= 链路 down → 交退避调度器重连。
        ws 为任务创建时的快照：循环期间 _ws 可能被发送路径置 None（同一断链事件），
        recv 必须仍引用旧 ws 让其 recv() 抛错退出。
        """
        while not self._closed:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=600)
            except asyncio.TimeoutError:
                logger.info("apm recv idle 600s, session timeout")
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("apm recv end: %s", e)
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "response.output.delta":
                kind = msg.get("kind")
                if kind == "text" and msg.get("text") and self._on_text:
                    await self._on_text(msg["text"])
                elif kind == "audio" and msg.get("audio"):
                    try:
                        pcm = f32_to_s16_16k(base64.b64decode(msg["audio"]))
                    except Exception as e:  # noqa: BLE001
                        logger.warning("apm audio decode fail: %s", e)
                        continue
                    await self._on_audio_out(pcm)
                elif kind == "listen" and self._on_state:
                    await self._on_state("listening")
            elif mtype == "session.closed":
                logger.info("apm session closed: %s", msg.get("reason"))
                break
            elif mtype == "error":
                logger.error("apm error: %s", msg)
                break
        # A8：recv 循环退出 = 链路断——置 None（feed 开始丢弃计数）+ 退避重连。
        # 仅当仍是本代链路才置 None/调度：若期间已重连成功（_ws 换代），跳过防误伤。
        if not self._closed and self._ws is ws:
            self._ws = None
            self._scheduler.schedule()

    async def close(self) -> None:
        self._closed = True
        self._scheduler.cancel()
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "session.close", "reason": "user_stop"}))
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._recv_task is not None:
            self._recv_task.cancel()
