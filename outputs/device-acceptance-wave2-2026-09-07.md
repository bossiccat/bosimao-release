# 真机验收 wave2 报告 — 2026-09-07

仓库：`C:\Users\Administrator\WorkBuddy\监视app`（master）
范围：2026-09-06 16:43 ~ 2026-09-07 12:09 的修复与取证批次
性质：**只固化证据，不改生产代码**（backend/ sidecar/ mobile-app/ 本轮零改动，本报告的产出全部落在 `outputs/` 与 `scripts/`）

---

## 1. 执行摘要

**一句话结论**：wave2 把「卡断 / 静音 / 误打断 / 采集削波」四类故障的代码级根因全部定位并修复、每一条都有 RED→GREEN 证据，但**无线 ADB 未打通 → 09-07 当天零真机取证**，商用门禁需要的 TTFP / TTS TTFB / barge-in 三类数据**一个都拿不到**。

**判定：NO-GO（维持）**

三条判定依据：

1. 商用门禁不可判 —— TTFP / TTS TTFB / barge-in 三条**全部无实测样本**（逐项状态见 `outputs/commercial-metrics-baseline-2026-09-07.md`）。
2. 真机证据缺失 —— 09-07 当天 `adb connect 100.75.48.99:46813` → `10061 主动拒绝`（手机无线调试未开），PCM、声学、逐帧对账全部取不到。
3. 10.1s 首包无法拆分归因 —— 「response.created → 手机 firstAudioFrame 10.1s」已由 09-06 会话实锤，但**本地侧各跳耗时尚无可引用的实测数据**，因此拆不出各段占比；直接缺口是 Android 侧 L5（`onFirstAudioFrame`）未接入 F6/F7 trace。补齐 L5 后可一轮定位。

**不是缺陷的部分**：真机档证据少是事实，不是遗漏。本报告不把「静态/单元证据」升格为真机证据，也不把「未实测」写成 PASS。

---

## 2. 证据等级定义

| 等级 | 含义 | 判据 |
|------|------|------|
| **静态** | 代码/配置阅读、产物（APK dex / exe PYZ）扫描、文档对账 | 有人可读的原始物，但无执行 |
| **单元** | 单进程/单模块自动化测试（pytest / JVM 单测 / node --test） | 有 RED→GREEN 或全量回归数字 |
| **集成** | 本机多进程真实联跑（backend ↔ rtc_bridge ↔ sidecar ↔ 云端） | 有跨进程日志/时间戳，无手机 |
| **真机** | Android 设备运行时取证（logcat / diag_log / 线程普查 / PCM / 听测） | 有设备侧原始输出 |

---

## 3. 已闭环项

按时间序。所有 commit 均在 master。

| # | 修复项 | commit | 验证方式 | RED→GREEN 证据 | 等级 |
|---|--------|--------|----------|----------------|------|
| C1 | 采集增益正反馈失控（D1）+ 下调过慢（D2）：自适应噪声底只在非语音帧更新、候选门槛 `noiseFloor*3` clamp(20,300)、伪语音退增益不刷新保持窗、ATTACK_UP=0.05 / ATTACK_DOWN=0.25 | `d6cd29f` | JVM 单测 | 156/156 全绿（152 基线 + 4 条 D1/D2 回归） | **单元** |
| C2 | 管线存活判据与 `micRecorder` 耦合导致「守卫从未生效」→ 每次点击重建整条管线、采集源泄漏 | `43b196b` | 单测 + **真机复验** | 156 例全绿；真机连点 3 次 5/5 判据达标：`jax-rtc-capture` 5→1、`captureThreads` 5→1、`startPipeline skipped` 0→2、`start accepted` 3→1+2 conflict、`pipeline built seq` 每次点击=1 | **真机**（2026-09-06 17:45 采样） |
| C3 | 回声自激误打断：BargeInController 注入单调时钟 + PLAYBACK_ONSET_GUARD 400ms / VOICE_INTERRUPT_REARM 3000ms / 忽略日志 2s 降频；段边界只在「非 SPEAKING→SPEAKING 迁移」重置 | `9604be4` | 单测 + **真机复验** | 159 例全绿（156 基线 + 3）；diag_log 对比：`interrupt source=user_voice` **32→0**、播放段 1.0/1.6/1.8s 碎片 → **2112ms 完整放完**、本地 stop/flush 32→0、被拦 4 次 onset guard 13/34/54/74ms | **真机**（2026-09-06 18:33 采样） |
| C4 | 噪声门改到原始域并跟随自适应底噪（根治嘈杂房间门常开） | `2902fee` | 单测 | 与 C1 同批合入；单条测试数未在本轮产出中单独落盘记录 | 单元 |
| C5 | qwen realtime bridge 断线自动重连（复用 ReconnectScheduler，1s→60s 退避、5 次放弃；断线窗口丢帧 + 2s 节流日志；on_error → 结构化 `cloud_engine_down`） | `ec9a1d4` | TDD + backend 全量 | RED 5 例 → GREEN → **874 passed** | 单元 |
| C6 | brain flush 异步化（`await router.flush()` → `create_task`），上行循环不再被 /intent 挂起阻塞 | `ae2f563` | TDD + backend 全量 | RED 2 例（上行阻塞 0.5s+ / feed 阻塞 2.016s）→ GREEN → **876 passed** | 单元 |
| C7 | 日志持久化：rtc_bridge / relay 进程内 RotatingFileHandler（10MB×5，`*_app.log` 与启动器重定向解耦） | `3e39bb6` | 新增 `test_log_persistence.py`（55 行）+ 全量 | 纳入 P0 批次全量 **885 passed** | 单元 |
| C8 | qwen 引擎说完判定 pad 2s → **400ms**（顺带修掉 `int(pad_s)` 截断真 bug） | `fecce22` | 全量回归 | 全量 **885 passed** | 单元 |
| C9 | /intent extract 与 summary `asyncio.gather` 并行（两轮串行 5s → ~2.5s） | `0408852` | 全量回归 | 全量 **885 passed** | 单元 |
| C10 | qwen tool_call 去同步化（专职 worker 串行队列，不再冻结 recv 循环） | `71ef4f7` | 全量回归 | 全量 **885 passed** | 单元 |
| C11 | `[lat]` 延迟埋点 F1-F5 / F8 / F9 | `d3556ba` | 全量回归 | 全量 **885 passed** | 单元 |
| C12 | 持久化探针改 ERROR 级（绕开 4 个旧测试模块的 `logging.disable(WARNING)` 全局压日志） | `fcfa336` | 回归 | 修复 3 次「收集即失败」的**环境**误判 | 单元 |
| C13 | `JAX_DOWN_PCM_DUMP` 上下行 PCM 取证落盘开关 | `1b0e6f5` | 新增 `test_pcm_dump.py`（150 行） | 开关已写入根 `.env`，watchdog 拉起的实例自带取证能力 | 单元 |
| C14 | F6/F7 下行逐帧关联：`reply_id` / `frame_seq` / `src_seq` / `t_enq` / `t_send` 全链路透传；sidecar 落 L2（WS 收帧）/ L3（SDK 调用）两级证据 | `af08c8f` + `e8b854f` | RED→GREEN + 全量 + sidecar 复核 | 新测试 **7 项改前全 failed**（`KeyError: 'reply_id'` / 无 `begin_reply`）→ **7 passed**；backend 全量 **729 passed / 0 failed**；sidecar 23 passed + 独立复核 4/4（新格式透传/旧格式兼容/畸形降级/消息类型隔离） | 单元 + **集成**（sidecar 联跑复核） |

**说明（与派工口径的差异，需 lead 确认）**：派工简报写「真机档应为空」。但 `43b196b` 与 `9604be4` 在 **2026-09-06** 确有真机复验原始证据（见 C2/C3 判据列）。我按事实列入真机档并标注采样时刻——若你的口径是「09-07 当天」，则当天真机档确实为空（无新增）。**09-07 当天没有任何新的真机证据**。

---

## 4. 已定位未修项

### #35 日志持久化（P0-1）

- **根因**：`jax-services.ps1` 用 `-RedirectStandardOutput/Error` 启动进程，每次重启**截断**重定向文件。实锤：2026-09-06 21:06 会话日志被 21:40 重启覆盖，直接导致 10s 首包无法归因。
- **影响**：任何跨重启的故障归因都会在取证前被自己销毁。这是过去三轮反复踩的同一个坑。
- **已落地**：`3e39bb6` 给 rtc_bridge / relay 加进程内 RotatingFileHandler（10MB×5，文件名 `*_app.log`，与重定向目标不同路径）；`backend/app/utils/logger.py` 的 `setup_logging` 另有 `logs/jax.log`（5MB×7）。
- **未修部分**：
  1. **sidecar（Node）侧无等价滚动落盘**，仍依赖启动器重定向；
  2. 生产 `jax-backend.exe` 是否含 `3e39bb6` **未复核**（exe 为 09-05 打包，run_id `ffb1a75a`）；
  3. `jax-services.ps1` 仍在用重定向启动，对未自落盘的进程，重启毁证据的风险原样保留。
- **修复方向**：sidecar 加进程内滚动文件（winston/file 或自写轮转）；重打包 backend exe 并核对产物含新代码（用 `CArchiveReader` 解 PYZ 查 `co_names`，字符串 grep 对压缩 PYZ 无效）；`jax-services.ps1` 的重定向改为保留追加（任务 #17 在改同文件）。
- **本轮不修的原因**：需要**重新打包 exe + 重启生产**，属部署窗口动作；且真机取证未通，此项收益（保住证据）在拿不到证据的前提下排不上；优先级低于打通 adb。

### #38 F6/F7 端到端观测

- **根因**：下行跨 4 个边界（qwen delta → bridge 成帧 → WS → sidecar → TRTC SDK → 手机），改动前每跳日志只有**孤立字节数、无共享身份** → 「云端发了但手机没响」与「sidecar 收到但 SDK 没送出去」在日志里长得一模一样。这是「卡断」类故障长期无法定位的根因——不是没日志，是日志之间无法互相关联。
- **影响**：本轮 10.1s 首包**至今无法拆分归因** —— 本地侧各跳耗时无可引用的实测数据，且手机侧 L5 未接入，无法区分「帧没送到手机」与「帧到了但被 SDK 缓冲住」。
- **已落地**：`af08c8f` + `e8b854f` 打通 bridge ↔ sidecar 的逐帧身份（L2/L3）。
- **未修部分**：
  1. **Android 侧 L5（`onFirstAudioFrame`）未接入 trace 体系** —— 缺它就无法区分「帧没送到手机」与「帧到了但被 SDK 缓冲住」；
  2. L3 的证据语义边界：`sendCustomAudioData` 返回成功**只证明本地 API 调用返回**，不证明已出网、不证明手机已收到、不证明扬声器已响。要推进到「确实发声」需要外部声学回环或人工听测。
- **修复方向**：把 `reply_id` / `frame_seq` 透传到 Android `onFirstAudioFrame`（L5）并落 DiagLog；设计外部声学回环（PC 播参考音 → 手机采集 → 比对）把 L3 推进到真实发声验证。
- **本轮不修的原因**：需改 `mobile-app/` 并重新出包装机，而装机依赖无线 ADB（当前阻断）；收益也必须靠真机才能兑现。

### #39 PCM 真机取证

- **根因**：没有原始音频，就无法判定静音洞、削波、字节守恒、640B 帧边界——采集质量类问题只能靠「听起来行不行」猜。
- **影响**：C1/C4 的增益修复**没有任何真机样本**证明 out RMS 落在 2000~5000 目标区间。
- **已就绪**：`1b0e6f5` 的 `JAX_DOWN_PCM_DUMP=logs/dump/p0test-2306` 已写进根 `.env`（Load-Env 统一注入，watchdog 拉起的任何实例都自带开关）。
- **未修部分**：**零真实 PCM**。且固定前缀会被后续会话覆盖，取证后必须立刻收、收完从 `.env` 移除开关。
- **修复方向**：adb 通后按 `outputs/device-capture-sop-2026-09-07.md` 采一轮，会话结束立刻收 `.pcm` / `.up.pcm` / `.meta.json`。
- **本轮不修的原因**：纯环境依赖，无代码动作可做。

---

## 5. 环境阻断项

### #22 无线 ADB（阻断全部真机复验）

- **现象**：`adb connect 100.75.48.99:46813` → `由于目标计算机积极拒绝，无法连接。(10061)`。
- **判读**：`10061` = **主动拒绝**，手机在线但**该端口无监听** = 手机端无线调试未开启。**不是网络不通，不是 adb 可执行文件缺失**（`tmp/task6-tools/platform-tools/adb.exe` 有效，已复核）。
- **阻断原因**：需用户在手机上操作，脚本无法代替。
- **解除条件**：手机「开发者选项 → 无线调试 → 使用配对码配对设备」→ 取新端口。**端口每次开启都会变**（历史：38485 / 43783 / 44993 / 15463 / 36311 / 46813），锁屏或一段时间后监听自动关闭。可用 `python tmp/adb_scan_full.py --host <ip>` 并发扫描。
- **连带阻断**：#22 自身的 9604be4 复验、#39 的 PCM 取证、外放回声场景、二次会话回归、Android L5 全部取不到。

### #40 JDK 17（阻断出包与产物校验）

- **现象/判读**：Gradle 8.7 构建 Android 需要 JDK 17，默认环境是 JDK 1.8（实测 `java -version` = 1.8.0_501，`JAVA_HOME` 为空）→ 报 `No matching variant`。
- **重要更正**：**JDK 17 二进制并未缺失**，仍在本机 `C:/Users/Administrator/Downloads/jax-build/jdk17/jdk-17.0.20+8`（已复核，`java -version` = openjdk 17.0.20）。所谓「缺失」是**环境变量层面**的：JAVA_HOME 未设置、PATH 指向 1.8。
- **解除条件**：构建时显式指定即可，无需安装任何东西：
  ```
  JAVA_HOME=C:/Users/Administrator/Downloads/jax-build/jdk17/jdk-17.0.20+8
  GRADLE_USER_HOME=<repo>/gradle-home-v042c        # 仓库内已存在
  不要加 --offline                                  # aapt2-8.6.1 无离线缓存
  ```
- **阻断范围**：无法出含新修复的 debug APK → #40「核对回声防护 APK 与真机证据」停在产物校验一步（09-07 已有 `tmp/apk-manifest-evidence.txt` / `tmp/apk-audit-evidence-3.txt` 等只读产物扫描记录）；所有需装机的真机复验同样卡住。

### 后端存活监控（backend / rtc_bridge 缺存活监控面）

- **现状**（均为可坐实事实）：
  1. **无独立存活监控/告警**，只有 Windows 计划任务 `Jax-Watchdog-Every5Min`（≤5min 轮询，杀旧拉新）被动兜底；
  2. 人工核验靠 `curl -k https://127.0.0.1:8000/health`（自签 HTTPS，**不加 `-k`/CERT_NONE 则 curl 默认静默失败**）+ `curl http://127.0.0.1:19093/health` 看 `pid` / `run_id`；
  3. `jax-services.ps1` 的 restart 对 rtc-bridge **存在不真杀进程的缺陷**（PID 不变、run_id 不变，实测需手动 Stop-Process），存活判据本身不可信（任务 #17 在修同一文件）。
- **阻断原因**：#17 未闭环 + 无监控面 → 「进程在」与「服务活」无法区分，历史上有「旧长跑进程运行时劣化但端口在听」的实际故障（09-05 hello 兑付 transport failed 即此类）。
- **解除条件**：#17 闭环（restart 幂等且 PID/run_id 必变）+ 建一个周期性 health 采样落盘/告警面。

---

## 6. 证据等级矩阵

| 项 | commit / 来源 | 静态 | 单元 | 集成 | 真机 |
|----|----------------|:----:|:----:|:----:|:----:|
| 采集增益 D1/D2 修复 | `d6cd29f` | ● | ● | | |
| 噪声门原始域 + 自适应底噪 | `2902fee` | ● | ● | | |
| 管线存活判据解耦 | `43b196b` | ● | ● | | **●**（09-06 17:45） |
| 回声自激误打断限流 | `9604be4` | ● | ● | | **●**（09-06 18:33） |
| qwen 桥断线自动重连 | `ec9a1d4` | ● | ● | | |
| flush 异步化 | `ae2f563` | ● | ● | | |
| 日志持久化（rtc_bridge/relay） | `3e39bb6` | ● | ● | | |
| pad 2s→400ms | `fecce22` | ● | ● | | |
| /intent 并行 | `0408852` | ● | ● | | |
| tool_call 去同步化 | `71ef4f7` | ● | ● | | |
| `[lat]` 埋点 F1-F5/F8/F9 | `d3556ba` | ● | ● | | |
| PCM 取证开关 | `1b0e6f5` | ● | ● | | |
| F6/F7 下行逐帧关联 | `af08c8f`+`e8b854f` | ● | ● | ● | |
| response.created→firstAudioFrame 10.1s | 09-06 21:06 会话 | | | ● | |
| **TTFP P50/P95/P99** | — | | | | **未实测** |
| **TTS TTFB P50/P95/P99** | — | | | | **未实测** |
| **barge-in 开口→停播** | — | | | | **未实测** |
| **采集增益真机复算（out RMS 是否落 2000~5000）** | — | | | | **未实测** |
| **真实 PCM（静音洞/削波/字节守恒）** | #39 | | | | **未实测** |
| **外放回声场景（30 轮）** | #22 | | | | **未实测** |
| **Android L5 `onFirstAudioFrame`** | #38 | | | | **未实测** |
| **二次会话真机回归** | #22 | | | | **未实测** |

**矩阵结论**：

- **真机档只有 2 项**（`43b196b` / `9604be4`），且都产自 2026-09-06；**09-07 当天真机档为空**。
- 所有「已闭环」的延迟类修复（C8–C11）**只有单元证据**——代码正确不等于链路变快，真机收益全部未验证。
- 商用门禁需要的 5 个数据点（TTFP 分位、barge-in、PCM、回声场景、L5）**全部未实测**。
- 这是正确结果，不是缺陷。把上表任一「未实测」写成 PASS 才是缺陷。

---

## 7. 附：本轮产出索引

| 文件 | 内容 |
|------|------|
| `outputs/device-acceptance-wave2-2026-09-07.md` | 本文（主报告） |
| `outputs/device-capture-sop-2026-09-07.md` | 真机取证 SOP（adb 恢复后照做） |
| `outputs/commercial-metrics-baseline-2026-09-07.md` | GPT-Live 门禁 vs 实测，逐项标差距 |
| `outputs/internal-latency-budget-2026-09-07.md` | 延迟预算增补页（基线页为 `-2026-09-06`） |
| `outputs/f6f7-downlink-trace-2026-09-07.md` | F6/F7 实施与验证 |
| `outputs/market-benchmark-realtime-voice-2026-09-06.md` | 外部基准 |
| `scripts/device-acceptance-capture.ps1` | 取证一键脚本（PS 5.1，已通过语法解析 + 失败路径冒烟） |

---

## 8. 下一轮的第一件事

按依赖顺序，前一步不通后面全废：

1. **用户操作**：手机开无线调试 + 配对，取新端口（解 #22）。
2. 按 SOP 跑 `scripts/device-acceptance-capture.ps1`，拿到对时基准 + 30s 会话采样 + 电平样本。
3. 会话结束立刻收 PCM，收完从 `.env` 移除 `JAX_DOWN_PCM_DUMP`（闭 #39）。
4. 把 `reply_id` / `frame_seq` 透传到 Android `onFirstAudioFrame`（闭 #38 的 L5 缺口）。
5. 显式 `JAVA_HOME=.../jdk17/jdk-17.0.20+8` 出包，用 dex 字符串校验产物后再装（解 #40）。
6. 重复采样 ≥30 轮，才谈得上 P95/P99。
