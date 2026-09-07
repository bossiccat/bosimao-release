# 决策归位重构方案（Decision-Relocation Refactor）

- 日期：2026-09-07
- 状态：PROPOSED（待用户批准后执行）
- 前置证据：commit ece7cf2（握手确认）/ 4d6bcf1（barge-in 防误杀）/ 5d91ef1（播放期门控+上行解耦）；logs/rtc_bridge_app.log 15:00:41.931→41.952（21ms 误杀）
- 关联规格：SPEC §11-3（采集源规定，本方案提出修订）

---

## 1. 诊断：为什么一直在"修修补补"

### 1.1 病根（代码自证）

```
MicRecorder.kt:69        → 采集用 AudioSource.MIC
MicRecorder.kt:13        → spec §11-3 规定"必须用 MIC，勿用 VOICE_COMMUNICATION"
BargeInController.kt:25  → 自证：AcousticEchoCanceler 为 VOICE_COMMUNICATION 设计，
                           用 MIC 时"effect 创建成功但实际是空操作"
```

**因果链**：spec §11-3 当年为保留原始信号（v3 增益级 + KWS 需要未加工信号）选择 MIC →
平台 AEC 无回声参考 = 空转 → 扬声器回声残差必然进入上行 → 假 VAD / 假打断 →
各层加阈值止血。

### 1.2 补丁家族全景（三层启发式对抗同一个回声）

| 层 | 补丁 | 代价 |
|---|---|---|
| 手机 BargeInController | onset 400ms 窗 + **每播放段只许 1 次语音打断** + 3s rearm 兜底 | 用户对同一条回复只能插一次话（真实体验损失） |
| 手机 CaptureGainStage | 噪声门 + 自适应增益（独立问题，保留） | — |
| PC bridge | 800 RMS 单帧判定 → 宽限 0.5s + 持续 3 帧（4d6bcf1）→ 播放期门控（5d91ef1） | 阈值族需按设备/房间/音量调参，永远修不完 |

**结论**：修补不是工程能力问题，是结构性裂缝的利息。裂缝 = 采集源决定导致 AEC 空转，
全双工决策跑在污染信号上。

### 1.3 什么不是问题（不推倒重来的依据）

- 四跳链路（手机→TRTC→PC bridge→Qwen）是产品概念（PC agent 宿主）决定，非缺陷
- 运输层实测达标：TTFB 47-672ms、会话到首事件 ~2s（ChatGPT AV 区间 0.3-1s+）
- 握手确认、队列解耦、F6/F7 埋点是标准件，保留不退役

---

## 2. 目标终态

1. **上行干净**：回声在采集侧被真 AEC（或软件参考抑制）消除，上行全程流动（真全双工）
2. **决策语义化**：打断判定只存在于两处——手机（干净信号+播放参考）或云端 smart_turn（干净
   上行后可信赖）；bridge 退化为纯转发 + 会话管理
3. **补丁族退役**：800 RMS / 宽限期 / 持续帧 / 每段一次 / 播放期门控全部下线（flag 保底）
4. **量化验收**：AI 回复完整率 100%（无 flush 掐断）、真实插话 P95 ≤300ms（AC-13 复测）、
   回声零 commit（无 ttfb<100ms 垃圾 response）

---

## 3. 执行计划（先做最便宜的决定性实验，再决定造不造机器）

### M0 决定性实验（半天）：采集源 A/B

- **改动**：MicRecorder 采集源 flag 化（`MIC | VOICE_COMMUNICATION`），默认保持 MIC 不变
- **取证**：真机播放固定测试音频，双源各采 30s，三组读数：
  ① 播放段 mic 回声耦合（播放段 up RMS vs 静默段本底）
  ② v3 门放行率/增益曲线是否被平台 AGC/NS 扰动
  ③ KWS 唤醒字检出率
- **判定**：VOICE_COMMUNICATION 下播放段假触发≈0 且 KWS/门控可接受 → **M0 直通 M2**
  （平台 AEC 生效，无需自造回声抑制）；否则进 M1
- **真机门 G0**：数据落盘 `outputs/m0-aec-ab-2026-09-XX.md`
- **注意**：本方案同时是 SPEC §11-3 的修订提案——原规定有正当理由（原始信号），
  M0 用数据裁决两个目标的冲突

### M1 软件回声抑制兜底（仅 M0 失败时，3-4 天）

- 能量域自适应回声阈：播放参考手机本地可得（`RtcPlaybackSubscription` /
  `RealCustomAudioSource` 持有播放 PCM），阈值 = f(播放 RMS, 路径衰减估计)
- JVM 单测先行（EchoAwareGate 纯函数化，时钟/RMS 注入，仿 BargeInController 测试模式）
- **真机门 G1**：bridge 端播放段 up rms 回落至本底水平（与 M0 基线数据对比）

### M2 bridge 决策退役（1-2 天）

- env flag：`RTC_BRIDGE_LOCAL_BARGE_IN=off` 时，本地 RMS barge-in 与播放期门控全部短路，
  barge-in 语义 = Qwen speech_started（M0/M1 保证上行干净后该事件才可信赖）
- **kill-switch 必须保留**：出问题一条 env 切回 5d91ef1 行为
- **真机门 G2（三条全过才算过）**：
  ① AI 回复完整率 100%（连续 10 轮无 barge flush 掐断）
  ② 真实插话生效：用户开口 → speech_started → flush → 新 response，P95 ≤300ms
  ③ 零回声 commit：全程无 ttfb<100ms 的瞬时 response
- 对应 pytest：session 门控 flag 化后的行为分支（现有 test_uplink_playback_gating 扩展）

### M3 补丁族退役（半天）

- 删：手机端"每段一次"限制与 onset 窗（AEC 生效后失去存在理由）、PC 端宽限/持续帧
  （降级为 debug flag 或直接删除）
- 改：SPEC §11-3 修订落稿（新采集源规定 + 理由 + M0 数据引用）
- 回归：backend 全量 + JVM 全量 + 真机 AC-13/AC-14 复测

---

## 4. 测试与真机矩阵

| 层 | 内容 |
|---|---|
| JVM | EchoAwareGate（若走 M1）、采集源抽象、BargeInController 退役后的新语义 |
| backend pytest | session 门控/决策 flag 分支、barge-in 语义切换 |
| 真机 | S26U × 外放音量（大/中/小）× 环境（安静/日常）× 流程（正常对话/播放中插话/连续长句） |

## 5. 风险与回退

| 风险 | 缓解 |
|---|---|
| VOICE_COMMUNICATION 自带 AGC/NS 干扰 v3 增益与 KWS | M0 三组读数裁决；flag 随时切回 |
| 平台 AEC 对自定义 AudioTrack 播放无参考（部分机型） | M1 软件兜底路径已在方案内 |
| M2 关闭本地决策后云端判定延迟偏高 | G2 门 ② 卡 P95 ≤300ms，不过则保留手机侧快路径 |
| 商业节奏 | 全程维持 PRR Bronze / NO-GO 纪律，重构不改变放行门 |

## 6. 执行顺序与工作量

M0（0.5 天）→ [G0 判定] → M2（1-2 天）→ M3（0.5 天）；
仅当 G0 失败插入 M1（3-4 天）。总计 2-3 天（直通路径）/ 6-7 天（含 M1）。
