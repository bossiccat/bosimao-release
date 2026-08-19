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
 */
class VoiceSessionCoordinator(
    private val scope: CoroutineScope,
    private val actorDispatcher: CoroutineDispatcher = Dispatchers.Default,
    private val signSession: suspend (generation: Long, source: String) -> VoiceSessionInfo,
    private val enterRoom: suspend (generation: Long, session: VoiceSessionInfo) -> Unit,
    private val exitRoom: suspend (generation: Long) -> Unit,
    private val onModel: (VoiceSessionModel) -> Unit = {},
    private val onConflict: (String) -> Unit = {},
    private val signTimeoutMs: Long = 10_000L,
    private val enterTimeoutMs: Long = 15_000L,
    private val exitTimeoutMs: Long = 5_000L
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
        cancelTimeout()
        publish(m.copy(
            state = VoiceSessionState.ENTERING,
            sessionId = e.session.sessionId ?: e.session.roomId
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
        publish(m.copy(state = VoiceSessionState.IN_ROOM))
    }

    private fun handleExitSucceeded(e: Event.ExitSucceeded) {
        val m = _model.value
        if (e.generation != m.generation || m.state != VoiceSessionState.EXITING) {
            recordConflict("stale/illegal ExitSucceeded gen=${e.generation} state=${m.state}")
            return
        }
        cancelTimeout()
        publish(m.copy(state = VoiceSessionState.IDLE, sessionId = null, error = null))
    }

    private fun handleCancel() {
        when (_model.value.state) {
            VoiceSessionState.IDLE, VoiceSessionState.EXITING -> Unit // 幂等
            VoiceSessionState.SIGNING -> {
                // AC-05：取消直接回 IDLE，不等待退房回调
                cancelActiveWork()
                publish(_model.value.copy(state = VoiceSessionState.IDLE, sessionId = null, error = null))
            }
            VoiceSessionState.ENTERING, VoiceSessionState.IN_ROOM ->
                enterExiting()
        }
    }

    private fun handleTimeout(e: Event.Timeout) {
        val m = _model.value
        if (e.generation != m.generation || e.phase != m.state) return // 旧/过期超时丢弃
        when (m.state) {
            VoiceSessionState.SIGNING -> {
                cancelActiveWork()
                publish(m.copy(state = VoiceSessionState.IDLE, sessionId = null, error = "签发超时"))
            }
            VoiceSessionState.ENTERING -> enterExiting()
            VoiceSessionState.EXITING -> {
                // 退房回调缺失：强制回 IDLE，禁止永久退出锁
                cancelActiveWork()
                publish(m.copy(state = VoiceSessionState.IDLE, sessionId = null, error = "退出超时，已强制结束会话"))
            }
            else -> recordConflict("timeout while ${m.state}")
        }
    }

    private fun handleFailure(e: Event.Failure) {
        val m = _model.value
        if (e.generation != m.generation) return
        when (m.state) {
            VoiceSessionState.SIGNING -> {
                cancelActiveWork()
                publish(m.copy(state = VoiceSessionState.IDLE, sessionId = null, error = e.message))
            }
            VoiceSessionState.ENTERING, VoiceSessionState.IN_ROOM -> enterExiting()
            VoiceSessionState.EXITING -> {
                cancelActiveWork()
                publish(m.copy(state = VoiceSessionState.IDLE, sessionId = null, error = e.message))
            }
            else -> recordConflict("failure while ${m.state}")
        }
    }

    /** 进入 EXITING：取消进行中的效果并等待退房（退出超时兜底回 IDLE） */
    private fun enterExiting() {
        val m = _model.value
        cancelActiveWork()
        publish(m.copy(state = VoiceSessionState.EXITING, error = null))
        scheduleTimeout(exitTimeoutMs, VoiceSessionState.EXITING)
        launchExit(m.generation)
    }

    private fun launchSign(gen: Long, source: String) {
        signJob?.cancel()
        signJob = scope.launch {
            try {
                val s = signSession(gen, source)
                channel.send(Event.SignSucceeded(gen, s))
            } catch (e: CancellationException) {
                throw e
            } catch (t: Throwable) {
                channel.send(Event.Failure(gen, "sign_failed", t.message ?: "sign failed"))
            }
        }
    }

    private fun launchEnter(gen: Long, session: VoiceSessionInfo) {
        enterJob?.cancel()
        enterJob = scope.launch {
            try {
                // 这里只负责发起 SDK 请求；IN_ROOM 必须等外部真实 onEnterRoom 成功回调。
                enterRoom(gen, session)
            } catch (e: CancellationException) {
                throw e
            } catch (t: Throwable) {
                channel.send(Event.Failure(gen, "enter_failed", t.message ?: "enter failed"))
            }
        }
    }

    private fun launchExit(gen: Long) {
        exitJob?.cancel()
        exitJob = scope.launch {
            try {
                exitRoom(gen)
                channel.send(Event.ExitSucceeded(gen))
            } catch (e: CancellationException) {
                throw e
            } catch (t: Throwable) {
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
        signJob?.cancel(); signJob = null
        enterJob?.cancel(); enterJob = null
        exitJob?.cancel(); exitJob = null
        cancelTimeout()
    }

    private fun publish(m: VoiceSessionModel) {
        _model.value = m
        onModel(m)
    }

    private fun recordConflict(what: String) {
        conflicts++
        onConflict(what)
    }
}
