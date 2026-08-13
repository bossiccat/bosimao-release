# 波斯猫双工语音完整审计报告

- 审计日期：2026-08-07
- 审计范围：Android App、TRTC Android/Electron、Electron sidecar、rtc_bridge、会话签发、测试与交付链路
- 当前基线：`master`，工作区有未提交修改；本次只读审计，未修改业务代码
- 总体裁决：**FAIL，不具备发布条件**
- 生产就绪评分：**42/100（原型可继续验证，不是成熟可交付方案）**

## 一、结论摘要

问题不是单一网络故障，也不是缺少耳机外设。当前故障由四层问题叠加：

1. sidecar 核心 TRTC SDK 声明已升级，但实际依赖缺失，当前环境无法启动可信的 13.4 音频链路。
2. Android 会话取消存在确定性死锁，能直接复现“上一会话失败/一直连接中”。
3. Android 把远端静音状态实现成 `muteRemoteAudio(true)`，可能把后续 AI 回复永久挡住。
4. 下行 PCM 没有保证跨模型块连续、固定 20ms 帧；现有 E2E 也没有验证 Android 真正收到并播放远端音频。

因此，继续围绕采样率、解除静音或超时逐个补丁，不足以让方案成熟。应先建立可执行基线，再修状态机和音频契约，最后用真实跨端音频断言验收。

## 二、阻断项

### P0-1 sidecar 核心 SDK 实际缺失

**证据**

- `sidecar/package.json:12-16` 声明 `trtc-electron-sdk: 13.4.802-beta.3` 和 `electron: 31.7.7`。
- `sidecar/package-lock.json:698-703` 记录该 SDK，但条目被标记为 `extraneous`。
- 实际文件 `sidecar/node_modules/trtc-electron-sdk/package.json` 不存在。
- 机械验证：`npm --prefix sidecar ls --depth=0` 返回 `UNMET DEPENDENCY trtc-electron-sdk@13.4.802-beta.3`，退出码 1。

**影响**

无法证明 Electron 13.4 SDK 已被加载，也无法运行可信的 Electron→Android 跨端验证。当前“13.4 已对齐”只停留在依赖声明层。

**验收期望**

使用锁文件干净安装成功；`npm ls` 退出码 0；运行时日志输出实际 `getSDKVersion()`；sidecar 冒烟启动成功。

### P0-2 会话签发期间取消会造成永久退出锁

**证据**

- `VoiceForegroundService.kt:270-285` 启动同步签发请求，取消协程不能中断阻塞 HTTP。
- `VoiceForegroundService.kt:299-306` 用户再次点击时将 `inCall=false`、`rtcExiting=true`，然后调用 `rtcClient.exitRoom()`。
- `RtcClient.kt:329-333` 此时尚未进房，`exitRoom()` 因 `!inRoom` 直接返回。
- `VoiceForegroundService.kt:310-320` 只有 `onCallExited()` 才会把 `rtcExiting=false`；上述路径没有任何回调会触发它。
- `VoiceForegroundService.kt:228-233` 后续所有唤醒都因 `rtcExiting=true` 被拒绝。

**可复现路径**

点击“立即对话”后，在会话签发返回前再次点击。服务进入永久 `rtcExiting`，后续提示“正在退出上一会话”，除非重启服务。

**验收期望**

签发阶段取消与已进房退房使用不同状态；未进房时直接完成本地取消并恢复监听；用单一串行状态机覆盖 `IDLE → SIGNING → ENTERING → IN_ROOM → EXITING`。

### P0-3 远端静音事件错误改变播放订阅

**证据**

- `RtcClient.kt:231-264` 将 `audioStatus=2` 解释为远端停止，并调用 `muteRemoteAudio(userId, true)`。
- 恢复依赖后续 `audioStatus=1`、`onFirstAudioFrame` 或 `onUserAudioAvailable(true)` 再解除静音，见 `RtcClient.kt:177-185,218-228,241-260`。
- 但静音后 SDK 是否仍发出足以触发解除静音的事件没有真实跨端证据。
- `RtcRemoteAudioStatusTest.kt:73-92` 反而把“静音必须调用 muteRemoteAudio”写成测试期望，没有断言下一句能恢复播放。

**影响**

直接解释“已经连接、第一句或后续句无声音”。UI 状态转换不应通过修改远端订阅/播放状态实现。

**验收期望**

远端状态只驱动 UI；正常回复结束不调用远端 mute。打断应由显式会话协议或播放控制完成，并测试连续两轮回复均可听。

### P0-4 缺少真实双工 E2E 验收

**证据**

- `scripts/e2e_verify.py:1-4,52-194` 只验证后端健康、状态、配置热重载、提醒事件与推送，不涉及 Android/TRTC/PCM。
- Android 仅有 mock SDK 单测；`RtcClientTest.kt:19-31` 明确不连接真实 RTC 云。
- 没有测试同时断言：手机上行 PCM 到达 sidecar、APM 产生下行、sidecar 注入 TRTC、Android 收到远端音频帧、播放路由非静音。

**影响**

现有测试全绿也无法证明用户核心流程可用。此前 Electron phone 收到音频只能证明 Electron↔Electron，不能证明 Electron→Android。

**验收期望**

发布门禁必须包含 Android 模拟器/真机跨端 E2E，至少连续两轮语音：

- Android 本地采集 RMS > 0；
- sidecar `upFrames/upBytes` 增长；
- bridge `downFrames/downBytes` 增长；
- Android 出现 `onUserAudioAvailable(true)` 与远端 PCM/首帧事件；
- 通过音频帧回调或系统回环形成非零播放证据；
- 第二轮仍可播放，覆盖静音恢复与会话重进。

### P0-5 公开签发接口缺少身份认证

**证据**

- `routes_voice.py:133-177` 的 `/api/v1/voice/session` 与 `/session/sign` 只校验 ID 格式并直接签发。
- 请求模型 `routes_voice.py:39-50` 没有设备凭证、sidecar token、签名、nonce 或重放防护。
- sidecar 使用公开云函数域名，见 `rtc_bridge/server.py:28-31,185-193`。

**影响**

任何能访问端点的人都可为任意合法格式的 `device_id/user_id` 申请短期 TRTC 凭证，可能进入房间、抢占身份或消耗资源。

**验收期望**

手机设备注册凭证 + sidecar 服务身份认证；限流；nonce/短期请求签名；签发审计日志；禁止客户端任意指定特权 userId。

## 三、重要问题

### P1-1 下行整形器会发送不足 20ms 的短帧

- `shaper.py:64-66` 对每个模型块独立切片，尾部不足 640B 也直接发送。
- `rtc.js:79-88` 对每个收到的块重采样后立即调用 `sendCustomAudioData`。
- 没有跨块尾帧缓存，也没有对短帧补齐或合并。

自定义音频采集应维持连续固定节拍。需要在 shaper 内维护 residue buffer，只发送完整 20ms 帧；流结束时按契约丢弃或补静音，并测试任意块边界。

### P1-2 sidecar 正常退出留下 Electron 主进程壳

- `rtc.js:209-215` 正常退出仅调用 `window.close()`。
- `main.js:46-49` 明确配置全部窗口关闭后主进程不退出。
- 只有 renderer 崩溃/无响应时才 `app.exit(1)`，见 `main.js:29-39`。

结果是 hold 超时、控制退出或 phone 测试结束后可能残留主进程；看门狗再拉起新实例，重现僵尸和多实例抢连接。应通过 IPC 让主进程执行 `app.exit()`，并用 PID/互斥锁保证单实例。

### P1-3 PCM 上下行队列无界，缺少背压

- `session.py:57,85-99` 上行使用无界 `asyncio.Queue`。
- `shaper.py:28,37-41` 下行同样使用无界队列。

模型、网络或 WebSocket 变慢时会积压原始 PCM，带来内存增长和越来越旧的语音延迟。需要有界队列、延迟预算、丢弃策略和指标告警。

### P1-4 唤醒词默认关闭，与产品承诺冲突

- `VoiceConfig.kt:95-104` 明确 `WAKE_DEFAULT_ENABLED=false`。
- `VoiceConfig.kt:26-35` 配置迁移会把该默认值写入本地配置。
- `VoiceForegroundService.kt:142-158` 关闭时完全不构造唤醒引擎。

默认安装后说“波斯猫”不会触发是当前设计的确定行为。要么修复 KWS 并默认开启，要么 UI 和交付文档明确降级为手动对话，不能同时宣称语音唤醒已完成。

### P1-5 文档与可构建性漂移

- `mobile-app/README.md:1-5` 仍写 0.1.0 与旧 WS 架构。
- `README.md:68-78` 声称可运行 `./gradlew`，但仓库没有 `gradlew/gradlew.bat`。
- 实现已经是 TRTC 0.6.5，README 大量章节仍描述废弃的 LAN/RELAY/E2EE。

需要把构建入口、运行拓扑、当前版本、配置、验证命令和已知限制更新成当前事实；CI 必须从干净检出执行。

## 四、验证结果

| 项目 | 结果 | 说明 |
|---|---|---|
| 后端测试 | PASS | 项目 `.venv` 执行，347 passed，2 warnings，27.28s |
| rtc_bridge Python 语法 | PASS | `python -m compileall -q backend/rtc_bridge` |
| sidecar JS 语法 | PASS | `node --check sidecar/rtc.js` |
| sidecar 依赖完整性 | FAIL | `UNMET DEPENDENCY trtc-electron-sdk@13.4.802-beta.3` |
| Android 构建/单测 | FAIL（环境阻断） | 使用 Gradle 8.7 + JDK 17 + `--offline` 执行 `:app:testDebugUnitTest :app:assembleDebug`；Gradle 在 native services 初始化阶段因 `native-platform.dll.lock` 拒绝访问退出，未进入编译、单测或打包阶段 |
| Android APK 新产物 | 未生成 | 工作区可见 APK 属于历史 `build`/未提交产物；本次失败命令未生成可归因于当前源码的 APK |
| Android→sidecar 上行 | 历史局部通过 | 旧日志证明手机上行帧流动；本次未形成可重复的干净构建后复测 |
| sidecar→Android 下行 | FAIL/无证据 | Android 无远端事件；13.4 Electron SDK 实包缺失，无法完成可信跨端复测 |
| 连续两轮双工 | 未覆盖 | 现有测试没有真实 RTC 跨端断言；Electron 模拟对端不等于 Android 扬声器闭环 |
| 唤醒词 | FAIL（默认路径） | `WAKE_DEFAULT_ENABLED=false`，默认未构造 KWS 引擎 |

## 五、成熟度评分

| 维度 | 分数 | 结论 |
|---|---:|---|
| 功能正确性 | 8/25 | 核心下行与会话恢复未闭环 |
| 可靠性与生命周期 | 7/20 | 存在退出死锁、僵尸进程、无界队列 |
| 测试可信度 | 8/20 | 后端测试扎实，但核心双工没有真实 E2E |
| 安全性 | 6/15 | TRTC 传输有基础保障，签发接口未认证 |
| 可构建与可运维 | 7/10 | 依赖和 wrapper 不完整，文档漂移 |
| 可观测性 | 6/10 | 已有 DiagLog/统计，但缺统一 session correlation 与播放证据 |
| **总分** | **42/100** | **原型阶段，不可发布** |

## 六、整改顺序

1. **恢复干净可执行基线**：锁文件干净安装 sidecar 依赖；补 Gradle wrapper；CI 从零安装并构建。
2. **重写会话状态机**：单一串行状态，先修签发期间取消死锁，再覆盖快速点击、超时、重进和服务停止。
3. **移除正常静音的 `muteRemoteAudio(true)`**：UI 状态与音频订阅分离，增加连续两轮回复测试。
4. **修复固定帧契约**：16k 模型输出跨块缓存为 20ms，再重采样成 48k 20ms；禁止短帧。
5. **修复 sidecar 进程闭环**：正常退出主进程、单实例锁、统一看门狗所有权。
6. **补有界队列与延迟策略**：上/下行按实时音频原则丢旧保新，暴露 queue depth 与 age。
7. **给签发接口加认证与限流**。
8. **建立真正发布门禁**：模拟器用于状态机与网络扰动，至少一台真机用于声学播放闭环；连续两轮双工通过后才能发版。

## 七、RoleVerdict

```yaml
verdict: fail
blocking:
  - 违反项: sidecar 核心 SDK 未安装
    证据: npm ls 返回 UNMET DEPENDENCY，运行时包文件不存在
    期望: 干净安装、运行时版本证据、sidecar 启动通过
  - 违反项: 会话签发取消造成 rtcExiting 永久锁
    证据: VoiceForegroundService.kt:270-320 + RtcClient.kt:329-333
    期望: 未进房取消立即恢复，状态机串行且可测试
  - 违反项: 远端停止事件错误调用 muteRemoteAudio(true)
    证据: RtcClient.kt:231-260
    期望: UI 状态不改变订阅，连续两轮回复可播放
  - 违反项: 核心双工没有真实 E2E 验收
    证据: scripts/e2e_verify.py:1-194 与 Android mock 单测范围
    期望: Android/TRTC/sidecar/bridge/APM 全链路机械证据
  - 违反项: 公开签发接口无身份认证
    证据: routes_voice.py:39-50,133-177
    期望: 设备和 sidecar 认证、限流与防重放
advisory:
  - 建议项: 固定 20ms PCM 帧并跨块缓存尾帧
    理由: 当前短帧会破坏实时注入节拍
  - 建议项: sidecar 正常退出主进程并加单实例锁
    理由: 防止僵尸和多实例抢连接
  - 建议项: 上下行队列增加背压和延迟预算
    理由: 防止内存增长与陈旧语音堆积
evidence:
  - artifact_ref: mobile-app/app/src/main/java/com/jax/voice/voice/VoiceForegroundService.kt
    line: 270
    说明: 签发、取消、退出锁路径
  - artifact_ref: mobile-app/app/src/main/java/com/jax/voice/net/RtcClient.kt
    line: 231
    说明: 远端状态驱动 muteRemoteAudio
  - artifact_ref: sidecar/package-lock.json
    line: 698
    说明: SDK 锁记录与 extraneous 状态
  - artifact_ref: backend/rtc_bridge/shaper.py
    line: 64
    说明: 短尾帧直接发送
  - artifact_ref: backend/app/api/routes_voice.py
    line: 133
    说明: 无认证签发路由
  - artifact_ref: scripts/e2e_verify.py
    line: 52
    说明: 现有 E2E 不覆盖语音链路
```

## 八、审计限制

本轮业务代码保持不变。Android 构建已使用已知的 Gradle 8.7、JDK 17 和离线缓存发起复测，但 Gradle 尚未进入项目配置阶段，就在 native services 初始化时因 `native-platform.dll.lock` 文件拒绝访问退出；因此本轮没有新的 Android 编译、单测或 APK 产物证据，历史 APK 不作为当前构建通过证据。sidecar 的 `trtc-electron-sdk` 实包仍不存在，无法完成可信的 Electron 13.4 跨端复测。真机声学播放、WiFi/4G/断网重连和连续两轮双工门禁仍未执行。

专家裁决仅采信带有 `verdict/blocking/advisory/evidence` 结构和具体 artifact 引用的独立回传；未回传或上游不可用的专家不会被伪造成通过。当前阻断结论由源码、依赖完整性检查、测试范围检查和本次 Gradle 失败堆栈共同支撑。
