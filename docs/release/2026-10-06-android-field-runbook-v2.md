# Android 现场取证 Runbook v2（claim: android-duplex-audio 门禁）

> **Changelog**
> - 2026-10-06（HEAD `35df39d`）：全面审计并重写。前代 SOP 为 `docs/reports/device-capture-sop-2026-09-07.md`；任务单引用的 `docs/release/2026-09-26-android-field-runbook.md` 在工作区与全 git 历史（`git log --all`）中均不存在，本文件为当前唯一权威版本。
> - 相对 09-07 SOP 的漂移修正：① TAG `CaptureGain:*` 已不存在（CaptureGainStage 纯计算零日志），电平日志实际 TAG 为 `RtcCustomAudio`（会话内）/ `MicRecorder`（会话外）；② 新增 `DeviceEnv` TAG（场景4/5 可观测性，已接线 VoiceForegroundService.kt:100/:469）；③ UI 商业化后主界面开发字段隐藏，**长按连接状态区导出 diag 的入口失效**（锚点 tvConnection 已 gone），diag 导出依赖 debug 包 run-as；④ `stopLocalAudio` 在生产代码已不调用（自定义采集替代），场景3 teardown 证据行相应更正；⑤ logcat 过滤 TAG 清单扩充（RtcClient/RtcPlayback/VoiceSessionApi 等）；⑥ 证据入库流程锚定 `scripts/record-release-evidence.py`（2026-09-24 起门禁只认场景覆盖 PASS）。
> - 本文件审计时所有 file:line 与日志文案均对照 HEAD `35df39d` 源码核验。手机上线只有一次机会：先读完 §0-§3，再上场。

---

## 0. 目标与硬约束

- claim：`governance/claims/android-duplex-audio.json`，state=EvidencePending，6 个 required_scenarios 全部要 PASS。
- 交付：**1 个证据 bundle 文件**（§7）+ 6 条场景覆盖判定，由 `scripts/record-release-evidence.py` 写入 claim。
- 手机无线调试端口每次开启都会变；logcat 主缓冲只留 40~80 秒 → **扫描→连接→装机→采集必须在同一次执行内串完，全程后台流式落盘**。
- 本 runbook 全程 READ-ONLY on code；只写 `out/` 与 claim。

## 1. 连接（无线 adb over Tailscale）

手机 Tailscale IP 历史 `100.75.48.99`；端口轮换（历史 38485/43783/44993/15463/36311/46813），**不要硬编码**。

```bash
# 手机端（需用户）：设置→开发者选项→无线调试→开启→记下配对码页 host:port；保持亮屏不锁
tmp/task6-tools/platform-tools/adb.exe version        # 仓库内 adb（已核实在位）
python tmp/adb_scan_full.py --host 100.75.48.99       # 扫出 adbd 端口
tmp/task6-tools/platform-tools/adb.exe connect 100.75.48.99:<端口>
tmp/task6-tools/platform-tools/adb.exe devices        # 必须看到 <ip:port>  device
tmp/task6-tools/platform-tools/adb.exe reverse tcp:8443 tcp:8000   # 每次 connect 后必做，重连即丢
tmp/task6-tools/platform-tools/adb.exe reverse --list # 必须看到 tcp:8443 tcp:8000
```

| 失败输出 | 含义 | 处置 |
|---|---|---|
| 10061 积极拒绝 | 该端口无监听 = 无线调试未开 | 回手机端重开，取新端口 |
| 10060/超时 | 网络不通 | 查 Tailscale 在线状态 |
| `offline` | 未授权 | 手机点「允许 USB 调试」；仍 offline 则 `adb kill-server` 重连 |

## 2. 装机与产物校验（防拿旧包验新代码）

**取证必须装 debug 包**：`run-as` 读 diag 文件依赖 debuggable；release 包的 UI 导出入口已随商业化隐藏（见 §6.3），没有替代路径。

```bash
# 1) 构建后先做 dex 字符串扫描（唯一硬判据；不信 gradle UP-TO-DATE、不信时间戳）
unzip -p mobile-app/app/build/outputs/apk/debug/app-debug.apk classes*.dex | strings | grep "bluetooth_a2dp"
# ↑ DeviceEnvObserver 的设备类型标签，HEAD 35df39d 新代码必在包里；扫不到=旧包，禁止上场
# 2) 装机
tmp/task6-tools/platform-tools/adb.exe install -r mobile-app/app/build/outputs/apk/debug/app-debug.apk
# 签名不匹配 INSTALL_FAILED_UPDATE_INCOMPATIBLE → 先卸载手机上的旧包再装
# 3) 产物绑定：记录本次 APK 的 sha256（claim target 用）
certutil -hashfile mobile-app/app/build/outputs/apk/debug/app-debug.apk SHA256
```

## 3. 对时与链路预检

```bash
tmp/task6-tools/platform-tools/adb.exe shell date -u +%s     # 手机 epoch
powershell -c "[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()" # PC epoch，记 offset 到 out/clocksync.txt
curl -k https://127.0.0.1:8000/health    # 后端必须活；-k 必须
curl http://127.0.0.1:19093/health       # rtc_bridge pid/run_id，确认新实例
# 手机端 App：设置页确认服务器指向 PC（localhost:8443 走 reverse）
```
offset >1000ms 先修对时（手机开自动网络时间），否则跨设备时序判读不可信。logcat 用设备时钟，bridge/backend 用 PC 时钟；`t_enq/t_send` 是进程内 monotonic，跨进程相减无意义。

## 4. 采集（全程后台流式落盘，TAG 过滤）

TAG 常量清单（**类名 ≠ TAG**，v2 已对照源码逐一核实）：

| TAG | 源 | 用途 |
|---|---|---|
| `VoiceSessionCoord` | VoiceSessionCoordinator.kt:63 | 场景1/2/3 状态机主线 |
| `RtcClient` | RtcClient.kt:61 | enterRoom/exitRoom/超时兜底/TRTC error |
| `RtcCustomAudio` | RealCustomAudioSource.kt:43 | 会话内采集：`lvl raw= gain= out= gate= floor= adp=`（500ms 周期，LEVEL_LOG_FRAMES=25） |
| `MicRecorder` | MicRecorder.kt:36 | 会话外 KWS 电平（`lvl raw= n= routedType=`，格式不同） |
| `DeviceEnv` | DeviceEnvObserver.kt:34 | **场景4/5**：audio_device/screen/network/lifecycle 事件 |
| `VoiceService` | VoiceForegroundService.kt:41 | pipeline built/started/released、mic restarted |
| `BargeInCtrl` | BargeInController.kt:45 | 打断（场景6） |
| `RtcPlayback` / `VoiceSessionApi` / `WakeWordEngine` / `MainActivity` / `JaxApp` | 各类 TAG 常量 | 辅助 |

```bash
# 不用 --pid（App 重启即失效）；纯 TAG 过滤
tmp/task6-tools/platform-tools/adb.exe logcat -v time -s \
  VoiceService:* VoiceSessionCoord:* BargeInCtrl:* RtcCustomAudio:* MicRecorder:* \
  DeviceEnv:* RtcClient:* RtcPlayback:* VoiceSessionApi:* WakeWordEngine:* MainActivity:* \
  > out/logcat-field-$(date +%H%M%S).txt &
```

> 注意：`onEnterRoom result=` **只写 DiagLog 文件、不出 logcat**（RtcClient.kt:139），场景1 必须另取 diag_log.txt（§6）。BargeIn 事件同理只有 diag 可靠。
> 一键脚本 `scripts/device-acceptance-capture.ps1` 仍在，但其内置 TAG 表含已消亡的 `CaptureGain:*` 且缺 `DeviceEnv`/`RtcClient`（脚本属只读范围未改）；用它兜底可以，场景4/5 证据必须用上面手工命令补采。

## 5. 六场景执行矩阵（每格 = 操作 → 期望证据行，全部在采样窗口内完成）

### S1 enterRoom 异步成功回调状态机
主界面点「**立即对话**」（btnTalk，activity_main.xml ID 未变；入口 `VoiceEntry.startConversation(this,"main")`）→ 依次应看到：
`VoiceSessionCoord: start requested source=main` → `start accepted ... state=IDLE->SIGNING` → `sign succeeded ... state=SIGNING->ENTERING` → `RtcClient: enterRoom room=... [rtc#N]` → diag `Rtc onEnterRoom result=<正数ms>` → `enter succeeded ... state=ENTERING->IN_ROOM` → `RtcCustomAudio: custom capture started ... captureThreads=1`。
**判据**：全链顺序出现、无 conflict 行、captureThreads=1。

### S2 失败/超时/取消/晚回调/重入的幂等 teardown（负路径，单独再开一个 logcat 文件）
- 重入：IN_ROOM 中再点立即对话 → `conflict #N: start(main) while IN_ROOM`，会话不受扰。
- 签发超时：飞行模式发起 → `timeout phase=SIGNING` + `timeout while SIGNING -> IDLE`（10s）。
- 进房超时：sign 后立即断网 → `RtcClient: onEnterRoom timeout (15000ms): forcing enter failure recovery` + `failure code=enter_timeout` + `failure while ENTERING -> EXITING`（coordinator 15s 兜底）。
- 取消：会话中通知栏停止/再点 → `cancel enqueued`；SIGNING 期取消 → `cancel while SIGNING -> IDLE (no exit effect)`；IN_ROOM 期取消 → `->EXITING`。
- 晚回调：退出后迟到的 SDK 回调 → `stale/illegal EnterSucceeded/SignSucceeded gen=...`（conflict 计数）。
- 退出超时兜底：`timeout while EXITING -> IDLE (exit callback missing, forced)`（5s）。
**判据**：每条负路径都回到 IDLE、无永久锁、conflicts 有留痕。

### S3 exitRoom 与 stopLocalAudio（= 自定义采集对称关闭）
正常退出会话 → `VoiceSessionCoord: ->EXITING ... exitTimeoutMs=5000ms` → `RtcClient: exitRoom [rtc#N] inRoom=true pendingEnter=false` → `RtcCustomAudio: custom capture stopped inst=... captureThreads=0` → diag+logcat `RtcClient: onExitRoom reason=0 (0主动退出/1被踢/2房间解散)` → `exit succeeded ... ->IDLE` → `VoiceService: mic restarted after session (listening resumed)`。
**架构说明（写给 reviewer）**：2026-09-05 起上行改为自定义采集，生产代码不再调用 `stopLocalAudio`（teardown = `customAudioSource.stop()` + `enableCustomAudioCapture(false)` + `exitRoom()`，RtcClient.kt:276-306；`stopLocalAudio` 仅存于测试 mock）。本场景按上述证据行判定。

### S4 下行播放 / 蓝牙 / 耳机 / 音频焦点（DeviceEnv 已接线：VoiceForegroundService.kt:100 onCreate→start，:469 onDestroy→stop）
会话中依次：① 播报中插/拔 3.5mm 耳机 → `DeviceEnv: audio_device added/removed type=wired_headset`；② 开/关蓝牙音箱 → `type=bluetooth_a2dp`（SCO 则 `bluetooth_sco`）；③ 正常听完整回复 + 一次语音打断 → `RtcCustomAudio lvl` 持续、`BargeInCtrl` 打断行、diag 中 playback 段完整。
**判据**：`DeviceEnv: observer started` 基线在会话前已出现（baseline 快照）；每次设备增删都有 added/removed 行；下行播放在设备切换后仍恢复出声。
（App 层不请求音频焦点、播放走 SDK——焦点行为本身由 TRTC SDK 承担，本场景可采证据 = DeviceEnv 设备事件 + lvl + 主观听感记录，如实写，不推测。）

### S5 锁屏 / 后台 / 网络切换
会话中：① 按电源键锁屏 10s → 解锁：`DeviceEnv: screen SCREEN_OFF / SCREEN_ON / USER_PRESENT`，之后会话仍 IN_ROOM（无 failure 行）；② Home 退后台 30s → 回前台，会话存活；③ 关 Wi-Fi 走蜂窝 → `DeviceEnv: network lost ...` + `network capabilities ... transport=cellular`，TRTC SDK 内置重连（`RtcClient: TRTC error ...` 后自愈；对端侧观察桥接日志）。反向切回同理。
**判据**：三类事件各有 DeviceEnv 行；全程会话未死（状态机无 failure/timeout）。

### S6 连续两轮真实双向语音
对着手机正常音量说 2~3 句完整的话→听回复→再打断→再回复，两轮不间断。**判据**：说话段 `lvl raw` 明显抬升、`out` 落 2000~5000、静默段 `out≈0` 且 `gate=false`、`gain` 无单调上冲（D1 正反馈特征：raw 降 gain 升）；打断由 diag `interrupt source=user_voice`/`voice ignored` 计数佐证；线程普查 `jax-rtc-capture`=1：

```bash
tmp/task6-tools/platform-tools/adb.exe shell "cat /proc/\$(pidof com.jax.voice)/task/*/comm | sort | uniq -c" > out/proc.txt
```

## 6. 导出（会话结束立刻做）

```bash
# 6.1 diag_log.txt（S1 onEnterRoom result、BargeIn、DeviceEnv 双写的唯一可靠来源；debug 包）
tmp/task6-tools/platform-tools/adb.exe shell run-as com.jax.voice cat files/diag_log.txt > out/diag_log.txt
# 6.2 logcat 各分段已在 §4/§5 落盘 out/logcat-field-*.txt
# 6.3 release 包 UI 导出入口已失效（商业化后 tvConnection 行 visibility=gone，长按锚点不可达，
#     MainActivity.kt:124 的监听仍在但收不到事件）——这就是必须用 debug 包取证的原因，勿现场试 release。
```

## 7. 证据入库（机械条件由工具保证，人工只对真实性负责）

6 个场景证据**合并为一个 bundle 文件**（工具每次写入单条 evidence，覆盖式）：
`out/android-duplex-audio-bundle-<YYYYMMDD>.txt` = 对时记录 + 各场景 logcat 分段（保留原始行，标场景分隔头）+ diag_log.txt + proc.txt + lvl 统计（gate 占比 / raw,out P50/P95/Max / gain 轨迹）+ 每场景 PASS 判定与依据行号。

```bash
./.venv/Scripts/python.exe scripts/record-release-evidence.py \
  --claim-id android-duplex-audio --kind android-field \
  --evidence out/android-duplex-audio-bundle-<YYYYMMDD>.txt \
  --artifact mobile-app/app/build/outputs/apk/debug/app-debug.apk \
  --owner impl-team --reviewer independent-qa \
  --collected-at <采集实际完成时刻 UTC，如 2026-10-06T12:18:18Z> \
  --scenario-coverage "enterRoom 异步成功回调状态机=PASS" \
  --scenario-coverage "失败/超时/取消/晚回调/重入的幂等 teardown=PASS" \
  --scenario-coverage "exitRoom 与 stopLocalAudio=PASS" \
  --scenario-coverage "下行播放 / 蓝牙 / 耳机 / 音频焦点=PASS" \
  --scenario-coverage "锁屏 / 后台 / 网络切换=PASS" \
  --scenario-coverage "连续两轮真实双向语音=PASS"
```

硬规则（工具已强制，违者拒写）：`kind` 必须 `android-field`（policy）；场景文案与 claim `required_scenarios` **逐字一致**且全 PASS（2026-09-24 起缺项/非 PASS 以 SCENARIO_COVERAGE_INCOMPLETE 拒绝）；`reviewer ≠ owner`；`expires_at = collected_at + 72h`（故 `--collected-at` 填真实采集时刻，采集后尽快入库，超 72h 证据作废）；`raw_sha256`（`sha256:<hex>`）工具按文件字节现算——bundle 落盘后**不得再改**，改了必须重算重录。写盘后跑 `scripts/check-release-blockers.py` 复核。某场景未实测就如实填 FAIL 并补测，**禁止用推测值填空**。

## 8. 上线即采 checklist（手机上线后从上到下执行，≤40 步）

1. 手机：开发者选项 → 无线调试开启 → 记配对码页 host:port → 保持亮屏。
2. PC：`tmp/task6-tools/platform-tools/adb.exe version` 可执行。
3. `python tmp/adb_scan_full.py --host 100.75.48.99` 扫端口。
4. `adb connect 100.75.48.99:<端口>`；`adb devices` 见 `device`。
5. `adb reverse tcp:8443 tcp:8000`；`adb reverse --list` 确认。
6. `curl -k https://127.0.0.1:8000/health` 通；`curl http://127.0.0.1:19093/health` 见新 run_id。
7. dex 扫描：`unzip -p .../app-debug.apk classes*.dex | strings | grep bluetooth_a2dp` 非空。
8. `certutil -hashfile .../app-debug.apk SHA256` 记产物哈希。
9. `adb install -r .../app-debug.apk` 成功。
10. `adb shell date -u +%s` 与 PC epoch 记入 out/clocksync.txt（offset≤1000ms）。
11. 手机打开 App，完成登录/权限，回主界面。
12. 启动后台流式 logcat（§4 命令，全 TAG 清单）→ out/logcat-field-<t1>.txt。
13. 确认 logcat 已出现 `DeviceEnv: observer started`（无 = 未装新包，停止并回步 7）。
14. 【S1】点「立即对话」；核对 start requested→accepted→sign succeeded→enterRoom→enter succeeded→IN_ROOM→custom capture started(captureThreads=1)。
15. 记下 S1 证据行号，勿关 logcat。
16. 【S2a】IN_ROOM 中再点立即对话 → 应有 `conflict #N: start(main) while IN_ROOM`。
17. 【S2b】通知栏停止会话 → cancel/EXITING/IDLE 链完整。
18. 【S2c】飞行模式发起新会话 → `timeout while SIGNING -> IDLE`；关飞行模式。
19. 【S2d】重进会话后立即断网 → `onEnterRoom timeout (15000ms)` + enter_timeout 链；恢复网络。
20. 结束 S2，kill 该 logcat，另起 out/logcat-field-<t2>.txt。
21. 【S3】正常进会话再正常退出 → exitRoom→custom capture stopped(captureThreads=0)→onExitRoom reason=0→exit succeeded→mic restarted。
22. 确认回到监听态（「监听中」或 btnTalk 可再点）。
23. 【S4】会话中插拔 3.5mm 耳机 → `DeviceEnv audio_device added/removed type=wired_headset`。
24. 【S4】开关蓝牙音箱 → `type=bluetooth_a2dp`；切后下行仍出声。
25. 【S4】完整听一条回复 + 一次语音打断成功。
26. 【S5】锁屏 10s → 解锁：SCREEN_OFF/SCREEN_ON/USER_PRESENT 三行齐，会话仍 IN_ROOM。
27. 【S5】Home 退后台 30s → 回前全会话存活。
28. 【S5】Wi-Fi↔蜂窝切换 → network lost/capabilities transport 行，SDK 自愈。
29. 结束 S4/S5，kill 该 logcat，另起 out/logcat-field-<t3>.txt。
30. 【S6】连续两轮：说 2~3 句→听完整回复→打断→再回复，全程对着手机正常音量。
31. 线程普查 → out/proc.txt；确认 `jax-rtc-capture` 计数=1。
32. kill 全部 logcat。
33. `adb shell run-as com.jax.voice cat files/diag_log.txt > out/diag_log.txt`。
34. lvl 统计：说话段 out∈2000~5000、静默 out≈0 gate=false、gain 无上冲；BargeIn/diag interrupt 计数核对。
35. 合并 bundle：out/android-duplex-audio-bundle-<date>.txt（对时+三段 logcat+diag+proc+统计+逐场景 PASS 判定）。
36. `certutil -hashfile <bundle> SHA256` 自留底；bundle 此后不得改动。
37. 跑 §7 record-release-evidence.py（6 条场景逐字 PASS；--collected-at=真实采集时刻）。
38. 工具打印 Verified + 哈希 → 跑 `scripts/check-release-blockers.py` 复核。
39. 把 bundle 路径、APK sha256、claim diff 交给 lead / independent-qa 复核。
40. 手机端：关闭无线调试；PC 端：确认 claim 已入库、证据未过期（≤72h）。
