# 方案 B 商业化双工语音实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将“退下→本地 KWS 待命→重新唤醒→新 generation 重进房”的方案 B 实现为唯一商业状态机，并用真实云端、真机和可归因发布候选完成 Gate-0～Gate-6；`RECOVERY_SPEECH` 在 MVP 中默认保持 `UNSUPPORTED/EvidencePending`，不得阻塞或掩盖主链 P0。

**Architecture:** Android 在 `IN_ROOM` 时将麦克风和音频焦点独占交给 TRTC；收到“退下”后进入 `EXITING`，依次确认 Android exitRoom、sidecar close、rtc_bridge drain、APM stop、Brain turn cancel/finalize，确认全部完成后才进入 `STANDBY_LOCAL/KWS_READY`。KWS 命中“波斯猫”后使用新的 nonce、短期 userSig、session_id、room binding 和 generation 重新签发/进房。所有旧 generation 媒体、控制、文本和副作用 fail-closed 丢弃。Brain 只接受可信 `speaker=user` 最终 transcript，并要求 `session_id/generation/turn_id/idempotency_key`。

**Tech Stack:** Android Kotlin、LiteAVSDK_TRTC `13.4.0.20477`、Windows Node/Electron sidecar、Python `rtc_bridge`、MiniCPM-o Realtime/APM、商业控制面 OpenAPI `docs/api/commercial-voice-openapi.yaml`、CloudBase/TRTC 运行链。具体实现必须以仓库已锁定版本和实际 artifact 为准。

---

## Task 1: 冻结候选边界与证据目录

**Files:**
- Modify: `docs/api/commercial-voice-openapi.yaml`
- Modify: `docs/governance/release-harness.md`
- Modify: `governance/release-policy.json`
- Create: `outputs/evidence/<candidate-id>/manifest.json`
- Test: `scripts/` 现有 Gate-0 采集命令

**Steps:**
1. 在 clean tagged commit 上创建唯一 candidate-id；禁止把当前 dirty 工作树直接当候选。
2. manifest 绑定 Git commit/tag、APK versionCode/versionName、APK SHA-256、签名证书 SHA-256、sidecar bundle hash、rtc_bridge tree/package hash、CloudBase function revision/bundle hash、OpenAPI revision、依赖锁、设备/OS/TRTC SDK 版本和测试日期。
3. 将主控制面唯一规范锁定为 `commercial-voice-openapi.yaml`，记录旧 `docs/openapi.yaml` 的 wake/suspend/half-duplex/relay 语义为废弃或隔离，禁止 P0 隐式 fallback。
4. 运行 `git rev-parse HEAD`、`git status --porcelain=v1`、`git diff --check`、tag 检查；任一 dirty/untagged/未跟踪发布件即 FAIL。
5. 验收：manifest 可从源码和产物 hash 双向追溯；当前工作树不能升级为 release candidate。

## Task 2: 扩展商业控制面契约

**Files:**
- Modify: `docs/api/commercial-voice-openapi.yaml`
- Test: 新增控制面 schema/contract tests

**Steps:**
1. 为 `session.terminate` 定义幂等 request：`session_id/device_id/room_id/generation/request_id/reason=user_standby`。
2. 为 `session.terminated` 定义 terminal acknowledgement：`android_exit_room/sidecar_closed/bridge_drained/apm_stopped/brain_cancelled_or_finalized` 全部布尔确认；部分失败返回可重试错误，不得报告成功。
3. 为 wake/re-enter 定义一次性 `wake_event_id`、`X-Request-Nonce`、新 `session_id`、短期 `userSig`、`generation=n+1` 和 session-bound room binding。
4. 扩展 WS hello：`session_id/device_id/room_id/generation/proof/nonce`；服务端必须校验 proof、nonce 原子消费、hello timeout 和 generation。
5. 增加状态与错误码：`STANDBY_LOCAL/KWS_READY/ERROR`、`KWS_UNAVAILABLE/EXIT_TIMEOUT/TERMINATION_PARTIAL/REENTER_TIMEOUT/STALE_GENERATION/SESSION_TERMINATED/RESIDUAL_MEDIA/RESIDUAL_SIDE_EFFECT/SPEAKER_UNTRUSTED`。
6. 扩展 telemetry schema：`session_id/turn_id/generation/wake_event_id/exit_latency_ms/reenter_latency_ms/first_audio_frame_ms/first_playable_frame_ms/barge_in_ms/old_generation_drop_count/brain_side_effect_count`。
7. 运行 schema 校验和未知字段/旧 generation/nonce 重放黑盒测试。

## Task 3: Android 方案 B 状态机与 KWS 所有权

**Files:**
- Modify: `mobile-app/app/src/main/java/com/jax/voice/voice/VoiceForegroundService.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/voice/VoiceSessionCoordinator.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/config/VoiceConfig.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/voice/WakeWordEngine.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/voice/FrameDispatcher.kt`
- Modify: `mobile-app/app/src/main/java/com/jax/voice/net/RtcClient.kt`
- Test: `mobile-app/app/src/test/...` 对应 coordinator/state/KWS contract tests

**Steps:**
1. 先写失败测试：`IN_ROOM → EXITING → STANDBY_LOCAL/KWS_READY → SIGNING(new generation) → ENTERING → IN_ROOM`；`EXITING` 不得映射成 `CONNECTING`。
2. 为“退下”实现单飞终止：停止新 turn，发起 `exitRoom`，等待 terminal ack；退出超时/部分失败进入可见 `ERROR`，不可启动 KWS。
3. 将 `VoiceConfig.kt` 的默认待命可唤醒策略改为以 readiness 为准；KWS disabled/unavailable/error 必须 fail-closed 并可见报错，不得静默假装 ready。
4. 终止确认后启动/恢复本地 KWS；KWS 独占音频焦点与 MicRecorder；成功签发并进房后才停止 KWS、将麦克风移交 TRTC。
5. 命中“波斯猫”使用单飞 `wake_event_id`，generation 原子递增，fresh nonce/userSig/session/room；重复命中和旧回调只计 drop。
6. 将 UI 映射修正为 `EXITING/ENDING` 和 `STANDBY_LOCAL/KWS_READY`；失败显示明确错误与重试/手动入口，不显示 CONNECTING 假象。
7. 加入 App kill/restart、锁屏后台、权限变化、KWS 初始化失败、exit timeout、sign/enter failure 的 fail-closed 测试。
8. 运行 Android unit/lint/build；禁止通过 skip/xfail/only/focus 弱化测试。

## Task 4: Sidecar / rtc_bridge terminal drain 与代际隔离

**Files:**
- Modify: `sidecar/bridge.js`
- Modify: `sidecar/rtc.js`
- Modify: `backend/rtc_bridge/server.py`
- Modify: `backend/rtc_bridge/session.py`
- Modify: `backend/app/voice/apm_bridge.py`
- Modify: `backend/app/voice/apm_handshake.py`
- Modify: `backend/app/brain/voice_intent_router.py`
- Modify: `backend/rtc_bridge/main.py`
- Test: `backend/tests/integration/`、`backend/tests/unit/`

**Steps:**
1. 先写失败测试：`ACTIVE(n) → DRAINING(n) → CLOSED` 后所有上/下行帧、控制、APM 和旧 callback 均被拒绝并计数。
2. termination 到达后立即封闭输入、停止输出、清空有界上下行队列、取消 APM、取消或最终化未完成 Brain turn；terminal ack 只有全部确认才返回成功。
3. hello 强制校验 session-bound proof、nonce、device/session/room/generation；旧连接和迟到帧 fail-closed。
4. 为每帧和每个 turn 增加 generation/sequence/turn_id/response_id；跨代帧必须 drop 并计数。
5. 移除 `[STANDBY]/[ACTIVE]` 的商业控制效力；marker 仅可作为内容展示或默认关闭调试兼容路径。
6. 修复 APM 握手失败的 finally close；确认官方 MiniCPM-o 版本、wire schema、输入采样率、输出事件、turn end/commit 语义；未确认前不得宣称 200ms 性能修复。
7. 将 `EndDetectFeeder` 的 wall-clock 改为 monotonic/cancellable endpoint；以真实协议替代未经证明的 1.2s silence + 2s pad。
8. Brain 只接受可信 `speaker=user` 最终 transcript；assistant 文本永不进入 intent；增加 outbox/ack/idempotency/recovery，避免先清 buffer 后静默丢任务。
9. 200ms 与 1s 做同设备、同语料真实 MiniCPM-o A/B；记录 speech_start、last_speech、input_append、first_response_audio、first_play、错误率、队列丢帧/背压 P50/P95；无真实证据保留 feature flag 和 1s 回退。
10. 运行后端 lint/type/unit/integration；失败必须记录根因并修复，不得以 mock 结果替代外部协议证据。

## Task 5: RECOVERY_SPEECH 技术 spike（默认不进入 MVP）

**Files:**
- Create: `docs/spikes/recovery-speech-liteavsdk-130420477.md`
- Inspect only: 锁定 `LiteAVSDK_TRTC:13.4.0.20477` AAR/JAR API
- Test: 独立 Android prototype/spike tests，不能修改主链

**Steps:**
1. 核验锁定 SDK 是否支持 custom audio/render、回调线程、自动播放互斥、蓝牙/AudioFocus/锁屏/underrun。
2. 若可行，设计独立 PCM sink、bounded queue、`AudioTrack` renderer、`PlaybackParams` pitch=1.0；禁止在 TRTC PCM 回调中修改/耗时。
3. 原子递增 generation，拒绝旧帧，清空 bounded queue，`AudioTrack.flush()`；profile 绑定 session/generation/turn/response。
4. 只验证 MVP `RECOVERY_SPEECH`：首条 response，从 first_playable_frame 起最多 4 秒或 2 短句；默认 0.90x，范围 0.85x–1.00x；无能力则记录 `UNSUPPORTED` 并回退 1.00x。
5. 记录首帧、首可播放帧、实际语音时长/音节速率、处理耗时、队列等待、AudioRoute/AudioFocus/underrun、barge-in P50/P95；慢速不得使首可播放帧恶化超过 50ms。
6. 任何 spike 结果不得升级为产品 Claim，除非通过 Gate-4 真机对照和独立 reviewer 签核。

## Task 6: Brain 语义边界与幂等恢复

**Files:**
- Modify: `backend/app/brain/voice_intent_router.py`
- Modify: `backend/rtc_bridge/main.py`
- Modify: `docs/api/commercial-voice-openapi.yaml`
- Test: assistant-command black-box tests、replay/out-of-order/idempotency tests

**Steps:**
1. 写失败测试：assistant 回复含“执行/重构/帮我”等词时零任务。
2. 仅允许已认证、当前 generation、`speaker=user`、有 `turn_id` 和 `idempotency_key` 的最终用户 transcript进入 Brain。
3. callback 成功前不清空可恢复 buffer；收到 task_id/ack 后才标记投递完成；失败进入 outbox/recovering，不盲重 POST。
4. termination 时取消或终结未完成 Brain turn，`brain_side_effect_count` 可证明为零或已最终化。
5. 运行黑盒回放、乱序、重复请求和网络超时测试。

## Task 7: 真实 Gate-3 / Gate-4 证据采集

**Files:**
- Create: `outputs/evidence/<candidate-id>/gate-3/`
- Create: `outputs/evidence/<candidate-id>/gate-4/`
- Test: 受控 Windows、Android 真机、真实 MiniCPM-o、packaged sidecar

**Steps:**
1. Gate-3：生成 release APK，执行 zipalign、apksigner、SHA-256、安装验证；保存完整原始输出和 provenance。
2. 绑定 APK、sidecar、bridge、API/OpenAPI revision、设备/OS/TRTC SDK、网络条件为同一 evidence manifest。
3. Gate-4 三入口：main/overlay/notification 各连续两轮；保存 session_id、turn_id、generation、RMS、up/down frames+bytes、remote first frame、first playable/nonzero playback、AudioRoute、AudioFocus、人工可听或等价播放证据。
4. 验证方案 B 同轮事件链、退下后普通语音零响应、再次“波斯猫”新 generation 重进房、旧 PCM 丢弃和零 Brain side effect。
5. 正常语速与恢复慢速分别进行开口/点击 barge-in 各至少 10 次，P95≤300ms；第二轮及后续恢复正常语速。
6. 覆盖 KWS disabled/unavailable/error、exit timeout、sign/enter failure、网络切换、userSig expired、App kill/restart、锁屏后台、RTC/APM/sidecar 重启、重复 wake；全部 fail-closed。
7. 任何日志“play started”不能替代实际非零扬声器/AudioRoute/AudioFocus 证据。
8. 独立 reviewer 签核后，才能变更 `android-duplex-audio` 和 `windows-popup-free` Claim 状态；否则保持 EvidencePending。

## Task 8: Gate-0～Gate-6 发布安全与回滚

**Files:**
- Modify: `.github/workflows/android-gates.yml`
- Modify: `scripts/jax-services.ps1`
- Modify: `scripts/jax-watchdog.ps1`
- Modify: `scripts/install-scheduled-tasks.ps1`
- Create: `delivery/README.md`, `delivery/DEPLOY.md`, `delivery/TEST_REPORT.md`, `delivery/USER_GUIDE.md`, `delivery/.env.example`
- Test: 受控发布机与干净 Windows 机

**Steps:**
1. Gate-0：clean tagged commit、唯一 candidate manifest、每个发布件 hash、签名连续性、无未跟踪发布件。
2. Gate-1：采集实际 PID/父 PID/完整命令行、端口 owner、health run_id、计划任务结果；受控 reboot 后重复并检查无重复 room/intent/side effect。
3. Gate-2：凭证/TLS/nonce/限流/最小权限缺失时 fail-closed；禁止开发态 `VOICE_TOKEN`/relay E2EE 警告即启动。
4. Gate-5：记录 CloudBase function revision、bundle hash、生产 custom domain/certificate fingerprint、数据库索引/备份/恢复、N→N-1 回滚。
5. Gate-6：自包含交付、每日备份和恢复演练、告警送达、上一版本 artifact/manifest 可回滚。
6. 重新执行 Gate-0～Gate-6；任何一个 P0 fail 或 EvidencePending 均保持 NO-GO。

## Task 9: 独立验证与收尾

**Files:**
- Create: `outputs/evidence/<candidate-id>/final-verdict.json`
- Modify: `outputs/commercial-readiness-no-go-2026-08-23.md`

**Steps:**
1. QA 使用 held-out 速度/代际用例复测；实现 diff 与测试 diff 分离，禁止 skip/xfail/only/focus 弱化。
2. DevOps 复核 manifest、签名、运行态、回滚和告警证据。
3. 前端复核 TRTC renderer、AudioRoute/Focus、旧帧 flush 和实际播放；后端复核协议、Brain、APM、端到端延迟。
4. 独立 reviewer 根据 Gate-0～Gate-6 给出结构化 `verdict`；未全通过则写明 blocking、证据和期望，维持 NO-GO。
5. 只有全部 P0 关闭、`android-duplex-audio` 与 `windows-popup-free` Claim 通过签核后，才可申请商业发布复评。
