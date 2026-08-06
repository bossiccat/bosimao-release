# RtcClient 状态机测试设计（手机端，mock TRTC SDK）

> 版本：v1.0（2026-08-06）
> 作者：qa（测试工程师）
> 状态：**测试设计先行**（QA-PLAN §4.1 手机端 L0），等 fe-mobile 落地 `RtcClient.kt` 后落为 `mobile-app/app/src/test/.../RtcClientTest.kt` 执行。
> 依据：ADR-012（决策 1/3、§5.1、实施补充）、ARCHITECTURE §5.1、QA-PLAN §4.1/§3.3、mobile-app 现有 `VoiceState.kt`（六态 + ConnectionState）
> 防线：**mock 触发点按回调名参数化**（QA-PLAN §4.1 新增防线）——TRTC 真实回调名以 fe-mobile 锁定的 SDK jar 为准，测试通过注入回调名接线，fe-mobile 确认真实签名后只改 mock 接线、不改用例断言。

---

## 1. 被测对象与 mock 策略

**被测对象**：`RtcClient.kt`（fe-mobile 新增，TRTC Android SDK 封装，替换 VoiceWsClient）。

**依赖注入**：RtcClient 构造注入 `TRTCCloud`（或接口抽象 `IRtcEngine`），测试传 FakeRtcEngine（实现同签名接口），**不连真实 RTC 云**。

```kotlin
// 契约（ADR-012 实施补充，以锁定 SDK jar 为准，fe-mobile 回写差异到 ADR）
interface FakeRtcEngine {
    fun enterRoom(params: TRTCParams, scene: Int)
    fun exitRoom()
    fun startLocalAudio(quality: Int)
    fun stopLocalAudio()
    // 回调（mock 触发点参数化注入）
    fun onEnterRoom(result: Int)
    fun onExitRoom()
    fun onRemoteUserAudioStatus(userId: String, status: Int)   // 远端说话/静音
    fun onConnectionLost()
    fun onTryToReconnect()      // 注意：官方名 onTryToReconnect，非 onTryReconnect
    fun onConnectionRecovery()
    fun onUserVoiceVolume(userId: String, volume: Int)
}
```

**关键契约断言（来自 ADR-012 实施补充）**：
- 回调名 `onTryToReconnect`（非 onTryReconnect）
- `TRTCParams.strRoomId` 与 `intRoomId` 互斥（用 strRoomId 时 intRoomId=0）
- `exitRoom` 需等 `onExitRoom` 回调后再重启 MicRecorder（避免 mic 抢占竞态）

---

## 2. 用例清单（六态状态机 + 连接状态）

> 连接状态：`ConnectionState{DISCONNECTED, CONNECTING, CONNECTED}`（现有 VoiceState.kt）
> 会话状态：六态 VoicePhase（monitoring/listening/thinking/speaking/alerting）

### 2.1 进房（KWS 唤醒 → 进房）

| # | 场景 | mock 动作 | 断言 |
|---|------|-----------|------|
| S1 | KWS 唤醒 → 请求会话 → 进房成功 | 唤醒回调 → mock REST 返回 roomId/userSig → FakeRtcEngine.enterRoom 成功 → onEnterRoom(0) | connection=CONNECTING（进房前）→ CONNECTED（onEnterRoom 后）；startLocalAudio(SPEECH) 被调用 |
| S2 | 进房失败（token 无效/房间不存在） | onEnterRoom(-3316 或非 0) | connection 回落 DISCONNECTED（或错误态）；lastError 非空；不进入 CONNECTED |
| S3 | 重复进房幂等 | enterRoom 已 CONNECTED 时再次调用 enterRoom | 不重复创建房间/不崩溃；仍 CONNECTED（QA-PLAN §2 场景 C 幂等） |

### 2.2 断线 → 重连恢复

| # | 场景 | mock 动作 | 断言 |
|---|------|-----------|------|
| R1 | 断网触发重连 | onConnectionLost() → onTryToReconnect() → onConnectionRecovery() | connection=DISCONNECTED→CONNECTING→CONNECTED；UI 有重连状态；不丢会话上下文（不 reset 到 IDLE） |
| R2 | 重连失败保持重试 | onConnectionLost() → 多次 onTryToReconnect 无 recovery | connection 保持 CONNECTING；不误判退出房间 |
| R3 | 重连恢复后六态不受污染 | recovery 后用户继续说话 | 六态正常（monitoring→listening→…） |

### 2.3 退房（会话结束 → 退房恢复 MicRecorder）

| # | 场景 | mock 动作 | 断言 |
|---|------|-----------|------|
| E1 | 正常退房 | exitRoom() → onExitRoom() | connection=DISCONNECTED；**MicRecorder 重启**（mic handoff 恢复）；stopLocalAudio 被调用 |
| E2 | 退房前不重启 MicRecorder | 只 exitRoom() 未收到 onExitRoom | MicRecorder **未**重启（防止 mic 抢占竞态，ADR-012 实施补充） |
| E3 | 静默超时 → 退房 | 15s 无对话 → 自动 exitRoom → onExitRoom | 六态回落 monitoring；connection=DISCONNECTED |

### 2.4 mic handoff（ADR-012 关键）

| # | 场景 | mock 动作 | 断言 |
|---|------|-----------|------|
| H1 | 唤醒进房前 MicRecorder 停止 | KWS 命中 → RtcClient 进房 | MicRecorder.stop() 被调用（释放 mic）；RTC startLocalAudio 开始采集 |
| H2 | 会话期本地 VAD 不参与打断 | onRemoteUserAudioStatus(speaking) → 状态机切 Listening（不依赖本地 VAD） | 六态 Listening；MicRecorder 保持停止（会话期不重启） |
| H3 | 打断：远端说话 → 停播 → Listening | onRemoteUserAudioStatus(speaking→silent) | speaking→listening 切态；下行停止动作被触发 |
| H4 | 退房后 MicRecorder 恢复 | onExitRoom() | MicRecorder.start() 被调用；恢复"一直在听" |

### 2.5 六态状态机（回归，QA-PLAN §3.3）

| # | 场景 | mock 动作 | 断言 |
|---|------|-----------|------|
| T1 | monitoring→listening | 唤醒命中 | 六态正确切换 |
| T2 | listening→thinking→speaking | 上行 VAD 说完 → 回复开始 | 状态迁移正确 |
| T3 | speaking→monitoring | 回复结束 | 回落 monitoring |
| T4 | alerting | 异常（进房失败/重连失败超时） | 六态 alerting；lastError 有值 |
| T5 | **UI 状态与音频停止一致** | 音频已停但状态机未切 | 不允许「音频已停 UI 还在 Speaking」（QA-PLAN §3.3） |

---

## 3. mock 触发点参数化（幻觉依赖防线）

TRTC 回调名以 fe-mobile 锁定的 SDK jar 为准，测试不硬编码假设回调名。RtcClient 内部做一层回调名映射：

```kotlin
// RtcClient.kt 内部（fe-mobile 实现，QA-PLAN §4.1）
interface RtcEventListener {
    fun onEnterRoom(result: Int)
    fun onExitRoom()
    fun onRemoteAudioSpeaking(userId: String)
    fun onRemoteAudioSilent(userId: String)
    fun onConnectionLost()
    fun onReconnecting()
    fun onConnectionRecovery()
}
```

- 测试用例断言 `RtcEventListener` 语义事件，**不直接断言 TRTC 回调名**。
- fe-mobile 落地时：确认 TRTCCloudListener 真实回调名 → 在 RtcClient 内把 `onTryToReconnect` 等映射到 `onReconnecting()`——**只改映射接线，不改测试用例**。
- 若 fe-mobile 发现真实回调名与 ADR-012 不同 → **回写 ADR-012 实施补充**，qa 更新本设计；禁止静默改测试。

---

## 4. Kotlin 测试骨架（落位参考）

```kotlin
// mobile-app/app/src/test/java/com/jax/voice/net/RtcClientTest.kt
// L0：RtcClient 状态机测试（mock TRTC SDK）——QA-PLAN §4.1 / ADR-012 §5.1
package com.jax.voice.net

import org.junit.Assert.*
import org.junit.Test

class RtcClientTest {

    class FakeEngine : IRtcEngine { /* 记录调用 + 手动触发回调 */ }

    @Test fun `wake then enter room -> CONNECTED`() {
        val engine = FakeEngine()
        val client = RtcClient(engine)
        client.onWakeWordHit()          // KWS 唤醒
        engine.fireOnEnterRoom(0)       // 进房成功
        assertEquals(ConnectionState.CONNECTED, client.connectionState.value)
        assertTrue(engine.startLocalAudioCalled)
    }

    @Test fun `enter room fail -> not CONNECTED and error set`() {
        val engine = FakeEngine()
        val client = RtcClient(engine)
        client.onWakeWordHit()
        engine.fireOnEnterRoom(-3316)   // 进房失败
        assertNotEquals(ConnectionState.CONNECTED, client.connectionState.value)
        assertFalse(client.lastError.value.isNullOrEmpty())
    }

    @Test fun `enter room idempotent when already connected`() {
        val engine = FakeEngine()
        val client = RtcClient(engine)
        client.onWakeWordHit()
        engine.fireOnEnterRoom(0)
        val roomsBefore = engine.enterRoomCount
        client.onWakeWordHit()          // 重复唤醒
        assertEquals(roomsBefore, engine.enterRoomCount)  // 不重复进房
    }

    @Test fun `connection lost -> reconnect -> recovery -> CONNECTED`() {
        val engine = FakeEngine()
        val client = RtcClient(engine)
        client.onWakeWordHit()
        engine.fireOnEnterRoom(0)
        engine.fireOnConnectionLost()
        assertEquals(ConnectionState.DISCONNECTED, client.connectionState.value)
        engine.fireOnTryToReconnect()
        assertEquals(ConnectionState.CONNECTING, client.connectionState.value)
        engine.fireOnConnectionRecovery()
        assertEquals(ConnectionState.CONNECTED, client.connectionState.value)
        // 会话上下文不丢：不进 IDLE
        assertNotEquals(VoicePhase.IDLE, client.phase.value)
    }

    @Test fun `exit room waits onExitRoom before restarting MicRecorder`() {
        val engine = FakeEngine()
        val mic = FakeMicRecorder()
        val client = RtcClient(engine, micRecorder = mic)
        client.onWakeWordHit()
        engine.fireOnEnterRoom(0)
        client.endSession()
        assertFalse(mic.isRecording)    // 尚未收到 onExitRoom → 不重启
        engine.fireOnExitRoom()
        assertTrue(mic.isRecording)     // onExitRoom 后才恢复
    }

    @Test fun `remote audio speaking drives Listening without local VAD`() {
        val engine = FakeEngine()
        val client = RtcClient(engine)
        client.onWakeWordHit()
        engine.fireOnEnterRoom(0)
        engine.fireOnRemoteUserAudioStatus(SPEAKING)
        assertEquals(VoicePhase.LISTENING, client.phase.value)
        assertFalse(client.micRecorder.isRecording)  // mic handoff：会话期 MicRecorder 停止
    }
}
```

---

## 5. 执行与验收

- **执行时机**：fe-mobile 落地 `RtcClient.kt` + `IRtcEngine` 抽象后，qa 将本设计落为 `RtcClientTest.kt` 并运行。
- **验收标准**：上述用例全绿；mock 全部 Fake（不连真云）；无 skip 掩盖；回调名参数化（改接线不改断言）。
- **反作弊**：本文件是**新增测试**（先行设计），落地后不得出现 `@Ignore`/`.only`/弱化断言。
- **对接**：fe-mobile 落地后请同步 qa（回调名核验结果 + RtcClient 接口签名），qa 据此落地测试文件。
