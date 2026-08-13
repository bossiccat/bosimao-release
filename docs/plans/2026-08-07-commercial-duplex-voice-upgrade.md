# Commercial Duplex Voice Upgrade Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将当前 Windows + Android 语音原型升级为具备成熟 RTC 主链路、生产认证、有界实时音频、串行会话状态机和真实 Android 扬声器证据的单用户商业 MVP。

**Architecture:** TRTC 承担 Android 与 Windows sidecar 之间的正式媒体平面，FastAPI 只负责设备、签发、状态和控制；`rtc_bridge` 以固定 20 ms PCM 和有界队列连接 MiniCPM-o/APM。Android 与 Windows 使用统一双层状态契约，Tauri 管理单实例 sidecar，SQLite 保存最小凭证元数据、审计和可选加密转写。

**Tech Stack:** Python 3.11 / FastAPI / SQLite / pytest；Kotlin / Android SDK 35 / TRTC 13.4.0.20477 / JUnit / MockK；React 18.3.1 / XState 5.19.0 / Tauri 2.11.x / Lucide 0.469.0；Electron 31.7.7 / TRTC Electron SDK 候选 13.4.802-beta.3。

**Authoritative Contract:** `docs/commercial-upgrade-SPEC.md`

**P0 Rules:** 禁止 emoji 功能图标；全项目只使用 Lucide 语义 SVG；禁止紫粉渐变；组件禁止 Token 外硬编码颜色；禁止空洞文案、虚假 Hero 和弹性缓动。

---

## Execution Order and Gates

按 Task 1 至 14 顺序执行。Task 3 与 Task 4 可在 Task 2 完成后并行；Task 6 与 Task 8 可在 API 契约生成后并行；Task 12 必须等待前序实现和局部测试全部通过。每个任务按红、绿、重构、验证、提交推进，不得把缺失凭证或真机证据改写为“跳过即通过”。

### Task 1: 锁定 ADR、OpenAPI 与版本清单

**Files:**
- Create: `docs/api/commercial-voice-openapi.yaml`
- Create: `docs/decisions/ADR-013-commercial-rtc-main-path.md`
- Create: `docs/decisions/ADR-014-voice-security-fail-closed.md`
- Create: `docs/decisions/ADR-015-bounded-20ms-audio.md`
- Create: `docs/decisions/ADR-016-serial-voice-state.md`
- Create: `docs/decisions/ADR-017-tauri-sidecar-supervision.md`
- Create: `docs/decisions/ADR-018-local-privacy-data.md`
- Modify: `docs/decisions/OPEN-DECISIONS.md`
- Modify: `requirements.txt`
- Modify: `pet-ui/package.json`
- Verify: `sidecar/package.json`, `mobile-app/app/build.gradle.kts`, `pet-ui/src-tauri/Cargo.toml`

**Step 1: 写契约失败检查**

新增静态检查 `backend/tests/contract/test_commercial_contract.py`，断言：OpenAPI 含 pairing-code、register、devices、revoke、session、session/pending、session/sign、status、stream 共 9 个端点和全部错误码；pairing_code TTL<=300s 且最多消费一次；register 返回 one-time `credential_secret`；前端依赖无 `^/~`；Python 运行依赖为精确版本；Lucide 是唯一图标依赖。

**Step 2: 运行失败检查**

Run:
```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/contract/test_commercial_contract.py -q
```
Expected: FAIL，提示 OpenAPI/ADR 不存在及范围版本仍存在。

**Step 3: 生成契约与精确版本**

按 `docs/commercial-upgrade-SPEC.md` 第 5 节创建 OpenAPI 3.0；为 RTC 主链路、生产关闭失败、有界音频、串行状态、sidecar 守护、隐私数据分别生成 MADR。把 O-001、O-014、O-015 就地标记 RESOLVED，保留 O-003、O-016 和 TRTC Electron 注入未决项。将现有已解析版本写为精确版本，不凭空升级依赖。

**Step 4: 运行契约检查**

Run:
```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/contract/test_commercial_contract.py -q
```
Expected: PASS。

**Step 5: 提交**

```bash
git add docs/api docs/decisions requirements.txt pet-ui/package.json backend/tests/contract/test_commercial_contract.py
git commit -m "docs: lock commercial voice contracts"
```

### Task 2: 建立 SQLite 安全存储与迁移

**Files:**
- Create: `backend/app/voice/storage.py`
- Create: `backend/app/voice/migrations/001_commercial_voice.sql`
- Create: `backend/tests/unit/test_voice_storage.py`
- Modify: `backend/app/voice/config.py`

**Step 1: 写失败测试**

覆盖 `settings/device_credentials/pairing_codes/revoked_sessions/session_events/transcripts/privacy_audit_events/consumed_nonces/rate_limit_buckets` 建表、索引、唯一约束、事务回滚和 Secret 不得明文落库。`pairing_codes` 的存储层测试必须证明只保存 `code_hash`、TTL 不超过 300 秒、消费在数据库事务内原子完成、并发消费只有一个成功；数据库文件不得出现 pairing_code 或 credential_secret 明文。

```python
def test_device_secret_is_hashed_and_never_returned_from_storage(tmp_path):
    store = VoiceStore(tmp_path / "voice.db")
    store.save_device(device_id="phone-1", secret="plain-secret")
    row = store.get_device("phone-1")
    assert row.credential_hash != "plain-secret"
    assert "plain-secret" not in (tmp_path / "voice.db").read_bytes().decode("latin1")
```

**Step 2: 验证失败**

Run:
```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/unit/test_voice_storage.py -q
```
Expected: FAIL，`VoiceStore` 不存在。

**Step 3: 最小实现**

实现单连接工厂、显式事务、迁移版本表、凭证哈希、pairing code 哈希与 `consume_pairing_code()` 原子 compare-and-update、脱敏事件 JSON 验证和 TTL 清理接口。`backend/app/voice/migrations/001_commercial_voice.sql` 必须拥有 `pairing_codes` 表及 expires/consumed 索引；`storage.py` 或 `repositories/pairing_codes.py` 必须拥有创建、读取元数据、原子消费和过期清理。单文件超过 300 行前拆为 `storage.py` 与 `repositories/*.py`。

**Step 4: 验证通过**

Run 同 Step 2。Expected: PASS。

**Step 5: 提交**

```bash
git add backend/app/voice backend/tests/unit/test_voice_storage.py
git commit -m "feat: add commercial voice security storage"
```

### Task 3: 实现设备身份、nonce、限流与生产 fail-closed

**Files:**
- Create: `backend/app/voice/auth.py`
- Create: `backend/app/voice/nonce.py`
- Create: `backend/app/voice/rate_limit.py`
- Create: `backend/app/voice/errors.py`
- Create: `backend/tests/unit/test_voice_auth.py`
- Create: `backend/tests/integration/test_voice_security_routes.py`
- Modify: `backend/app/voice/config.py`
- Modify: `backend/app/api/routes_voice.py`
- Modify: `backend/app/voice/rtc_session.py`

**Step 1: 写失败测试**

覆盖：无 Bearer 返回 40101；设备凭证不能调用 sidecar 签发；nonce 重放返回 40102；超限返回 42901；userSig TTL 不超过 600 秒；生产缺 TLS/validator/rate-limit/TRTC secret 时关闭失败。

**Step 2: 运行失败测试**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/unit/test_voice_auth.py backend/tests/integration/test_voice_security_routes.py -q
```
Expected: FAIL，现有路由仍接受无认证请求。

**Step 3: 最小实现**

实现 `CredentialPrincipal(type,subject_id,credential_id)`，设备与 sidecar 独立校验；nonce 以主体 + 哈希原子消费；device/IP 双键滑动窗口或固定窗口限流；统一错误响应；生产配置校验不允许回退匿名 WS。

**Step 4: 验证通过**

Run 同 Step 2。Expected: PASS，且日志不含 credential、nonce、userSig。

**Step 5: 提交**

```bash
git add backend/app/voice backend/app/api/routes_voice.py backend/tests
git commit -m "feat: secure voice session issuance"
```

### Task 4: 实现设备列表与强一致撤销

**Files:**
- Create: `backend/app/voice/devices.py`
- Create: `backend/tests/integration/test_voice_device_routes.py`
- Modify: `backend/app/api/routes_voice.py`
- Modify: `backend/app/voice/session.py`
- Modify: `backend/app/voice/rtc_session.py`

**Step 1: 写失败测试**

覆盖 owner 生成 pairing_code、TTL<=300s、数据库只存 code_hash、并发注册只有一个成功、过期/已消费配对码拒绝、设备注册 Secret 只返回一次、列表不返回 Secret、重复撤销幂等、撤销立即拒绝 credential、活动 session 终止、userSig 指纹进入撤销表、其他设备不受影响。

**Step 2: 运行失败测试**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/integration/test_voice_device_routes.py -q
```
Expected: FAIL，端点不存在。

**Step 3: 最小实现**

按 OpenAPI 实现 pairing-code/register/list/revoke；pairing code 由本机 owner 生成，随机值只返回一次、服务端仅存哈希、TTL<=300s、原子单次消费；register 以 pairing_code 作为 bootstrap 主体并一次性返回 device Secret。撤销在单一 service 事务中更新设备、登记未过期 session、发出终止事件和写审计。跨进程终止失败时返回明确失败并保持可重试，不报告虚假成功。

**Step 4: 验证通过**

Run 同 Step 2。Expected: PASS。

**Step 5: 提交**

```bash
git add backend/app backend/tests/integration/test_voice_device_routes.py
git commit -m "feat: add device lifecycle and revocation"
```

### Task 5: 固定 20 ms PCM residue 与有界背压

**Files:**
- Create: `backend/rtc_bridge/bounded_audio_queue.py`
- Create: `backend/rtc_bridge/frame_buffer.py`
- Create: `backend/tests/unit/test_bounded_audio_queue.py`
- Create: `backend/tests/unit/test_audio_frame_buffer.py`
- Modify: `backend/rtc_bridge/config.py`
- Modify: `backend/rtc_bridge/session.py`
- Modify: `backend/rtc_bridge/shaper.py`
- Modify: `backend/rtc_bridge/health.py`

**Step 1: 写失败测试**

```python
def test_residue_is_preserved_across_chunks():
    buffer = PcmFrameBuffer(frame_bytes=640)
    assert buffer.feed(b"a" * 300) == []
    assert buffer.feed(b"b" * 340) == [b"a" * 300 + b"b" * 340]
```

再覆盖 639/640/641 bytes、会话尾部补零/丢弃、max_frames、max_bytes、最大帧龄、丢旧保新、generation flush 和指标。

**Step 2: 运行失败测试**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/unit/test_audio_frame_buffer.py backend/tests/unit/test_bounded_audio_queue.py -q
```
Expected: FAIL，组件不存在。

**Step 3: 最小实现**

实现跨块 residue；只输出完整 640-byte 帧；队列条目携带 generation、created_at、size；入队同时检查帧、字节、帧龄；过载丢旧保新并记录 high watermark/drops/backpressure。音频回调只做非阻塞投递。

**Step 4: 验证通过并压力验证**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/unit/test_audio_frame_buffer.py backend/tests/unit/test_bounded_audio_queue.py -q
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/unit/test_voice_session.py backend/tests/unit/test_voice_session_qa.py -q
```
Expected: PASS；队列峰值不超过配置，旧帧不会在 flush 后消费。

**Step 5: 提交**

```bash
git add backend/rtc_bridge backend/tests/unit
git commit -m "feat: bound realtime audio buffering"
```

### Task 6: 串行化 Android 会话生命周期

**Files:**
- Create: `mobile-app/app/src/main/java/com/jax/voice/voice/VoiceSessionLifecycle.kt`
- Create: `mobile-app/app/src/main/java/com/jax/voice/voice/VoiceSessionCoordinator.kt`
- Create: `mobile-app/app/src/test/java/com/jax/voice/voice/VoiceSessionCoordinatorTest.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/voice/VoiceForegroundService.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/net/VoiceSessionApi.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/net/RtcClient.kt`

**Step 1: 写失败测试**

覆盖 SIGNING 取消直接回 IDLE、ENTERING 取消进入 EXITING、超时作为事件、重复 start/stop 幂等、退出超时回 IDLE、旧 generation 回调丢弃、快速点击 20 次只有一个活动 session。

**Step 2: 运行失败测试**

```bash
mobile-app/gradlew.bat -p mobile-app testDebugUnitTest --tests "com.jax.voice.voice.VoiceSessionCoordinatorTest"
```
Expected: FAIL，coordinator 不存在。

**Step 3: 最小实现**

使用单 `CoroutineDispatcher`/actor 消费 `Start/SignSucceeded/EnterSucceeded/Cancel/ExitSucceeded/Timeout/Failure`；状态数据包含 generation、sessionId、error。`VoiceForegroundService` 只发送命令和渲染模型，移除 `inCall/rtcExiting` 业务协调。

**Step 4: 验证通过**

Run 同 Step 2，再运行现有 `RtcClientTest`。Expected: PASS。

**Step 5: 提交**

```bash
git add mobile-app/app/src/main mobile-app/app/src/test
git commit -m "feat: serialize Android voice lifecycle"
```

### Task 7: 解耦 Android 远端状态与播放订阅

**Files:**
- Create: `mobile-app/app/src/test/java/com/jax/voice/net/RtcPlaybackSubscriptionTest.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/net/RtcClient.kt`
- Modify: `mobile-app/app/src/test/java/com/jax/voice/net/RtcRemoteAudioStatusTest.kt`

**Step 1: 写失败测试**

断言远端 `audioStatus=2` 只发布 `RemoteAudioStopped` UI 事件，绝不调用 `muteRemoteAudio(true)`；第二轮远端开始后无需恢复订阅即可收到帧。

**Step 2: 运行失败测试**

```bash
mobile-app/gradlew.bat -p mobile-app testDebugUnitTest --tests "com.jax.voice.net.RtcPlaybackSubscriptionTest"
```
Expected: FAIL，现有代码触发 mute。

**Step 3: 最小实现**

删除正常远端停止到 mute 的映射；打断只调用显式本地播放 stop/flush 和 generation 失效，不改变长期远端订阅。

**Step 4: 验证通过**

```bash
mobile-app/gradlew.bat -p mobile-app testDebugUnitTest --tests "com.jax.voice.net.*"
```
Expected: PASS。

**Step 5: 提交**

```bash
git add mobile-app/app/src/main/java/com/jax/voice/net mobile-app/app/src/test/java/com/jax/voice/net
git commit -m "fix: keep TRTC playback subscription active"
```

### Task 8: 实现统一 VoiceUiModel 与幂等打断

**Files:**
- Create: `mobile-app/app/src/main/java/com/jax/voice/voice/VoiceUiModel.kt`
- Create: `mobile-app/app/src/test/java/com/jax/voice/voice/VoiceUiModelTest.kt`
- Create: `mobile-app/app/src/test/java/com/jax/voice/voice/BargeInControllerTest.kt`
- Create: `mobile-app/app/src/test/java/com/jax/voice/voice/VoiceEntryPointTest.kt`
- Create: `backend/tests/contract/test_voice_p0_scope.py`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/voice/VoiceState.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/voice/VoiceController.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/voice/VoiceForegroundService.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/ui/FloatingOverlay.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/MainActivity.kt`
- Modify: `pet-ui/src/state/petMachine.ts`
- Modify: `pet-ui/src/components/VoiceOrb.tsx`

**Step 1: 写失败测试**

覆盖 10 个 experienceState、合法主操作、错误模型、`speaking -> interrupted -> listening`、重复 pause/flush/interrupt 幂等、旧 generation 下行丢弃。增加静态测试禁止 UI 读取并行业务布尔。`VoiceEntryPointTest` 必须分别驱动：`MainActivity.kt` 主页面立即对话、`FloatingOverlay.kt` 轻触、`VoiceForegroundService.kt` 通知 `ACTION_TALK`；三者必须独立进入同一个 `VoiceSessionCoordinator.Start` 与 TRTC 全双工路径，任一入口失败不得破坏另外两个。`test_voice_p0_scope.py` 必须断言 P0/DoD 不含唤醒词，且主链路失败不得自动进入半双工兼容模式。

**Step 2: 运行失败测试**

```bash
mobile-app/gradlew.bat -p mobile-app testDebugUnitTest --tests "com.jax.voice.voice.*"
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/contract/test_voice_p0_scope.py -q
npm --prefix pet-ui run build
```
Expected: Android 新测试 FAIL；P0 scope 契约测试因现有 wake/自动半双工范围未被锁定而 FAIL；前端在类型收紧后 FAIL。

**Step 3: 最小实现**

建立跨端一致枚举和 reducer/machine 映射；UI 只消费聚合模型。抽取统一 `startConversation(source)` 或等价命令适配层，使主页面、悬浮球和通知 action 仅标记 source 后投递同一个 coordinator Start 事件；不得复制签发/进房逻辑。P0 路径不调用 WakeWordEngine，也不在失败分支自动调用半双工实现。打断时间戳从用户开口或点击采集，到 Android 实际 stop playback 记录完成。

**Step 4: 验证通过**

Run 同 Step 2。Expected: PASS；其中 `backend/tests/contract/test_voice_p0_scope.py` 必须实际执行且退出 0。

**Step 5: 提交**

```bash
git add mobile-app/app/src pet-ui/src backend/tests/contract/test_voice_p0_scope.py
git commit -m "feat: unify voice UI and barge in state"
```

### Task 9: 修复 sidecar SDK 基线并验证真实注入契约

**Files:**
- Create: `sidecar/test/sdk-smoke.test.js`
- Create: `sidecar/test/audio-contract.test.js`
- Create: `scripts/verify-sidecar-sdk.js`
- Modify: `sidecar/package-lock.json`
- Modify: `sidecar/main.js`
- Modify: `sidecar/rtc.js`
- Modify: `sidecar/audio.js`
- Modify: `sidecar/bridge.js`
- Modify: `sidecar/logger.js`
- Modify: `sidecar/package.json`

**Step 1: 写失败测试**

`sdk-smoke.test.js` 必须断言正式 `node_modules/trtc-electron-sdk` 可解析、原生二进制存在、运行时输出 SDK 版本，并扫描确认只有 `sidecar/rtc.js` 创建/获取 TRTCCloud 和执行 `enterRoom/sendCustomAudioData`；`audio-contract.test.js` 必须确认只有 `sidecar/audio.js` 构造 TRTCAudioFrame、格式和调用签名来自实际 SDK 包且模型侧输入始终为 640 bytes。另测 sidecar 收 SIGTERM 后 Electron 主进程退出，日志不含 Secret。

**Step 2: 运行失败测试**

```bash
npm --prefix sidecar ls --depth=0
C:/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2/node.exe scripts/verify-sidecar-sdk.js
```
Expected: 当前 `UNMET DEPENDENCY` 或正式包路径缺失，门禁 FAIL。

**Step 3: 干净安装与最小实现**

唯一实现锁定为现有 Node/Electron sidecar：`main.js` 管 Electron 生命周期，`rtc.js` 是唯一 TRTC adapter，`audio.js` 是唯一格式 adapter，`bridge.js` 只连 localhost bridge；Tauri/Rust 不实现 RTC。删除仅项目内损坏的 `sidecar/node_modules` 后按 lockfile 干净安装；不得触碰个人目录。根据实际 SDK 类型和官方签名实现版本探测与音频适配，禁止继续假定 48 kHz。正常关闭必须退出主进程。若候选 SDK 不能通过官方契约和真机门禁，停止实施并回 Phase 1 架构变更，不得自行切 native/Rust adapter。

**Step 4: 验证通过**

```bash
npm --prefix sidecar ls --depth=0
C:/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2/node.exe --test sidecar/test/*.test.js
```
Expected: PASS，报告真实 SDK 版本和注入格式。

**Step 5: 提交**

```bash
git add sidecar scripts/verify-sidecar-sdk.js
git commit -m "fix: establish executable TRTC sidecar baseline"
```

### Task 10: Tauri sidecar 服务化与 Windows 常驻能力

**Files:**
- Create: `pet-ui/src-tauri/capabilities/sidecar.json`
- Create: `pet-ui/src-tauri/src/sidecar.rs`
- Create: `pet-ui/src-tauri/src/watchdog.rs`
- Create: `pet-ui/src-tauri/tests/sidecar_supervisor.rs`
- Modify: `pet-ui/src-tauri/tauri.conf.json`
- Modify: `pet-ui/src-tauri/Cargo.toml`
- Modify: `pet-ui/src-tauri/src/main.rs`
- Modify: `pet-ui/src-tauri/src/tray.rs`
- Modify: `pet-ui/src-tauri/src/window.rs`

**Step 1: 写失败测试**

覆盖 externalBin 存在、哈希失败拒绝启动、单实例复用、固定参数 capability、崩溃有限重启、退出清理、托盘和自启开关。

**Step 2: 运行失败测试**

```bash
cargo test --manifest-path pet-ui/src-tauri/Cargo.toml sidecar_supervisor
```
Expected: FAIL，supervisor/capability 不存在。

**Step 3: 最小实现**

使用 Tauri 官方 sidecar/externalBin 机制托管 Node/Electron 打包产物；`pet-ui/src-tauri/src/sidecar.rs` 只做存在性/哈希、固定参数、单实例、watchdog、退出清理，不链接 TRTC、不处理 PCM、不得形成第二套 RTC adapter。watchdog 采用有上限退避和熔断；退出时先发优雅停止，超时后终止子进程；同一时间只有一个 sidecar owner。

**Step 4: 验证通过**

```bash
cargo test --manifest-path pet-ui/src-tauri/Cargo.toml
npm --prefix pet-ui run build
npm --prefix pet-ui run tauri build
```
Expected: PASS，release 包内 sidecar 文件名带 target triple。

**Step 5: 提交**

```bash
git add pet-ui/src-tauri pet-ui/package-lock.json
git commit -m "feat: supervise TRTC sidecar from Tauri"
```

### Task 11: 本地隐私、加密转写与脱敏诊断

**Files:**
- Create: `backend/app/voice/privacy.py`
- Create: `backend/app/voice/transcripts.py`
- Create: `backend/app/voice/diagnostics.py`
- Create: `backend/tests/unit/test_voice_privacy.py`
- Create: `backend/tests/unit/test_transcript_storage.py`
- Create: `backend/tests/unit/test_redacted_diagnostics.py`
- Modify: `pet-ui/src/components/Settings.tsx`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/SettingsActivity.kt`

**Step 1: 写失败测试**

覆盖默认无 transcripts 正文、OS-bound key 适配器、删除不留正文副本、导出只到用户路径、诊断敏感字段扫描、四类开关动作失败回滚。

**Step 2: 运行失败测试**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/unit/test_voice_privacy.py backend/tests/unit/test_transcript_storage.py backend/tests/unit/test_redacted_diagnostics.py -q
```
Expected: FAIL，service 不存在。

**Step 3: 最小实现**

服务层编排 SQLite 写入与 runtime action；密钥接口在 Windows 使用 DPAPI 适配器，测试使用内存 fake；诊断采用字段 allowlist 而非 denylist；UI 显示开关即时影响和撤销二次确认。

**Step 4: 验证通过**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/unit/test_voice_privacy.py backend/tests/unit/test_transcript_storage.py backend/tests/unit/test_redacted_diagnostics.py -q
npm --prefix pet-ui run build
mobile-app/gradlew.bat -p mobile-app testDebugUnitTest
```
Expected: PASS。

**Step 5: 提交**

```bash
git add backend/app/voice backend/tests/unit pet-ui/src mobile-app/app/src
git commit -m "feat: add local voice privacy controls"
```

### Task 12: 设计 Token、可访问性与 P0 静态门禁

**Files:**
- Create: `pet-ui/src/styles/design-tokens.json`
- Modify: `pet-ui/src/styles/tokens.css`
- Modify: `pet-ui/src/styles/global.css`
- Modify: `mobile-app/app/src/main/res/values/colors.xml`
- Modify: `mobile-app/app/src/main/res/values/dimens.xml`
- Modify: `mobile-app/app/src/main/res/values/themes.xml`
- Create: `scripts/check-ui-p0.py`
- Create: `backend/tests/contract/test_ui_p0.py`
- Modify: `pet-ui/src/components/*.tsx`

**Step 1: 写失败扫描**

扫描 `.tsx/.jsx/.vue/.html/.css/.kt/.xml`：禁止 emoji 功能图标、紫粉渐变、组件硬编码色、多个图标库、空洞文案、弹性缓动和虚假 Hero；检查交互目标 44px、focus-visible 和 reduced-motion。

**Step 2: 运行失败扫描**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/check-ui-p0.py
```
Expected: 对现有违规逐文件报行号并 FAIL。

**Step 3: 最小修复**

只修商业语音相关 UI；颜色集中到 Token；图标统一 Lucide/对应 VectorDrawable；按钮可访问名称完整；不得进行无关页面重构。

**Step 4: 验证通过**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/check-ui-p0.py
npm --prefix pet-ui run build
mobile-app/gradlew.bat -p mobile-app lintDebug testDebugUnitTest
```
Expected: PASS。

**Step 5: 提交**

```bash
git add pet-ui/src mobile-app/app/src/main/res scripts/check-ui-p0.py backend/tests/contract/test_ui_p0.py
git commit -m "feat: enforce commercial voice UI tokens"
```

### Task 13: 全链路指标、故障注入与自动化 E2E 证据包

**Files:**
- Create: `backend/app/voice/metrics.py`
- Create: `backend/tests/integration/test_voice_fault_recovery.py`
- Create: `scripts/e2e_commercial_voice.py`
- Create: `scripts/verify_commercial_evidence.py`
- Modify: `backend/app/api/routes_voice.py`
- Modify: `backend/rtc_bridge/health.py`
- Modify: `sidecar/logger.js`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/util/DiagLog.kt`
- Modify: `scripts/e2e_verify.py`

**Step 1: 写失败测试**

覆盖 `session_id/turn_id` 贯穿、首远端帧、首非零播放、帧/字节、队列、重连、错误；注入签发、进房、中继、模型、麦克风、sidecar 崩溃；验证旧 PCM 不复播。

**Step 2: 运行失败测试**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/integration/test_voice_fault_recovery.py -q
```
Expected: FAIL，指标和故障注入接口不完整。

**Step 3: 最小实现**

采用结构化事件 allowlist；证据脚本只接收可核验指标，不用“进房成功”替代播放；缺真机字段时退出非零并列出缺口。

**Step 4: 验证通过**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests -q
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/e2e_commercial_voice.py --mode simulated-faults
```
Expected: 自动化故障矩阵 PASS；真机模式在无设备时明确 BLOCKED，不能伪 PASS。

**Step 5: 提交**

```bash
git add backend sidecar mobile-app/app/src/main/java/com/jax/voice/util scripts
git commit -m "feat: add verifiable duplex voice telemetry"
```

### Task 14: 干净构建、Android 真机门禁与交付

**Files:**
- Create: `docs/release/commercial-voice-evidence-2026-08-07.md`
- Create: `docs/release/commercial-voice-runbook.md`
- Modify: `README.md`
- Modify: `mobile-app/README.md`
- Modify: `.env.example`
- Update: `.workbuddy/memory/pitfalls.jsonl` only for newly observed stable failures

**Step 1: 运行完整机械门禁**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests -q
npm --prefix pet-ui ci
npm --prefix pet-ui run build
npm --prefix sidecar ci
npm --prefix sidecar ls --depth=0
C:/Users/Administrator/.workbuddy/binaries/node/versions/22.22.2/node.exe --test sidecar/test/*.test.js
cargo test --manifest-path pet-ui/src-tauri/Cargo.toml
npm --prefix pet-ui run tauri build
mobile-app/gradlew.bat -p mobile-app clean lintDebug testDebugUnitTest assembleDebug
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/check-ui-p0.py
```
Expected: 全部退出 0；若 Gradle native lock 复发，使用隔离 `GRADLE_USER_HOME` 重跑，并在证据中区分环境失败和源码失败。

**Step 2: 执行 Android 三入口独立真机门禁**

Run:
```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/e2e_commercial_voice.py --mode android-device --entry main --rounds 1
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/e2e_commercial_voice.py --mode android-device --entry overlay --rounds 2
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe scripts/e2e_commercial_voice.py --mode android-device --entry notification --rounds 1
```
Expected: 主页面、悬浮球、前台通知 action 均能独立进入同一个 TRTC 全双工状态机；每个入口都有非零采集、上行、下行、远端首帧、首非零播放和扬声器路由证据。悬浮球路径连续两轮均可听。关闭或故障注入任一入口后，另外两个入口仍能发起；日志证明没有进入 WakeWordEngine 或自动半双工 fallback。

**Step 3: 执行打断与恢复矩阵**

分别验证开口打断、点击打断、连续三次打断、暂停/恢复、退出/重进、断网、锁屏/后台、sidecar 崩溃、设备撤销。Expected: 打断 P95 不超过 300 ms；任何结束路径回 IDLE；撤销后旧 credential/userSig/WS 被拒绝。

**Step 4: 生成证据和运行手册**

证据文档只记录命令、退出码、版本、哈希、`session_id`、指标摘要和阻断项，不写 Secret、nonce、原始音频、截图、代码或完整转写。README 与当前拓扑、版本和限制一致。

**Step 5: 最终独立 QA**

使用 `mvp-dev-expert-team-qa` 读取：
- `references/01-standards/test-discipline.md`
- `references/01-standards/test-integrity-anti-gaming.md`
- `references/01-standards/verifier-critic-pattern.md`
- `references/01-standards/generated-code-failure-modes.md`
- `references/01-standards/production-readiness-scorecard.md`

QA 必须输出 RoleVerdict。P0 缺陷不为零时保持 FAIL，不进入部署。

**Step 6: 提交**

```bash
git add docs/release README.md mobile-app/README.md .env.example .workbuddy/memory/pitfalls.jsonl
git commit -m "docs: publish commercial voice evidence"
```

## Final Delivery Criteria

只有同时满足以下条件才能把实现裁决从 FAIL 改为 PASS：

1. OpenAPI、ADR、精确依赖和锁文件通过契约检查。
2. 生产认证、nonce、防重放、限流、TLS 和 fail-closed 全部通过。
3. bridge 固定 20 ms、有界队列和迟到 generation 丢弃通过压力测试。
4. Android 生命周期不再依赖业务布尔竞态，正常远端停止不静音订阅。
5. sidecar SDK 干净安装、运行版本、真实注入签名和退出清理通过。
6. Tauri externalBin、最小 capability、单实例、自启、托盘和 watchdog 通过安装后验收。
7. P0 UI 静态扫描、构建、lint、类型检查、单测和故障矩阵全绿。
8. 至少一台 Android 真机连续两轮均有非零扬声器播放证据，打断 P95 不超过 300 ms。
9. QA RoleVerdict 为 pass，P0 缺陷为零。
