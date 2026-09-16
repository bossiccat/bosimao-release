# 真机取证 SOP（无线 ADB 恢复后直接执行）

版本：2026-09-07
适用：jax-pet / com.jax.voice，Samsung S26U + Tailscale 无线 ADB
目标：一次执行内拿到「静默基线 → 会话采集 30s → 增益复算 → 日志导出」的完整证据包
配套脚本：`scripts/device-acceptance-capture.ps1`（本 SOP 的自动化版本，手工执行按本文）

---

## 0. 执行前必读（本 SOP 的每一条坑都是上轮踩出来的）

| # | 坑 | 后果 | 本 SOP 的对策 |
|---|----|------|----------------|
| 1 | 手机**无线调试端口每次开启都会变**，锁屏/一段时间后监听自动关闭 | `adb connect` 10061 主动拒绝 | §1 先扫描再连；扫描→连接→装机→验收必须在**同一进程/同一次执行**内串完 |
| 2 | `adb reverse tcp:8443 tcp:8000` **绑定在 transport 上，每次重连都会丢** | 手机端 `Failed to connect to localhost/127.0.0.1:8443` | §1.5 每次 connect 后**必须重建** reverse |
| 3 | 设备 logcat 主缓冲被三星系统日志（PackageConfigPersister 等）刷爆，只留 **约 40~80 秒** | 跑完隔一分钟再抓，关键日志全没了 | §5/§7 用**后台流式落盘**，不要事后 `logcat -d` |
| 4 | 全量 `adb logcat -d` 在无线链路上回传 MB 级日志，实测把脚本拖到 10 分钟不返回 | 取证超时 | 过滤必须在**设备端**做（`--pid` + `shell` 侧 `grep -v`），不要拉回 PC 再过滤 |
| 5 | **类名 ≠ logcat TAG** | 用 `VoiceForegroundService` 去 grep 一条都匹配不到，关键统计全变 0，看起来像「功能没生效」 | §6 用真 TAG 常量清单 |
| 6 | DiagLog 只写 App 私有目录文件，**从不输出 logcat** | adb 里 BargeIn 事件计数永远为 0，易误判「没发生打断」 | §7.3 单独 `run-as` 取 `diag_log.txt` |
| 7 | `gradle UP-TO-DATE ≠ 装的是新包` | 拿旧包验新修复，得出假结论 | §2 用 **dex 字符串扫描**校验产物 |
| 8 | 采样周期必须**短于被观测过程的时间常数**（本次 32→14 只要 300ms） | 2s 采样只能看到混叠假象，误判为「参数没调好」 | §6 电平日志周期 500ms，采样 ≥30s |
| 9 | 后台 `logcat --pid` 会因 App 重启失效 | 重启后新 pid，过滤全空 | §7 用 TAG 过滤而非 `--pid`，或重启后重挂 |
| 10 | 本 bug 只在「远端播放 + 本地把回声当人声」时出现，**静默采样测不到** | 第一轮 0 个播放段，无法判读 | §5 采样窗口内**必须有人对着手机说话** |

---

## 1. 设备准备

### 1.1 手机端（需用户操作，无法自动化）

1. 设置 → 开发者选项 → **无线调试** → 打开
2. 进入「无线调试」→「使用配对码配对设备」→ **记下屏幕上显示的 host:port**
3. 保持屏幕常亮、不要锁屏（锁屏会关闭 adbd 监听）
4. 确认 App 已安装且是目标构建（见 §2）

### 1.2 PC 端：确认 adb 可执行

```bash
# 仓库内自带（优先）
tmp/task6-tools/platform-tools/adb.exe version
# 或系统已装
C:/Users/Administrator/Downloads/jax-build/android-sdk/platform-tools/adb.exe version
```

**判读**：报错「系统找不到指定的文件」= 路径写错；报错「连接被拒绝」= 路径对但设备不通。两者处置完全不同，先看清楚是哪一种。

### 1.3 扫描端口（端口会轮换，不要硬编码上次的）

手机 IP 走 Tailscale，历史值 `100.75.48.99`。端口每次重开都变（历史：38485 / 43783 / 44993 / 15463 / 36311 / 46813）。

```bash
# 并发扫 1024-50000，找 adbd
python tmp/adb_scan_full.py --host 100.75.48.99
```

若全端口无开放 = **无线调试已关闭**，回到 §1.1 让用户重开。

### 1.4 连接

```bash
adb connect 100.75.48.99:<扫描到的端口>
adb devices          # 必须看到 <ip:port>   device
```

**失败判读表**：

| 输出 | 含义 | 处置 |
|------|------|------|
| `由于目标计算机积极拒绝，无法连接。(10061)` | 手机在线但**该端口无监听** = 无线调试未开 | 回 §1.1，让用户重开并取新端口。**不是网络问题、不是 adb 缺失** |
| `无法连接到... (10060)` / 超时 | 网络不通 | 检查 Tailscale 是否在线、手机是否连同一 tailnet |
| `offline` | 未授权 | 手机点「允许 USB 调试」；仍 offline 则 `adb kill-server` 后重连 |

### 1.5 建立反向隧道（每次 connect 后必做）

```bash
adb reverse tcp:8443 tcp:8000
adb reverse --list        # 必须看到 tcp:8443 tcp:8000
```

丢失表现为手机端 `Failed to connect to localhost/127.0.0.1:8443`。

---

## 2. 校验装机产物（防「拿旧包验新修复」）

```bash
# 扫 APK 的 dex 字符串，确认新代码在包里
# 例：验 9604be4（回声自激限流）应能扫到 VOICE_INTERRUPT_REARM_MS / onset guard 相关字符串
unzip -p mobile-app/app/build/outputs/apk/debug/app-debug.apk classes*.dex | strings | grep -i "VOICE_INTERRUPT_REARM"
```

**不要用文件时间戳、不要用 gradle UP-TO-DATE 判断**。dex 字符串在不在，是唯一硬判据。

装机：

```bash
adb install -r mobile-app/app/build/outputs/apk/debug/app-debug.apk
```

签名不匹配会报 `INSTALL_FAILED_UPDATE_INCOMPATIBLE` → 用 `~/.android/debug.keystore`（alias `androiddebugkey`）。

---

## 3. 对时基准（没有它，跨设备时序全是猜的）

```bash
adb shell date -u +%s          # 手机 epoch 秒
adb shell date +"%Y-%m-%d %H:%M:%S.%N"
```

PC 侧同一时刻：

```powershell
[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
```

记录到 `notes/clocksync.txt`：

```
device_epoch=<...>   pc_epoch=<...>   offset_ms=<pc - device>
```

**规则**：

- 手机 logcat（`-v time`）用**设备时钟**；rtc_bridge / backend 日志用 **PC 时钟**；两者只在有 offset 时才能对齐。
- rtc_bridge 的 `t_enq` / `t_send` 是**进程内 monotonic**，与手机/PC 都不是同一基准，**跨进程相减无意义**，只允许进程内做差。
- offset 超过 1000ms 先修对时（手机开启自动网络时间），否则 §6 的增益时序判读不可信。

---

## 4. 静默基线采样（判定「底噪」与「门是否常开」）

前置：确认后端链路活着（不活着后面全是假阴性）：

```bash
curl -k https://127.0.0.1:8000/health          # 必须 -k / CERT_NONE，curl 默认静默失败
curl http://127.0.0.1:19093/health             # 看 pid / run_id，确认 bridge 是新实例
```

- 手机静置、**不说话**、环境保持目标场景（安静房 / 开视频噪声），采样 **30 秒**。
- 采集命令见 §5（同一条流水线，只是本轮人不开口）。

**判据**：

| 指标 | 期望 | 读法 |
|------|------|------|
| `gate=true` 占比 | 静默段应接近 **0**（历史故障值：98.8%） | 底噪被自己的增益放大越过门限 = 正反馈（D1），必须复现并定位 |
| `raw` 分位 | 记录 P50/P95/Max，作为该环境的底噪基线 | 换环境必须重采，GATE_RMS 是按特定房间校准的 |
| `out` | 静默段应 ≈ 0 | 非 0 说明噪声门形同虚设 |
| 电平日志条数 | 500ms 周期 → 30s ≈ 60 条 | 条数明显偏少 = 过滤写错或 TAG 写错（见 §6） |

---

## 5. 会话建立 + 采集 30 秒

### 5.1 启动采集（设备端过滤，后台流式落盘）

```bash
PID=$(adb shell pidof com.jax.voice)
adb logcat -v time --pid=$PID -s VoiceService:* VoiceSessionCoord:* BargeInCtrl:* CaptureGain:* RtcCustomAudio:* \
  > out/logcat-capture-$(date +%H%M%S).txt &
LOGCAT_PID=$!
```

> 若 App 会重启，**不要用 `--pid`**（重启后过滤全空），改用 `-s TAG:*` 纯 TAG 过滤。

### 5.2 触发会话（UI 自动化）

```bash
adb shell am start -n com.jax.voice/.MainActivity
adb shell uiautomator dump /sdcard/window.xml     # 必须 dump 到 /sdcard 再 cat，/dev/tty 不回传
adb shell cat /sdcard/window.xml
adb shell input tap <btnTalk 中心 x y>            # bounds 每帧可能位移，dump 后立刻 tap
```

**注意**：

- 建会话并启动 `RealCustomAudioSource` 的是 **btnTalk（立即对话）**；`btnToggleListen` 只启停 KWS 服务，**采集增益只发生在会话内**。
- **不能发 keyevent 4**（MainActivity 是根界面，BACK 直接回桌面，后续 dump 抓到 launcher）。
- 按钮 bounds 会随 `tvPhase` 文案变化位移（实测下移 84px），必须 dump 后立刻 tap 或重 dump。

### 5.3 采样窗口内必须做的事

对着手机**正常音量说 2~3 句完整的话**（例如「今天天气怎么样」「帮我记一下明天开会」），每句之间停 2 秒。

> 本轮目标缺陷（回声自激误打断 / 上行空窗 / 下行卡断）**只在「远端播放 + 本地把回声当人声」或「真实发声」时出现**，静默采样测不到。

采样 **30 秒**（`-SampleSeconds 30`），结束后：

```bash
kill $LOGCAT_PID
```

---

## 6. 复算增益（CaptureGainStage 判读）

### 6.1 TAG 常量清单（**类名 ≠ TAG**，用错一条都匹配不到）

| 类 | 真 TAG |
|----|--------|
| `VoiceForegroundService` | **`VoiceService`** |
| `VoiceSessionCoordinator` | `VoiceSessionCoord` |
| `BargeInController` | `BargeInCtrl` |
| `CaptureGainStage` | 走 `lvl ` 前缀电平日志（见下） |

### 6.2 电平日志格式（周期 500ms）

```
lvl raw=<原始rms> gain=<当前增益> out=<增益后rms> gate=<true|false> floor=<自适应噪声底> adp=<候选门槛>
```

### 6.3 复算步骤

```bash
# 1) 全量去重（历史坑：deploy_and_accept.py 会把日志打两遍，不去重会算出「2 倍实例」假结论）
grep "lvl raw=" out/logcat-capture-*.txt | sort -u > out/lvl.txt

# 2) 去掉高频洪水行再看状态机日志（5 路并发时电平日志约 10 条/秒，会把关键日志挤出缓冲）
grep -v "lvl raw=" out/logcat-capture-*.txt | grep "VoiceSessionCoord"

# 3) 统计
#    - gate=true 占比
#    - raw / out 的 P50 / P95 / Max
#    - gain 的收敛轨迹（看是否出现「静音段增益反向上冲」= D1 正反馈）
```

### 6.4 判据

| 指标 | 期望 | 历史故障值 |
|------|------|------------|
| 说话时 `out` | 落在 **2000~5000** | v1 实测到 9702（削波）；固定 ×32 实测 12346~19563 |
| 静默时 `out` | 0 | 早期 177（门常开） |
| 静默段 `gain` 轨迹 | 不应单调上冲 | D1 故障：raw 6→1→0 时 gain 21.1→30.1（正反馈） |
| 增益下调时间 | ≤ 约 300ms（ATTACK_DOWN=0.25） | D2 故障：SMOOTHING=0.05 双向对称，32→14 需 1.2s，窗口内持续削波 |
| `jax-rtc-capture` 线程数 | **1** | 故障值 5（采集源泄漏） |

线程普查（泄漏类问题一眼可见，比读日志快得多）：

```bash
adb shell "cat /proc/\$(pidof com.jax.voice)/task/*/comm | sort | uniq -c"
```

---

## 7. 导出日志

### 7.1 logcat（已在 §5 落盘）

```
out/logcat-capture-<HHMMSS>.txt
```

### 7.2 进程与线程快照

```bash
adb shell "ps -A | grep com.jax.voice"                                  > out/proc.txt
adb shell "cat /proc/\$(pidof com.jax.voice)/task/*/comm | sort | uniq -c" >> out/proc.txt
```

### 7.3 DiagLog（BargeIn 唯一可靠来源）

```bash
# debug 包
adb shell run-as com.jax.voice cat files/diag_log.txt > out/diag_log.txt
```

release 包 `run-as` 不可用 → 用 MainActivity 长按连接状态区弹窗导出。

### 7.4 打断判读（回声自激）

```
grep -c "interrupt source=user_voice" out/diag_log.txt     # 修复前 32，修复后应 0
grep -c "voice ignored" out/diag_log.txt                   # onset guard 拦下的次数
```

正常样本：

```
修复前  17:43:01.615 SPEAKING -> .625/.644/.664/.703/.724 五次 interrupt user_voice
修复后  18:33:13    SPEAKING -> .141/.162/.182/.202 四次 voice ignored: onset guard 13/34/54/74ms
        （播放继续，未被切断；播放段 2112ms 完整放完）
```

### 7.5 PCM 取证（若已开启 `JAX_DOWN_PCM_DUMP`）

落盘位置由 `.env` 的 `JAX_DOWN_PCM_DUMP=logs/dump/p0test-2306` 决定，取：

```
logs/dump/p0test-2306.pcm        # 下行
logs/dump/p0test-2306.up.pcm     # 上行
logs/dump/p0test-2306.meta.json  # 元数据
```

**注意**：固定前缀会被后续会话覆盖，**会话一结束立刻收**。取证结束后从 `.env` 移除该开关（否则 watchdog 拉起的每个实例都在写盘）。

---

## 8. 证据包清单（交付前自检）

```
out/
  clocksync.txt                 # 对时偏移
  logcat-capture-<HHMMSS>.txt   # 采样期 logcat
  proc.txt                      # 进程 + 线程普查
  diag_log.txt                  # BargeIn 事件
  lvl.txt                       # 去重后电平样本
  (可选) *.pcm / *.meta.json    # PCM 取证
```

缺任意一项，对应判据标「未实测」，**不允许用推测值填空**。

---

## 9. 一键执行

```powershell
.\scripts\device-acceptance-capture.ps1 -Device "100.75.48.99:46813" -SampleSeconds 30 -OutDir "out"
```

脚本内置本 SOP 的全部失败判读与处置建议（adb 未连接 / reverse 丢失 / pid 取不到 / 日志为空 等），每一步都会打印明确状态，失败时给可执行的下一步，而不是只吐一个错误码。
