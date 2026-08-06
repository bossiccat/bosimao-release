# App 稳定性审计 — 波斯猫手机语音（架构级根治方案）

> 审计人：首席架构师 高见远（arch-audit）| 日期：2026-08-04
> 范围：mobile-app 全代码走查（只读，未改代码）| 代码基线：1426bed（mobile-app 与主仓库同基线）
> 依据：docs/STATUS.md、docs/specs/mobile-voice-spec.md、mobile-app/app/src/main/java/com/jax/voice/（全部 17 个源文件）、docs/OPS-003-live-test.md
> 结论：**App 反复闪退不是单点 bug，是线程模型 + 崩溃自愈 + 日志可达三层系统性缺陷**。KWS 判空修复（WakeWordEngine init 的 createStream null 防御）只堵了 Java 层空指针，没有触及根因。

---

## 0. TL;DR（30 秒结论）

1. **最可能的反复闪退根因 = sherpa-onnx JNI 跨线程生命周期竞态（native SIGSEGV）**：`WakeWordEngine.process()` 在 `jax-mic` 采集线程跑，`release()` 在主线程 `onDestroy` 跑，JNI 对象（KeywordSpotter/OnlineStream）非线程安全 → 服务每次停止/重启即可能 use-after-free → 原生崩溃。**原生崩溃不经过 Java UncaughtExceptionHandler，crash_log.txt 根本记不到** → 与"KWS 判空已修仍崩"完全吻合（修的是 Java 层，崩的是 native 层）。
2. **崩溃日志当前不可达**：crash_log.txt 写在 `getExternalFilesDir`（= `/Android/data/com.jax.voice/files/`），Android 11+ 用户/文件管理器/PC 全部读不到 → "无线诊断"功能对用户是死的。即使 Java 崩溃被记了，也取不出来。
3. **自研 App 的核心差异化（离线 KWS 唤醒 + 私有 E2EE 中继 + PC 大脑受控注入）真实存在，市场成熟方案无一能替代**。建议**继续自研 + 架构根治**，不切换。
4. 根治 = 线程模型单线程化收敛 + 崩溃自愈 + 日志可达（弹窗摘要/诊断页/MediaStore 镜像）+ 服务重启防御。预估 1-1.5 人周。

---

## 1. 崩溃根因分析（架构级）

### 1.1 线程模型总览（谁在哪个线程干什么——先画清战场）

| 组件 | 线程 | 操作 |
|---|---|---|
| `VoiceForegroundService.onStartCommand` / `startPipelineInner` | 主线程 | startForeground、创建 AudioRecord、建 WS/KWS |
| `MicRecorder.loop` | `jax-mic` 线程 | `record.read` + `onFrame` 回调 |
| `FrameDispatcher.onFrame` | jax-mic 线程 | 调 `wakeEngine.process()`（sherpa JNI）、`onUplink` → `ws.sendAudio` |
| `triggerWake`（KWS 回调） | jax-mic 线程 | 写 VoiceController、起协程、`ws.sendControl`、`ws.connect` |
| `VoiceWsClient.listener.*` | OkHttp 线程 | onState/onControl → `VoiceForegroundService.handleControlFrame` → `nm.notify` |
| 服务协程（idleTimeout/heartbeat/reconnect） | `Dispatchers.Default` | `ws.sendControl` |
| `onDestroy` | 主线程 | `wakeEngine.release()` / `ws.disconnect()` / `micRecorder.stop()` |
| `MainActivity` UI | 主线程 | collect StateFlow → 更新 UI / overlay |

**核心病灶**：一个 `jax-mic` 采集线程 + OkHttp 线程 + Default 协程 + 主线程，四条线程同时操作**同一批非线程安全对象**（sherpa JNI、OkHttp WebSocket、VoiceController 状态）。这就是"反复"的来源——每次服务启停都重新触发竞态。

### 1.2 缺陷 A（P0，最可能根因）：sherpa-onnx JNI 跨线程生命周期竞态

- 证据：`WakeWordEngine.kt:76-90` `process()` 在 jax-mic 线程调 `acceptWaveform/decode/getResult/reset`；`VoiceForegroundService.kt:297-309` `onDestroy()`（主线程）调 `wakeEngine?.release()` → `WakeWordEngine.kt:92-96` `stream?.release() + spotter.release()`。
- 机理：`release()` 释放 JNI 内存的同时，采集线程可能正在 `decode`/`acceptWaveform` 同一 OnlineStream → 悬垂指针 → **SIGSEGV，进程直接死，Java handler 不触发**。
- 触发面：每次 stop 监听、每次 START_STICKY 重启、服务被杀后系统重建（`onDestroy` 与残留 mic 线程并发）。用户"反复闪退"高度吻合。
- 附加：`KeywordSpotter` 本身亦非线程安全；M1 已交付的"48h 待机/进程不杀"验收在服务被系统回收→重建的路径上必然踩到。

### 1.3 缺陷 B（P0）：MicRecorder 单点异常吞掉整个采集循环 → 静默死亡 / 未捕获 Error 崩进程

- 证据：`MicRecorder.kt:69` `catch (e: Exception)` 包住**整个 while 循环**。任何一次 `onFrame` 抛异常（含 WS `send` 的 `IllegalStateException`、JNI 偶发 RuntimeException）→ 循环退出 → `record.stop()+release()` → 采集线程死亡。
- 后果一（静默死）：服务仍报 RUNNING，UI 显示"监听中"，但管线已死——用户感知为"App 装了不会说话/坏了"。
- 后果二（闪退）：若 `onFrame` 链抛 `Error`（OOM、`UnsatisfiedLinkError`、`StackOverflowError`）→ 不被 catch → **jax-mic 线程未捕获异常 → 进程死亡**（Android 任一线程未捕获即杀进程）→ 可见闪退。此时 UncaughtExceptionHandler 会记 crash_log.txt——但用户取不到（缺陷 D）。

### 1.4 缺陷 C（P0）：OkHttp WebSocket 三线程并发调用 + send 无防御

- 证据：`VoiceWsClient.sendControl()`（`:96-98`）与 `sendAudio()`（`:101-107`）无任何 try/catch 与锁；调用方横跨 jax-mic（triggerWake/FrameDispatcher）、OkHttp 线程、Default 协程（heartbeat/idleTimeout）、主线程（disconnect）。
- 机理：OkHttp `RealWebSocket.send()` 在 socket 处于 CLOSED/FAILED 态时抛 `IllegalStateException("closed")`；`connect()`/`cancel()`/`close()` 并发调用亦存在竞态。该异常在 triggerWake 路径上会一路冒到 `MicRecorder.loop` 的 catch → 触发缺陷 B（静默死亡）；在 heartbeat 协程里则杀死协程（SupervisorJob 吸收，无害但无观测）。
- 附带：`VoiceWsClient.kt:163-180` 下行音频帧已解密但**直接丢弃**（M1 无 AudioTrack，注释写明"M1 仅解码计数"）——功能缺失而非崩溃，但说明 M2 接 AudioTrack 前必须先把播放线程模型定好。

### 1.5 缺陷 D（P0，用户痛点）：崩溃日志不可达

- 证据：`JaxApp.kt:33` `getExternalFilesDir(null) ?: filesDir` → Android 11+ 该路径位于 `/Android/data/com.jax.voice/files/crash_log.txt`，受作用域存储保护。
- 影响：① 用户无法取日志（正是用户反馈）；② 只记 Java 崩溃，**native SIGSEGV 记不到**（缺陷 A 的崩溃永远无日志）——双重不可达。

### 1.6 缺陷 E（P1）：START_STICKY + microphone 前台服务后台重启风暴

- 证据：`VoiceForegroundService.kt:85` 返回 `START_STICKY`；`:90-97` startPipeline 只 `catch (t: Throwable)` 后 stopSelf。
- 机理：Android 12+ 从后台重启 mic 类型 FGS → `ForegroundServiceStartNotAllowedException`（或 5 秒内未 startForeground 被系统判违约）→ 防御分支吞掉并 stopSelf；但 STICKY 语义下系统可能反复尝试 → 重启-崩溃抖动，用户看到 App 反复"闪退/自动关"。Android 14 上此路径尤为突出。
- 附带：通知栏 ACTION_TALK/ACTION_PAUSE/ACTION_EXIT 用 `PendingIntent.getService`（`:248-262`）是普通 `startService`——服务未运行时在后台触发会抛 `IllegalStateException`（主线程 → 闪退）。应改用 `startForegroundService`（PendingIntent 不能直接改，需经一个前台中转 Activity 或允许服务内先 startForeground）。

### 1.7 缺陷 F（P1）：无崩溃自愈与状态可见性

- `VoiceController` 是纯状态总线（无进程内 watchdog）；服务被系统杀→重启失败→UI 只显示 STOPPED，用户无从得知"刚才崩了、为什么"。App 内无"诊断"入口。
- `MainActivity` 的 `dotPhase.background.mutate()`（`:105`）若 background 为空会 NPE（当前布局有背景，低风险，仍应防御）。
- `FloatingOverlay.hide()/show()` 无 try/catch，`removeView` 已移除视图时抛 IllegalArgumentException（主线程）→ 边缘闪退。

### 1.8 附加发现（非崩溃，但影响"是否值得继续修"的判断）

- **唤醒词配置未在真机验证**：`VoiceConfig.WAKEWORD = "波斯猫"`，而模型自带 keywords.txt 只含 8 个词（你好军哥/蛋哥蛋哥/小爱同学/你好问问/小艺小艺/小米小米/林美丽/你好西西），README 声明"支持自定义唤醒词"（ppinyin 免重训）。"波斯猫" = bo/si/mao 均为标准拼音音节，理论可 token 化，但**未真机验证**——若 token 化异常，KWS 永不触发（功能死），极端情况 JNI 边界崩。M1 验收（唤醒率/误触发）本就要求真机校准，此项必须列入修复验证。
- **PNG 图标违约**：`res/drawable/ic_button_bosimao.png`（17:54 新增）为位图，违反 spec §4.5"Android 端以 vector drawable 落地同一套图标"（P0-1 无 emoji/统一 SVG 库约束）。不影响崩溃，列为 P2。
- 单文件行数合规：最大 `VoiceWsClient.kt` 254 行 ≤300，代码组织规范无违反。

---

## 2. 崩溃日志可达性 — 根治方案（推荐组合）

> 目标：用户**无需 USB/adb** 就能看到崩溃内容；开发者能拿到原始栈。三层并行，全部零新增权限。

### 方案 A（主，必须）：App 内崩溃弹窗 + 诊断页
- **写入位置改到 app 私有目录**：`filesDir/crash_log.txt`（`Context.getFilesDir()`，App 内永远可读，零权限）。
- **下次启动弹窗摘要**：`MainActivity.onCreate` 读取 crash_log.txt，若 `mtime > 上次已读时间戳`（SharedPreferences 记 `last_crash_shown_ts`）→ `AlertDialog` 展示**前 20 行**（时间/设备/线程/异常头/栈顶 3 帧），按钮：`复制全部` / `分享`（ACTION_SEND 走系统分享，用户可发微信/邮件给团队）/ `不再显示`。每次启动只弹一次，不打扰。
- **诊断页**（SettingsActivity 加"诊断"入口，或独立 `DiagnosticsActivity`）：完整崩溃列表 + 复制 + 分享 + 导出按钮；顺带展示服务状态/连接模式/设备 ID（对排查"静默死亡"有用）。

### 方案 B（推荐，补外部可达）：MediaStore Download 镜像
- Android 10+（API 29+）经 `MediaStore.Downloads` 写入 `jax_crash_log.txt` **无需运行时权限**（作用域存储内 MediaStore 写入合法）。用户在"文件/下载"App、PC MTP 均可见 → 无需 USB 调试即可给团队。
- 实现：JaxApp.writeCrash 双写（私有 + MediaStore）；API 26-28 走 `WRITE_EXTERNAL_STORAGE` 运行时申请（或直接跳过镜像，私有路径已兜底）。

### 方案 C（推荐，自动化回传）：WS 控制帧上报
- 本项目已有 手机↔PC 的 WS 通道：崩溃摘要（前 20 行）在连接后发 `{"type":"crash_report", ...}` 控制帧给 PC 网关，PC 侧记录/推送（复用 PushManager 脱敏）。用户零操作，团队秒级拿到。**这是本项目独有的低成本优势，应优先做**。
- 注意隐私：只发栈顶/异常头，不发录音相关内容（延续"录音不进日志"边界）。

### 方案 D（不做）：直接写 `/storage/emulated/0/Download` 裸路径
- API 29+ 被作用域存储禁止（需 MANAGE_EXTERNAL_STORAGE，用户反感）；API 26-28 需 WRITE_EXTERNAL_STORAGE 运行时权限且用户可拒绝。不选。

**结论：A（私有 + 弹窗 + 诊断页）为 P0 必做；B（MediaStore）为低成本补充；C（WS 回传）为本项目特色，强烈建议；D 否决。**

---

## 3. 自研 vs 成熟方案 — 诚实决策

### 3.1 自研 App 的核心差异化（用户问"市场有成熟方案"——逐项核实）

| 能力 | 自研方案 | 成熟方案能否替代 |
|---|---|---|
| 息屏常驻 + 自定义中文唤醒词（离线） | sherpa-onnx KWS（Apache-2.0，免训练） | ① Porcupine/Picovoice：精度/功耗更优但**商用授权付费**，且只是唤醒词单点；② 系统助手（Siri/Google/Alexa）不支持自定义唤醒词 + 私有管线 |
| 音频全程私有（录音不出中继、E2EE、PC 侧本地处理） | 自建中继 + AES-256-GCM + 本地模型/STT | **无一成熟方案满足**——系统助手全部走其云端；第三方 SDK 均破坏"录音不出必要范围"硬边界 |
| 接入 PC 贾克斯大脑（DeepSeek 拆解 + 确认后注入） | 私有 WS 协议直达 PC brain | **不存在现成产品**：这是本项目独有的"手机语音 → PC 大脑"闭环 |
| V2 双向远程指挥通道复用 | 语音中继/WS 链路 V2 直接复用 | 换方案则 V2 通道需重建 |

**结论：差异化真实且是产品核心，不是"为了自研而自研"。**

### 3.2 切换成本对比（诚实评估）

| 方案 | 成本 | 是否能解决崩溃 | 结论 |
|---|---|---|---|
| 继续自研 + 架构根治（本审计） | 1-1.5 人周（线程收敛 + 自愈 + 日志 + 防御） | 能（根因明确、可修） | **推荐** |
| 换 WebApp/H5（浏览器常驻麦克风） | 重做 2-3 人周 | 不能解决"息屏唤醒/常驻"——浏览器后台无麦克风、无 FGS | 否决：丢核心形态，产品退化为"打开网页才能对话" |
| 换 React Native/Flutter 重写 | 重写 4-6 人周 + 崩溃根因照样存在 | 不能：FGS/JNI/唤醒词全部要自写原生模块，线程模型问题原样保留 | 否决：零收益 |
| 换 Porcupine 做唤醒词（组件级替换，非架构切换） | 商用授权费 + 集成 0.5 周 | 与崩溃无关；只影响唤醒精度/功耗 | 保留为 M1 真机实测不达标时的备选（spec §4.2 已定） |

### 3.3 决策建议

**继续自研（修复路线）**。理由：① 差异化真实；② 崩溃根因已定位、修复面可控（集中在线程模型，不深）；③ 切换方案要么丢形态（WebApp）要么不解决根因（RN/Flutter）；④ 唯一值得组件级换的是 KWS 引擎，且已在 spec §4.2 备选（Porcupine）中，属 M1 实测后决策，与崩溃无关。

---

## 4. 根治修复清单（按优先级）

### P0（阻塞交付，必须修）

| # | 修复点 | 做法 | 证据 |
|---|---|---|---|
| 1 | **JNI 生命周期单线程化**（消除 native 闪退根因） | WakeWordEngine 的 create/process/release **全部收敛到单一 KWS 线程**（采集线程内串行）；`release()` 改为"置停止标志 + 由采集线程最后执行 release"，服务 onDestroy 只发停止信号并等待线程 join（超时 1s 兜底）；process/release 加对象锁防并发 | WakeWordEngine.kt:76-96 / VoiceForegroundService.kt:297-309 |
| 2 | **MicRecorder 逐帧防御 + 自愈** | 每帧 try/catch（`onFrame` 异常只跳过当帧，不退出循环）；区分 `Exception`（记日志继续）与 `Error`（记 crash 后走重建）；服务加 watchdog：定期检查 mic 线程存活，死亡则自动重建管线并上报 UI | MicRecorder.kt:57-79 |
| 3 | **WS 调用收敛 + send 防御** | VoiceWsClient 全部 public 方法内部串行化到单 Executor/锁；`sendControl/sendAudio` 包 try/catch(IllegalStateException) 静默丢弃（socket 已关属预期）；禁止 jax-mic 线程直调 WS，改投递到 WS executor；下行帧接 AudioTrack 前先定义播放线程归属（M2 前置） | VoiceWsClient.kt:96-107 |
| 4 | **崩溃日志可达性** | ① 私有目录 `filesDir/crash_log.txt`（必读）；② MainActivity 启动检测新崩溃 → AlertDialog 摘要（前 20 行 + 复制/分享）+ 设置页"诊断"入口；③ MediaStore.Downloads 镜像（API 29+ 免权限）；④ WS 连接后发 `crash_report` 控制帧回传 PC | JaxApp.kt:32-51 |
| 5 | **服务重启防御** | START_STICKY 改为：onStartCommand 捕获 `ForegroundServiceStartNotAllowedException`（API 31+）→ 停服务 + UI 显示"可在前台一键重连"，**不**靠系统反复拉起；通知按钮 PendingIntent 改 `startForegroundService` 语义（经中转或服务内先 startForeground），杜绝后台普通 startService 闪退 | VoiceForegroundService.kt:62-86, 248-262 |

### P1（重要）

| # | 修复点 | 做法 |
|---|---|---|
| 6 | startPipeline 失败清理 | catch 分支释放已创建对象（wakeEngine/ws/dispatcher/mic 半初始化即回收），防泄漏 + 防半初始化状态 |
| 7 | 崩溃场景单测 | KWS 空流/重复 release/并发 process（JVM 层可用 mock 或封装接口测时序）；WS 关闭后 send 幂等；服务重建管线幂等 |
| 8 | 状态可见性 | VoiceController 增加 `lastError`/`restartCount`；服务心跳到 UI（"监听中/已停止/异常 N 次"），把静默死变成可见状态 |
| 9 | UI 防御 | `dotPhase.background` 判空；FloatingOverlay.hide/show 加 try/catch + 防重入 |

### P2（后续）

| # | 修复点 | 做法 |
|---|---|---|
| 10 | native 崩溃捕获（可选） | 接入 NDK 级 handler（如 Crashpad/Breakpad 或 sherpa 提供的 crash catcher）捕获 SIGSEGV；P0-1 修复后优先级下降，先不做 |
| 11 | 唤醒词真机验证 | "波斯猫" on-device 实测（唤醒率/误触发/功耗）；不达标按 spec §4.2 评估 Porcupine |
| 12 | ic_button_bosimao.png 换 vector | 统一 SVG 图标库约束（P0-1 纪律），防 drawable 膨胀 |

---

## 5. 验收标准

- [ ] 连续 20 次"启动监听 → 停止 → 再启动"无闪退（覆盖缺陷 A 竞态面）
- [ ] 进程被杀后重启（START_STICKY 路径）Android 12+ 无 ForegroundServiceStartNotAllowedException 抖动
- [ ] 强制制造一次 Java 崩溃（测试代码注入）→ 下次启动弹窗显示摘要，可复制/分享；`filesDir/crash_log.txt` 存在；API 29+ 的 Download 目录有镜像
- [ ] 构造 WS 关闭后 send 场景 → 不崩、不静默死，服务状态可见
- [ ] 单测全绿；新增崩溃/时序用例覆盖 P0-1~P0-5
- [ ] 日志隐私审计：crash_report 不含录音/原始音频内容

---

## 6. 附：证据文件索引

| 文件 | 关键行 | 说明 |
|---|---|---|
| app/src/main/java/com/jax/voice/JaxApp.kt | 32-51 | 崩溃写入 getExternalFilesDir（Android 11+ 不可达）|
| app/src/main/java/com/jax/voice/voice/WakeWordEngine.kt | 76-96 | process（mic 线程）vs release（主线程）跨线程 JNI |
| app/src/main/java/com/jax/voice/voice/MicRecorder.kt | 57-79 | 整循环 catch(Exception)，Error 崩进程、异常静默死 |
| app/src/main/java/com/jax/voice/net/VoiceWsClient.kt | 96-107, 163-180 | send 无防御；下行帧丢弃 |
| app/src/main/java/com/jax/voice/voice/VoiceForegroundService.kt | 62-86, 248-262, 297-309 | START_STICKY + 通知 PendingIntent.getService + onDestroy release |
| app/src/main/java/com/jax/voice/config/VoiceConfig.kt | 33 | WAKEWORD="波斯猫"（模型内置 8 词未含，待真机验证）|
