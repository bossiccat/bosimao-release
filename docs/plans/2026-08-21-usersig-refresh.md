# userSig 续签与过期恢复实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让 Android 端在 userSig 临近过期或收到 TRTC 凭证过期错误时，单次刷新、退出旧房间、重新签发并重进房，且拒绝旧代数结果回写。

**Architecture:** 复用现有安全 session 签发接口，不新增绕过认证的 refresh API；服务端补齐 `expires_at` 响应。Android 将有效期纳入 `VoiceSessionInfo`，由串行 `VoiceSessionCoordinator` 维护续签定时器和 refresh 标记，续签时走 `EXITING -> SIGNING -> ENTERING -> IN_ROOM`，并以 generation 丢弃迟到结果。所有刷新触发源都进入同一个 actor，天然去重。

**Tech Stack:** Kotlin、Coroutines、JUnit 4、OkHttp、FastAPI、Node.js Cloud Function。

---

### Task 1: 写 userSig 生命周期失败测试

**Files:**
- Modify: `mobile-app/app/src/test/java/com/jax/voice/net/VoiceSessionApiTest.kt`
- Modify: `mobile-app/app/src/test/java/com/jax/voice/voice/VoiceSessionCoordinatorTest.kt`

**步骤:**
1. 增加响应解析测试，断言 `expires_at` 被保留并转换为 epoch 毫秒。
2. 增加临近过期自动刷新测试，断言只产生一次新的签发、退出和重进房。
3. 增加过期错误触发刷新测试，重复上报只允许一次刷新。
4. 先运行目标测试，确认因缺少 `expires_at` 字段/刷新逻辑而失败。

### Task 2: 补齐服务端 expires_at 契约

**Files:**
- Modify: `deploy/trtc-sign/index.js`
- Modify: `backend/app/voice/rtc_session.py` 或统一响应层
- Test: `deploy/trtc-sign/test/*.test.js`、`backend/tests/unit/test_voice_session.py`

**步骤:**
1. Node 手机 session 和 sidecar sign 响应加入 ISO8601 `expires_at`。
2. Backend 保持内部 epoch 秒，HTTP 边界保证客户端可解析的 expires_at。
3. 运行 Node 与 backend 会话契约测试。

### Task 3: 扩展 Android 会话凭证模型与解析

**Files:**
- Modify: `mobile-app/app/src/main/java/com/jax/voice/net/VoiceSessionApi.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/voice/VoiceSessionLifecycle.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/voice/VoiceForegroundService.kt`

**步骤:**
1. 在 API 响应中 fail-closed 解析 ISO、epoch 秒、epoch 毫秒和数字字符串。
2. 将 `expiresAtEpochMs` 传入 `VoiceSessionInfo`，不把 secret 写入 UI model。
3. 运行 API 与编译测试。

### Task 4: 实现串行续签状态机

**Files:**
- Modify: `mobile-app/app/src/main/java/com/jax/voice/voice/VoiceSessionCoordinator.kt`
- Test: `mobile-app/app/src/test/java/com/jax/voice/voice/VoiceSessionCoordinatorTest.kt`

**步骤:**
1. 在 `IN_ROOM` 成功后按 `expiresAtEpochMs - 60s` 安排 refresh 事件。
2. 将 TRTC 过期错误分类为 refresh 请求，普通错误保持原有退出行为。
3. refresh 事件设置 pending 标记并复用现有退出动作；退出成功后启动新 generation 的签发。
4. 对重复 refresh 请求、旧 generation 结果和刷新失败做 fail-safe 处理。
5. 逐个运行测试，确认全量 Android JVM 测试通过。

### Task 5: 真实构建与运行态验证

**Files:**
- Generate: `outputs/波斯猫-v0.6.7.apk`
- Generate: `outputs/usersig-refresh-validation.md`
- Modify: `overview.md`

**步骤:**
1. 运行 Android 单测和 release 构建，检查 versionCode、签名与上一版一致。
2. 安装到模拟器，执行配对、进房、force-stop 恢复和 session 续签流程。
3. 记录服务端响应、logcat、TRTC 进房/退房次数和最终状态。
4. 只有真实验证通过后交付 APK。
