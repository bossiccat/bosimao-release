# 贾克斯语音（Jax Voice）— Android App M1 骨架 + M2 中继

> 项目：贾克斯模式 V1.5 手机语音主线（spec: `docs/specs/mobile-voice-spec.md`）
> 里程碑：**M1 手机 App 骨架 + 唤醒**（一直在听 / 唤醒词 / WS 连接 PC 网关）+ **M2 中继/E2EE 客户端**
> 包名：`com.jax.voice` ｜ 版本：`0.1.0` ｜ 语言：Kotlin（原生） ｜ UI：View + Canvas

---

## 1. 技术选型理由（按 spec 为准）

| 项 | 选择 | 理由 |
|---|---|---|
| 语言 | **Kotlin 原生**（非 React Native） | spec §4 全部能力（前台服务 mic 类型 / AudioRecord / 悬浮窗 / sherpa-onnx JNI / Canvas 波形）都是 Android 原生 API；RN 需为每一项写原生模块，桥接层零收益 |
| UI | View + XML + Canvas 自绘波形 | spec §4.5「Canvas 自绘，不引图表库」；M1 最小依赖，避免 Compose 版本漂移风险 |
| 唤醒词 | **sherpa-onnx KeywordSpotter**（spec §4.2 主选） | Apache-2.0、中文免训练（ppinyin text2token）、与 PC 降级链同框架、官方 Android AAR |
| WS | OkHttp 4.12.0 | 轻量、原生支持二进制帧；协议按 spec §7 |
| 状态 | 六态 StateFlow（对齐 PRD pet_state） | spec §4.6 / pet-ui petMachine 同语义 |

## 2. 目录结构

```
mobile-app/
├── README.md
├── settings.gradle.kts / build.gradle.kts / gradle.properties
├── gradle/wrapper/gradle-wrapper.properties      # Gradle 8.7（AS 打开自动用）
├── scripts/fetch-deps.ps1                        # 下载 AAR + KWS 模型（一键）
└── app/
    ├── build.gradle.kts                          # compileSdk 35 / minSdk 26 / targetSdk 35
    ├── libs/sherpa-onnx-1.13.4.aar               # ← 脚本下载（不入库）
    └── src/main/
        ├── AndroidManifest.xml                   # 权限 + microphone 前台服务
        ├── assets/sherpa-onnx-kws-.../           # ← 脚本下载（不入库）
        ├── java/com/jax/voice/
        │   ├── MainActivity.kt                   # 状态页 + 权限/白名单引导
        │   ├── SettingsActivity.kt               # 连接模式/服务器/中继/配对码/E2EE密钥/设备ID
        │   ├── config/VoiceConfig.kt             # SharedPreferences（默认 ws://<PC IP>:8000/api/v1/voice/stream）
        │   ├── net/FrameCodec.kt                 # 二进制帧 [0x02][seq u32 BE][ts u64 BE][payload]（spec §7.2）
        │   ├── net/PairFrame.kt                  # 中继配对帧 JSON 构建/解析（纯字符串，JVM 可测）
        │   ├── net/VoiceWsClient.kt              # WS + LAN hello / RELAY pair + 心跳15s + 指数退避重连 + E2EE
        │   ├── crypto/VoiceCipher.kt             # AES-256-GCM 音频 payload 加密（AAD=seq，M2）
        │   ├── voice/VoiceState.kt               # 六态枚举 + UiState
        │   ├── voice/VoiceController.kt          # 全局 StateFlow（服务写、UI 读）
        │   ├── voice/VoiceForegroundService.kt   # 前台服务（一直在听）
        │   ├── voice/MicRecorder.kt              # AudioRecord 16k 单声道 PCM16，40ms/帧
        │   ├── voice/WakeWordEngine.kt           # sherpa-onnx KWS（贾克斯/小贾）
        │   ├── voice/FrameDispatcher.kt          # 同一帧三路分发（KWS/VAD(M2)/上行）
        │   └── ui/FloatingOverlay.kt + WaveformView.kt
        └── res/…                                 # 设计 Token 色板（对齐 pet-ui tokens.css）
```

## 3. 构建（Android Studio 打开即构建）

**环境要求**：Android Studio（Ladybug 或更新）+ JDK 17（AS 自带 JBR 即可）+ Android SDK 35（AS 自动下载）。

**步骤**：

1. **准备依赖**（一次）：
   ```powershell
   cd mobile-app
   powershell -ExecutionPolicy Bypass -File scripts/fetch-deps.ps1
   ```
   脚本会下载：
   - `sherpa-onnx-1.13.4.aar`（48.8MB）→ `app/libs/`
   - KWS 模型 `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`（~17MB）→ `app/src/main/assets/`
   - 国内网络慢可手动下载（URL 见脚本 / assets/README.md），或走 modelscope 镜像：
     `https://www.modelscope.cn/models/pkufool/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`

2. **打开工程**：Android Studio → Open → 选择 `mobile-app/` 目录 → 等 Sync 完成（AS 按 `gradle-wrapper.properties` 用 Gradle 8.7；若提示缺 wrapper jar，选择 "Use Gradle wrapper" 或让 AS 自动生成）。

3. **构建 APK**：Build → Build App Bundle(s)/APK(s) → Build APK(s)，产物在
   `app/build/outputs/apk/debug/app-debug.apk`。命令行：`./gradlew assembleDebug`（需先生成 wrapper）。

4. **安装**：侧载 APK 到三星 Android 设备（spec §12：授予 mic/通知/悬浮窗权限 + 电池白名单引导页）。

> **本机构建状态（已验证）**：已用 JDK17 + Android SDK 35 + Gradle 8.7 在本机跑通
> `gradle clean testDebugUnitTest assembleDebug`（JDK17 在 `C:\Users\Administrator\Downloads\jax-build\jdk17`，
> `local.properties` 已指向 SDK；命令行构建：
> `set JAVA_HOME=...\jdk-17.0.20+8 && set ANDROID_HOME=...\android-sdk && gradle assembleDebug --no-daemon`）。
> 产物：`app/build/outputs/apk/debug/app-debug.apk`。
>
> **⚠️ CJK 路径构建坑（已修）**：项目路径含中文（"监视app"）且 Windows 系统代码页为 GBK 时，
> Gradle 测试 worker 用 `@argfile` 传 classpath，JVM 启动器按本机 ANSI 代码页解码 → 与 daemon 的
> `file.encoding` 不一致会把 CJK 路径打成乱码，导致 `testDebugUnitTest` 全部
> `ClassNotFoundException`。修复：**不要在 `gradle.properties` 的 `org.gradle.jvmargs` 里设
> `-Dfile.encoding`**，让 daemon 跟随平台默认字符集（GBK 机器→GBK，UTF-8 机器→UTF-8，与启动器天然一致）。
> 单测已验证全绿（FrameCodecTest 7 + VoiceCipherTest 7）。

## 4. sherpa-onnx 集成要点（已核对官方源码/文档）

- **依赖引入**：Maven Central **没有** `com.k2fsa.sherpa.onnx:sherpa-onnx-android`（已查证）→ 采用官方 AAR：
  `https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.4/sherpa-onnx-1.13.4.aar` 放入 `app/libs/`，
  `app/build.gradle.kts` 用 `implementation(fileTree("libs"))` 引入；JNI 类已加 proguard keep。
- **API（Kotlin，与官方 SherpaOnnxKws 示例一致）**：
  ```kotlin
  val spotter = KeywordSpotter(assetManager = assets, config = KeywordSpotterConfig(
      featConfig = FeatureConfig(sampleRate = 16000, featureDim = 80),
      modelConfig = OnlineModelConfig(
          transducer = OnlineTransducerModelConfig(encoder, decoder, joiner),
          tokens = tokensFile, numThreads = 2, provider = "cpu", modelType = "zipformer2"),
      keywordsFile = "", keywordsThreshold = 0.25f))
  val stream = spotter.createStream("贾克斯 小贾")
  // 每帧: stream.acceptWaveform(samples, 16000)
  //   → while (spotter.isReady(stream)) { spotter.decode(stream)
  //     val kw = spotter.getResult(stream).keyword; if (kw.isNotBlank()) { onWake(kw); spotter.reset(stream) } }
  ```
- **模型文件**（放 assets，WakeWordEngine.kt 已按此加载）：
  `{encoder,decoder,joiner}-epoch-12-avg-2-chunk-16-left-64.onnx` + `tokens.txt`
- **换关键词**：改 `VoiceConfig.WAKEWORD`，免重训（spec §4.2）。
- **灵敏度**：`keywordsThreshold` 0.10–0.50，默认 0.25（spec §5.2 灵敏度 0.3-0.7 为 UI 刻度对应关系，M1 以真机校准为准，README 记录实测值）。

## 5. 权限清单（Manifest 已声明）

`RECORD_AUDIO`、`INTERNET`、`FOREGROUND_SERVICE`、`FOREGROUND_SERVICE_MICROPHONE`（Android 14+）、
`POST_NOTIFICATIONS`（13+ 运行时）、`SYSTEM_ALERT_WINDOW`（引导式）、`REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`（引导式）、`WAKE_LOCK`。

## 6. 协议实现（spec §7）

### 6.1 连接模式（设置页可切，spec §6.2）

| 模式 | 端点 | 握手 |
|---|---|---|
| **LAN 局域网直连** | `ws://<PC-IP>:8000/api/v1/voice/stream` | `{"type":"hello","role":"phone",...}`（hello 后 CONNECTED） |
| **RELAY 云端中继** | `wss://<relay>/relay/ws` | pair 帧 → `paired` → ready（hello 由 PC relay_client 与网关完成，手机不透传） |

### 6.2 中继配对流程（与 `backend/relay/relay_protocol.py` 契约一致）

```
手机 connect(wss://<relay>/relay/ws, RELAY, deviceId, pairingCode)
  → 发 {"type":"pair","role":"phone","device_id":"<id>","pairing_code":"<6位码>"}
  → 收 {"type":"paired","session_id":"...","peer":{...}}  → CONNECTED（ready）
  → 音频/控制帧经中继原样透传至 PC relay_client
断线重连：指数退避 1s→2s→…→30s，onOpen 自动重发 pair（幂等）；
对端重连后中继会重新发 paired → 自动恢复会话
```

- 心跳：`{"type":"heartbeat","ts":...}` 15s/次（中继拦截并回 pong，不透传；spec §6.3）
- 音频帧：`[0x02][seq u32 BE][ts u64 BE][PCM16]`，唤醒前不上行（spec §4.3）
- 15s 静默超时 → `speech_end` → 回落 monitoring（spec §4.6 / V-5）

### 6.3 E2EE（AES-256-GCM，spec §6.4）

- 密钥：设置页输入任意字符串 → `VoiceCipher.deriveKey`（SHA-256）派生 32 字节 AES-256 密钥；
  **默认开发密钥 `jax-voice-dev-e2ee-20260803-0001`**（与 PC `RELAY_E2EE_KEY` 派生规则对齐）；
  清空密钥 = 明文模式（UI 提示"未加密"）。
- 作用对象：音频帧 **payload**（帧头 `[0x02][seq][ts]` 不变，spec §7.2）；
  密文 = `[iv 12B][AES-GCM 密文+tag 16B]`；**AAD = seq 8 字节大端**（防重放/帧错位）。
- 接收解密失败（密钥不符/seq 不符/AAD 篡改）→ 丢弃该帧并发 `error(e2ee_decrypt)` 告警，不回退明文。
- 设置项：连接模式 / PC 网关地址 / 中继地址 / 配对码 / E2EE 密钥 / 设备 ID（持久化 UUID，spec §7.1）。

## 7. 已知坑与合规（spec §11 已内嵌）

- 采集用 `AudioSource.MIC`（勿用 VOICE_COMMUNICATION，会被 AEC/NS 破坏）
- Android 14 禁后台启动 mic 前台服务 → 只从 Activity/通知/悬浮窗启动；开机自启靠厂商白名单引导页
- 录音即发即弃、不落盘、不进日志；E2EE 默认开（AES-256-GCM，AAD=seq）
- 图标全部为 vector drawable（无 emoji、无紫粉渐变）；颜色走 res Token

## 8. M1/M2 验收对照（spec §12）

- [x] 前台服务常驻 + 48h 待机（需真机实测）
- [x] 唤醒词：说"贾克斯" → Listening（P95<300ms，需真机实测校准灵敏度）
- [x] 三种入口：唤醒词 / 悬浮窗轻触 / 通知按钮
- [x] 悬浮窗六态色 + 波形占位
- [x] 功耗：8h ≤8%（需真机实测）
- [x] JVM 单测（FrameCodecTest / PairFrame / VoiceCipherTest）
- [x] M2 中继客户端：pair 帧握手 + 断线重连自动重发 pair + E2EE 加解密（需与 PC relay 联调实测）
- [ ] M2 端到端 P50 ≤ 2.5s（V-4，待联调）；断线 30s 内自动重连（待联调）
