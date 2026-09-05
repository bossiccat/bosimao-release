package com.jax.voice.voice

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlin.math.max

/**
 * 串行会话生命周期协调器（SPEC §4.2 / ADR-016）。
 *
 * 所有事件（Start/SignSucceeded/EnterSucceeded/Cancel/ExitSucceeded/Timeout/Failure）经
 * 单一 Channel 由单一消费协程在 [actorDispatcher] 上串行处理——不依赖并行业务布尔竞态。
 *
 * - 事件携带 generation：Start 每次被接受时递增；旧 generation 的迟到完成事件一律丢弃。
 * - SIGNING 取消直接回 IDLE，不等待退房回调（AC-05，修复永久退出锁）。
 * - ENTERING 取消/超时/失败：幂等进入 EXITING，有限时间回 IDLE（AC-06）。
 * - 退出超时强制回 IDLE：退房回调缺失时不得永久锁（AC-05/AC-06 兜底）。
 * - 非法转换记录 onConflict 并忽略，不静默执行（AC-07）。
 *
 * 效果（signSession/enterRoom/exitRoom）由调用方注入并分别启动于独立协程；
 * 完成结果以带 generation 的事件回投，actor 在串行消费时校验并丢弃过期结果。
 *
 * 可观测性（2026-09-05 真机取证补上）：每一次迁移/超时/失败/冲突/效果异常都经
 * [logger] 记一条带 generation 的结构化日志，TAG 为 [TAG]，真机过滤：
 * `adb logcat -s VoiceSessionCoord:*`。日志不改变任何行为语义。
 */
class VoiceSessionCoordinator internal constructor(
    private val scope: CoroutineScope,
    private val actorDispatcher: CoroutineDispatcher = Dispatchers.Default,
    private val signSession: suspend (generation: Long, source: String) -> VoiceSessionInfo,
    private val enterRoom: suspend (generation: Long, session: VoiceSessionInfo) -> Unit,
    private val exitRoom: suspend (generation: Long) -> Unit,
    private val onModel: (VoiceSessionModel) -> Unit = {},
    private val onConflict: (String) -> Unit = {},
    private val signTimeoutMs: Long = 10_000L,
    private val enterTimeoutMs: Long = 15_000L,
    private val exitTimeoutMs: Long = 5_000L,
    private val refreshLeadMs: Long = 60_000L,
    private val nowMs: () -> Long = { System.currentTimeMillis() },
    private val logger: SessionLogger = AndroidSessionLogger
) {
    /** actor 内部事件：带 generation 的完成事件由 [handle] 按当前代数校验 */
    sealed class Event {
        data class Start(val source: String) : Event()
        data class SignSucceeded(val generation: Long, val session: VoiceSessionInfo) : Event()
        data class EnterSucceeded(val generation: Long) : Event()
        data class ExitSucceeded(val generation: Long) : Event()
        data class Failure(val generation: Long, val code: String, val message: String) : Event()
        data class Timeout(val generation: Long, val phase: VoiceSessionState) : Event()
        data object Cancel : Event()
    }

    companion object {
        /** logcat tag：短，便于真机 `adb logcat -s VoiceSessionCoord:*` 取证 */
        internal const val TAG = "VoiceSessionCoord"
    }

    private val channel = Channel<Event>(Channel.UNLIMITED)
    private val _model = MutableStateFlow(VoiceSessionModel())
    val model: StateFlow<VoiceSessionModel> = _model.asStateFlow()

    /** 仅 actor 写入；postFailure 等外部事件发送时跨线程读取，须可见 */
    @Volatile
    private var generation = 0L
    private var signJob: Job? = null
    private var enterJob: Job? = null
    private var exitJob: Job? = null
    private var timeoutJob: Job? = null
    private var refreshJob: Job? = null
    private var refreshRequested = false
    private var lastStartSource = "main"

    @Volatile
    var conflicts: Int = 0
        private set

    init {
        scope.launch(actorDispatcher) {
            for (event in channel) {
                try {
                    handle(event)
                } catch (e: CancellationException) {
                    throw e
                } catch (t: Throwable) {
                    // 带堆栈：只记 message 会丢掉定位所需的信息（真机取证硬性要求）
                    logger.e(
                        TAG,
                        "actor error: ${t.javaClass.simpleName}: ${t.message} | " +
                            "generation=$generation state=${_model.value.state}",
                        t
                    )
                    recordConflict("actor error: ${t.message}")
                }
            }
        }
    }

    /** 发起会话（三入口统一命令；活动会话期间幂等忽略） */
    fun start(source: String) {
        scope.launch { channel.send(Event.Start(source)) }
    }

    /** 取消当前会话（IDLE/EXITING 幂等忽略；SIGNING 直接回 IDLE） */
    fun cancel() {
        scope.launch { channel.send(Event.Cancel) }
    }

    /** 上报指定 generation 的真实 RTC 进房成功；旧会话回调由 actor 丢弃。 */
    fun postEnterSucceeded(generation: Long) {
        scope.launch { channel.send(Event.EnterSucceeded(generation)) }
    }

    /** 上报当前会话失败（如 RTC onError）；IDLE 时忽略 */
    fun postFailure(code: String, message: String) {
        scope.launch { channel.send(Event.Failure(generation, code, message)) }
    }

    private fun handle(event: Event) {
        when (event) {
            is Event.Start -> handleStart(event.source)
            is Event.SignSucceeded -> handleSignSucceeded(event)
            is Event.EnterSucceeded -> handleEnterSucceeded(event)
            is Event.ExitSucceeded -> handleExitSucceeded(event)
            is Event.Failure -> handleFailure(event)
            is Event.Timeout -> handleTimeout(event)
            is Event.Cancel -> handleCancel()
        }
    }

    private fun handleStart(source: String) {
        val m = _model.value
        if (m.state != VoiceSessionState.IDLE) {
            recordConflict("start($source) while ${m.state}")
            return
        }
        generation++
        lastStartSource = source
        refreshRequested = false
        logger.i(TAG, "start accepted source=$source generation=$generation state=${m.state}->SIGNING signTimeoutMs=${signTimeoutMs}ms")
        publish(m.copy(
            state = VoiceSessionState.SIGNING,
            generation = generation,
            sessionId = null,
            error = null
        ))
        scheduleTimeout(signTimeoutMs, VoiceSessionState.SIGNING)
        launchSign(generation, source)
    }

    private fun handleSignSucceeded(e: Event.SignSucceeded) {
        val m = _model.value
        if (e.generation != m.generation || m.state != VoiceSessionState.SIGNING) {
            recordConflict("stale/illegal SignSucceeded gen=${e.generation} state=${m.state}")
            return
        }
        val sessionId = e.session.sessionId ?: e.session.roomId
        cancelTimeout()
        logger.i(
            TAG,
            "sign succeeded generation=${e.generation} sessionId=$sessionId roomId=${e.session.roomId} " +
                "sdkAppId=${e.session.sdkAppId} expiresAtEpochMs=${e.session.expiresAtEpochMs} " +
                "state=${m.state}->ENTERING enterTimeoutMs=${enterTimeoutMs}ms"
        )
        publish(m.copy(
            state = VoiceSessionState.ENTERING,
            sessionId = sessionId,
            sessionExpiresAtEpochMs = e.session.expiresAtEpochMs
        ))
        scheduleTimeout(enterTimeoutMs, VoiceSessionState.ENTERING)
        launchEnter(e.generation, e.session)
    }

    private fun handleEnterSucceeded(e: Event.EnterSucceeded) {
        val m = _model.value
        if (e.generation != m.generation || m.state != VoiceSessionState.ENTERING) {
            recordConflict("stale/illegal EnterSucceeded gen=${e.generation} state=${m.state}")
            return
        }
        cancelTimeout()
        logger.i(TAG, "enter succeeded generation=${e.generation} sessionId=${m.sessionId} state=${m.state}->IN_ROOM refreshLeadMs=${refreshLeadMs}ms")
        publish(m.copy(state = VoiceSessionState.IN_ROOM))
        scheduleRefresh(m.generation, m.sessionExpiresAtEpochMs)
    }

    private fun handleExitSucceeded(e: Event.ExitSucceeded) {
        val m = _model.value
        if (e.generation != m.generation || m.state != VoiceSessionState.EXITING) {
            recordConflict("stale/illegal ExitSucceeded gen=${e.generation} state=${m.state}")
            return
        }
        cancelTimeout()
        logger.i(
            TAG,
            "exit succeeded generation=${e.generation} sessionId=${m.sessionId} refreshRequested=$refreshRequested " +
                "state=${m.state}->${if (refreshRequested) "SIGNING(refresh)" else "IDLE"}"
        )
        if (refreshRequested) {
            refreshRequested = false
            generation++
            publish(_model.value.copy(state = VoiceSessionState.SIGNING, generation = generation, sessionId = null, sessionExpiresAtEpochMs = 0L, error = null))
            scheduleTimeout(signTimeoutMs, VoiceSessionState.SIGNING)
            launchSign(generation, lastStartSource)
        } else {
            publish(m.copy(state = VoiceSessionState.IDLE, sessionId = null, sessionExpiresAtEpochMs = 0L, error = null))
        }
    }

    private fun handleCancel() {
        val state = _model.value.state
        logger.i(TAG, "cancel requested state=$state generation=${_model.value.generation}")
        when (state) {
            VoiceSessionState.IDLE, VoiceSessionState.EXITING -> Unit // 幂等
            VoiceSessionState.SIGNING -> {
                // AC-05：取消直接回 IDLE，不等待退房回调
                cancelActiveWork()
                logger.i(TAG, "cancel while SIGNING -> IDLE generation=${_model.value.generation} (no exit effect)")
                publish(_model.value.copy(state = VoiceSessionState.IDLE, sessionId = null, error = null))
            }
            VoiceSessionState.ENTERING, VoiceSessionState.IN_ROOM ->
                enterExiting()
        }
    }

    private fun handleTimeout(e: Event.Timeout) {
        val m = _model.value
        if (e.generation != m.generation || e.phase != m.state) {
            // 旧/过期超时丢弃：真机上「点了没反应」的一大来源，必须留痕
            logger.d(TAG, "timeout dropped: stale event generation=${e.generation} phase=${e.phase} vs current generation=${m.generation} state=${m.state}")
            return
        }
        logger.w(TAG, "timeout phase=${e.phase} generation=${e.generation} state=${m.state}")
        when (m.state) {
            VoiceSessionState.SIGNING -> {
                cancelActiveWork()
                logger.i(TAG, "timeout while SIGNING -> IDLE generation=${e.generation}")
                publish(m.copy(state = VoiceSessionState.IDLE, sessionId = null, error = "签发超时"))
            }
            VoiceSessionState.ENTERING -> enterExiting()
            VoiceSessionState.EXITING -> {
                // 退房回调缺失：强制回 IDLE，禁止永久退出锁
                cancelActiveWork()
                logger.i(TAG, "timeout while EXITING -> IDLE generation=${e.generation} (exit callback missing, forced)")
                publish(m.copy(state = VoiceSessionState.IDLE, sessionId = null, error = "退出超时，已强制结束会话"))
            }
            else -> recordConflict("timeout while ${m.state}")
        }
    }

    private fun handleFailure(e: Event.Failure) {
        val m = _model.value
        if (e.generation != m.generation) {
            logger.d(TAG, "failure dropped: stale generation=${e.generation} current=${m.generation} code=${e.code} message=${e.message}")
            return
        }
        // 真机取证主目标：Failed to connect / sign_failed / enter_failed 等都要能直接看到
        logger.w(TAG, "failure code=${e.code} message=${e.message} generation=${e.generation} state=${m.state}")
        if (isUserSigExpiry(e.code)) {
            if (m.state == VoiceSessionState.IN_ROOM) {
                logger.i(TAG, "failure is usersig expiry in room -> request refresh generation=${e.generation} code=${e.code} refreshRequested=$refreshRequested")
                requestRefresh(e.generation)
            } else {
                logger.i(TAG, "failure is usersig expiry outside room -> ignore generation=${e.generation} code=${e.code} state=${m.state} refreshRequested=$refreshRequested")
            }
            return
        }
        when (m.state) {
            VoiceSessionState.SIGNING -> {
                cancelActiveWork()
                refreshRequested = false
                logger.i(TAG, "failure while SIGNING -> IDLE generation=${e.generation} code=${e.code}")
                publish(m.copy(state = VoiceSessionState.IDLE, sessionId = null, sessionExpiresAtEpochMs = 0L, error = e.message))
            }
            VoiceSessionState.ENTERING, VoiceSessionState.IN_ROOM -> {
                logger.i(TAG, "failure while ${m.state} -> EXITING generation=${e.generation} code=${e.code}")
                enterExiting()
            }
            VoiceSessionState.EXITING -> {
                cancelActiveWork()
                refreshRequested = false
                logger.i(TAG, "failure while EXITING -> IDLE generation=${e.generation} code=${e.code}")
                publish(m.copy(state = VoiceSessionState.IDLE, sessionId = null, sessionExpiresAtEpochMs = 0L, error = e.message))
            }
            else -> recordConflict("failure while ${m.state}")
        }
    }

    /** 进入 EXITING：取消进行中的效果并等待退房（退出超时兜底回 IDLE） */
    private fun isUserSigExpiry(code: String): Boolean = code == "usersig_expired" || code == "-1001" || code == "70001"

    private fun requestRefresh(gen: Long) {
        if (refreshRequested || _model.value.generation != gen || _model.value.state != VoiceSessionState.IN_ROOM) {
            logger.d(TAG, "refresh request ignored generation=$gen state=${_model.value.state} refreshRequested=$refreshRequested")
            return
        }
        logger.i(TAG, "refresh requested generation=$gen -> EXITING (re-sign)")
        refreshRequested = true
        refreshJob?.cancel()
        refreshJob = null
        enterExiting()
    }

    private fun scheduleRefresh(gen: Long, expiresAtEpochMs: Long) {
        refreshJob?.cancel()
        if (expiresAtEpochMs <= 0L) {
            logger.d(TAG, "refresh not scheduled: session has no expiry generation=$gen")
            return
        }
        val delayMs = max(0L, expiresAtEpochMs - refreshLeadMs - nowMs())
        logger.d(TAG, "refresh scheduled generation=$gen delayMs=$delayMs expiresAtEpochMs=$expiresAtEpochMs")
        refreshJob = scope.launch {
            delay(delayMs)
            channel.send(Event.Failure(gen, "usersig_expired", "userSig 即将过期，准备续签"))
        }
    }

    private fun enterExiting() {
        val m = _model.value
        cancelActiveWork()
        logger.i(TAG, "->EXITING generation=${m.generation} sessionId=${m.sessionId} from=${m.state} exitTimeoutMs=${exitTimeoutMs}ms")
        publish(m.copy(state = VoiceSessionState.EXITING, error = null))
        scheduleTimeout(exitTimeoutMs, VoiceSessionState.EXITING)
        launchExit(m.generation)
    }

    private fun launchSign(gen: Long, source: String) {
        signJob?.cancel()
        signJob = scope.launch {
            logger.d(TAG, "sign effect started generation=$gen source=$source")
            try {
                val s = signSession(gen, source)
                logger.d(TAG, "sign effect returned generation=$gen sessionId=${s.sessionId ?: s.roomId}")
                channel.send(Event.SignSucceeded(gen, s))
            } catch (e: CancellationException) {
                logger.d(TAG, "sign effect cancelled generation=$gen source=$source")
                throw e
            } catch (t: Throwable) {
                // 真机取证关键：Failed to connect ... 这类网络失败原本只落到 message 里，堆栈全丢
                logger.e(TAG, "sign effect failed generation=$gen source=$source error=${t.javaClass.simpleName}: ${t.message}", t)
                channel.send(Event.Failure(gen, "sign_failed", t.message ?: "sign failed"))
            }
        }
    }

    private fun launchEnter(gen: Long, session: VoiceSessionInfo) {
        enterJob?.cancel()
        enterJob = scope.launch {
            val sessionId = session.sessionId ?: session.roomId
            logger.d(TAG, "enter effect started generation=$gen sessionId=$sessionId")
            try {
                // 这里只负责发起 SDK 请求；IN_ROOM 必须等外部真实 onEnterRoom 成功回调。
                enterRoom(gen, session)
                logger.d(TAG, "enter effect returned generation=$gen sessionId=$sessionId (IN_ROOM waits for real callback)")
            } catch (e: CancellationException) {
                logger.d(TAG, "enter effect cancelled generation=$gen sessionId=$sessionId")
                throw e
            } catch (t: Throwable) {
                logger.e(TAG, "enter effect failed generation=$gen sessionId=$sessionId error=${t.javaClass.simpleName}: ${t.message}", t)
                channel.send(Event.Failure(gen, "enter_failed", t.message ?: "enter failed"))
            }
        }
    }

    private fun launchExit(gen: Long) {
        exitJob?.cancel()
        exitJob = scope.launch {
            logger.d(TAG, "exit effect started generation=$gen")
            try {
                exitRoom(gen)
                channel.send(Event.ExitSucceeded(gen))
            } catch (e: CancellationException) {
                logger.d(TAG, "exit effect cancelled generation=$gen")
                throw e
            } catch (t: Throwable) {
                logger.e(TAG, "exit effect failed generation=$gen error=${t.javaClass.simpleName}: ${t.message}", t)
                channel.send(Event.Failure(gen, "exit_failed", t.message ?: "exit failed"))
            }
        }
    }

    private fun scheduleTimeout(ms: Long, phase: VoiceSessionState) {
        cancelTimeout()
        val gen = generation
        timeoutJob = scope.launch {
            delay(ms)
            channel.send(Event.Timeout(gen, phase))
        }
    }

    private fun cancelTimeout() {
        timeoutJob?.cancel()
        timeoutJob = null
    }

    private fun cancelActiveWork() {
        refreshJob?.cancel(); refreshJob = null
        signJob?.cancel(); signJob = null
        enterJob?.cancel(); enterJob = null
        exitJob?.cancel(); exitJob = null
        cancelTimeout()
    }

    private fun publish(m: VoiceSessionModel) {
        _model.value = m
        onModel(m)
    }

    /**
     * 非法转换留痕。此前完全静默（只递增计数 + 回调），真机上「点了没反应」时
     * 无法判断是不是被冲突分支吞掉了 —— AC-07 要求记录，记录就必须可见。
     */
    private fun recordConflict(what: String) {
        conflicts++
        logger.w(
            TAG,
            "conflict #$conflicts: $what | state=${_model.value.state} generation=${_model.value.generation} " +
                "sessionId=${_model.value.sessionId} conflicts=$conflicts"
        )
        onConflict(what)
    }
}
