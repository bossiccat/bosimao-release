# 更正（2026-09-16 18:50）：本文核心论断**已被推翻**，请勿据此行动

**本文的核心结论（"通话音频形态下本机向自采 AudioRecord 返回全零"）不成立。**

推翻它的实测：把"采样窗口"与"用户是否在说话"按时段对齐后，会话内 `RtcCustomAudio: lvl raw`
读到 **1029 / 718 / 426** 等峰值，且非零读数**成簇出现在说话窗口**，安静窗口才是 0。
即：**采集链路一直正常**，此前看到的 `raw=0` 测的都是"没人说话的静音段"。

因此本文中下列论证全部作废（它们都建立在静音样本之上）：
- "音效挂载（AEC/NS）是元凶" —— 那次 A/B 的对照与基线**都是静音**；
- "`MODE_NORMAL` 是解药" —— 从未做过对照实验（gradle 默认已改回 `KEEP`）；
- "帧长 320 vs 640" —— 前提不成立；
- "同机同麦、一条有声一条恒零" —— `MicRecorder` **会话中根本不运行**，该对照从来不是同时测的。

**本文仍保留的价值**：逐项排除的记录（权限/静音/传感器隐私/线程泄漏/采集源/读取失败/路由设备）
与"如何做同机同路径对照"的方法，可作为下次排查的检查表。

**真正的根因（同日 18:50 另测）**：手机侧建会话、进房、发布上行、采集全部正常，
但 TRTC 房间内 `user size` 恒为 1 —— **云端媒体面从未加入房间**，因此没有下行可播、
用户听到的是"说话没反应"。与采集无关。

---

# 真机采集恒零 —— 根因彻查报告

2026-09-16 · 设备 SM-S9480（Galaxy S26U）· Android 16 (SDK 36) · com.jax.voice 0.6.7(debug)

## 一、现象

会话建立成功（`IDLE→SIGNING→ENTERING→IN_ROOM` 正常），但上行采集**恒为零** ⇒ 用户说话无任何反应。

```
RtcCustomAudio: lvl raw=0 gain=8.0 out=0 gate=false floor=0.5    ← 每 500ms，从未非零
```

## 二、逐层取证（全部实测，非推断）

### 2.1 麦克风与系统侧：全部正常 —— 排除

| 检查 | 结果 |
|---|---|
| 系统录音机 | ✅ 有声音（用户实测） |
| 蓝牙/耳机 | ✅ 无外设 |
| `RECORD_AUDIO` 权限 | ✅ `allow (running)` |
| 麦克风静音（四机制） | ✅ `FromSwitch/FromRestrictions/FromApi/from system` 全 `false` |
| 会话 `silenced` | ✅ `silenced:false` |
| 全局传感器隐私 | ✅ 无开关置位 |
| 他人抢占麦克风 | ✅ 唯一 Requester 是本应用 |

### 2.2 采集线程与采集源：正常 —— 排除

| 检查 | 结果 |
|---|---|
| `jax-rtc-capture` 线程数 | ✅ **1 条**（历史曾泄漏到 5 条，本包正常） |
| `AudioSource` | ✅ **`MIC`**（`jax_capture_source=MIC`，文档规定的正确源，非"VC 全零"那个源） |
| `read()` 是否失败 | ✅ 每 20ms 稳定返回 320 样本（**不是读失败被当零**） |
| 计量算法 | ✅ `lastRawRms = rms(frame)`，正确 |

⇒ **交回的 320 个样本本身就是零。**

### 2.3 客户端音效挂载：**已被 A/B 实验否定**

`-PjaxCaptureEffects=NONE` 对照包（同时有 logcat 三行确证变体生效）下，`raw` **仍然恒为 0**。

⇒ 客户端挂 `AEC/NS/AGC` 不是原因。

### 2.4 客户端自采 vs KWS 采集：**同机同麦，差异被精确定位**

给两条路径加了同口径埋点后（`routedDevice` + 每 500ms 原始电平）：

| 项 | `MicRecorder`（**有声音**） | `RealCustomAudioSource`（**恒 0**） |
|---|---|---|
| `routedDevice` | `type=15 id=22 product=SM-S9480 addr=bottom` | **完全相同** |
| AudioSource / 格式 | MIC / 16k mono pcm16 | **相同** |
| 实测电平 | **39–72** | **0** |
| `dumpsys audio` 会话 | `dev=1ch`，`effects client=` **空** | **`dev=2ch`**，`effects client='aec' 'ns'` **且 `dev='aec' 'ns'`** |
| 录音会话条数 | 1 | **1** |

⇒ **"绑错麦"排除**（同一个麦）；**"TRTC 另开采集抢麦"排除**（只有一条会话）。
**差异只剩两处**：① `dev=2ch` ② **HAL 侧 `dev='aec' 'ns'` 生效**。

## 三、根因（调用顺序 + 历史同源）

`RtcClient.kt` 的实际顺序：

```
enterRoom(params, TRTCCloudDef.TRTC_APP_SCENE_AUDIOCALL)   :253
      ↓  onEnterRoom(result) 回调
   enableCustomAudioCapture(true)                          :149
   customAudioSource.start(cloud)                          :153  ← 自采 AudioRecord 在此创建
```

`TRTC_APP_SCENE_AUDIOCALL` 把设备切到**通话音频形态**（`MODE_IN_COMMUNICATION`；HAL 侧挂 AEC/NS、输入变 `2ch`）。
**我们的自采 AudioRecord 是在这个形态之后才创建的** ⇒ 三星在这条路径上**向应用返回全零**。

**这是本机已知陷阱的换入口重现** —— 代码注释与设计记录已写明：

- `RtcClient.kt:145-146`：「**SPEECH 档 VOICE_COMMUNICATION 源在本机（Samsung S26U，AGM LPI 路径）送出全零**，MUSIC 档（MIC 源）有声但无 AEC/NS」
- `CaptureGainStage.kt:48`：「VOICE_COMMUNICATION 源送全零，是死路」
- `RealCustomAudioSource.kt:56-60`：「TRTC SPEECH 档绑 VC 源在本机曾送全零，VC 变体若 lvl raw 恒 0 即同路径静音」

当年为了拿到 AEC/NS，团队从「TRTC 内部采集（SPEECH 档）」改为「**自建 AudioRecord(MIC) + 客户端 AEC/NS**」，
**但场景仍是 `AUDIOCALL`（通话形态）** ⇒ **以新的入口踩进了同一个坑**：
「通话形态下，本机对应用自采返回全零」这一条约束**从未被解除**，只是从"源"换到了"场景"。

**为什么 `MicRecorder` 有声音**：它在通话场景之外建立，走 `dev=1ch`、无 HAL 音效的**普通录音**形态。

## 四、结论

> **根因不是麦克风、不是权限、不是采集源、不是客户端音效，也不是线程泄漏。**
> **根因是：上行采集运行在 `TRTC_APP_SCENE_AUDIOCALL` 建立的"通话音频形态"之下，而本机
> （Samsung S26U / AGM-LPI 路径）在该形态下向应用自建的 AudioRecord 返回全零。**

## 五、修复方向（按推荐度，均需真机验证）

| # | 方案 | 说明 | 风险 |
|---|---|---|---|
| **A** | **回到 TRTC 内部采集**（去掉自采），用 `TRTC_APP_SCENE_AUDIOCALL` 的 SDK 内置 AEC/NS | 这是 TRTC 的标准用法；MUSIC 档当年"有声" | 需要确认 AUDIOCALL 档内部采集在本机**不**走全零路径（当年的"全零"是 SPEECH 档 + VC 源）——**必须先实测** |
| **B** | 保持自采，但**在建自采前把音频模式显式置回** `MODE_NORMAL`，建完恢复 | 直击根因（形态） | TRTC 的回声参考可能依赖该模式；需 A/B |
| **C** | 保持自采，但改 `TRTC_APP_SCENE_VOICE_CHAT`/`LIVE`（非通话形态） | 形态不同 | 失去平台回声抑制，可能引入新回声 |
| **D** | 自采用 `setPreferredDevice` 显式指定 1ch 内置麦 | 若有 1ch 变体 | 不改变形态，可能无效 |

**建议先做 A 与 B 的 A/B**（两者都能一次重建判掉），并**每次都取 `dumpsys audio` 的 `dev=`/`effects dev=` 作为形态证据**。

## 六、尚未验证 / 边界

- 方案 A/B/C/D **均未实测**，本文只到"根因锁定"这一步。
- `dev=2ch` 与 HAL 侧 `aec/ns` 是**现象**；"三星在该形态下返回全零"是从现象 + 历史记录推出的**结论**，尚未用最小复现（裸 AudioRecord + AUDIOCALL 形态）单独证实。
- 本机是否在 `MUSIC`/非通话场景下**必然**有声，只由当年的注释支持，本次未复测。

## 七、本次排查修掉的观测缺口（值得保留）

- `MicRecorder`（KWS）路径**此前没有任何电平日志** ⇒ 被迫用"唤醒次数"这种间接计数判断"有没有声音"，
  **并在本次排查中连续误导了两轮结论**。已补 `routedDevice` + 每 500ms 原始电平埋点（与自采路径同口径）。
- 铁律：**判断"有没有信号"必须用直接量（电平）；间接计数只在前提已被证明时才能用。**
