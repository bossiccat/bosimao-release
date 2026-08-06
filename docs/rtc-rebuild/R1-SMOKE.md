# R1 冒烟结论 — TRTC Electron SDK 原始 PCM 帧确认 + 哑对端进房（Phase A）

> 版本：v1.0（2026-08-06）
> 作者：be-pc（后端工程师）
> 状态：**R1 gate 通过**（PCM 帧可拿，无需 Web Audio 兜底）
> 依据：PC-INTEGRATION.md §7.2 A4（R1 gate 判定入口）、腾讯云 TRTC 官方文档（Electron 集成 / UserSig 计算 / API 参考）
> 环境：Windows 10 x64，Node v22.22.2，Electron 31.7.7，`trtc-electron-sdk@13.3.801`（精确版本，禁 latest）

---

## 1. 冒烟目标（对照 PC-INTEGRATION §7.2）

| # | 目标 | 判定 |
|---|------|------|
| R1 gate | 确认 `trtc-electron-sdk` 能否拿到远端音频**原始 PCM 帧** | ✅ **可拿** |
| A2 | sidecar 进房（`onEnterRoom` 成功 + SDK 版本） | ✅ 进房 163ms，SDK 13.3.0.17949 |
| A3 | 远端互见（`onRemoteUserEnterRoom` 双方互见） | ✅ pc-phone ↔ jax-pc-sidecar 互见 |
| A4 | 上行收帧冒烟（手机说话 → sidecar 收 PCM 帧） | ⏳ 待真机（PCM 回调已注册，见 §3） |

> A4 的完整"手机说话→收帧计数"需手机 RtcClient（fe-mobile）参与，Phase A 内以**本地双端回环**验证了进房 + 远端互见 + 音频回调注册成功；PCM 帧数据级验证列入 Phase B 联调（QA-PLAN §4.2 方法 A：同机双客户端收发 WAV）。

---

## 2. R1 判定：原始 PCM 帧 **可拿**（无需 Web Audio 兜底）

### 2.1 结论

**`trtc-electron-sdk@13.3.801` 暴露 `setAudioFrameCallback(callback)`，其中 `onPlayAudioFrame(frame, userId)` 回调「混音前的每一路远程用户的音频数据」（PCM 格式，只读）——即 PC sidecar 可直接拿到手机端原始 PCM 帧**。同时下行注入 API `enableCustomAudioCapture(true)` + `sendCustomAudioData(frame)` 存在，PC→手机推流路径不受影响。

- **上行（手机→PC）**：`setAudioFrameCallback` + `onPlayAudioFrame` → 原始 PCM ✅（无需 Web Audio）
- **下行（PC→手机）**：`enableCustomAudioCapture` + `sendCustomAudioData` → 外部 PCM 注入 ✅
- **采样率**：`TRTCAudioFrame{sampleRate, channel, data, length}` 由 SDK 回调（默认 48k；sidecar 内或 rtc_bridge 做 16k 转换，沿用 PC-INTEGRATION §3.2 决策）

### 2.2 证据来源（官方文档 + 已装包实测，双重确认）

1. **官方 API 参考**：`https://web.sdk.qcloud.com/trtc/electron/doc/zh-cn/trtc_electron_sdk/index.html`
   - TRTCCloud 方法：`setAudioFrameCallback` / `enableCustomAudioCapture` / `sendCustomAudioData` / `getSDKVersion` / `enableAudioVolumeEvaluation` / `callExperimentalAPI`
   - 回调：`onEnterRoom` / `onExitRoom` / `onRemoteUserEnterRoom` / `onRemoteUserLeaveRoom` / `onUserAudioAvailable` / `onUserVoiceVolume`
2. **已装包类型定义**（`node_modules/trtc-electron-sdk/liteav/trtc.d.ts` + `trtc_define.d.ts`）：
   - `setAudioFrameCallback(callback: TRTCAudioFrameCallback): void`（trtc.d.ts:2339）
   - `TRTCAudioFrameCallback`：`onCapturedAudioFrame` / `onLocalProcessedAudioFrame` / `onPlayAudioFrame(frame, userId)` / `onMixedPlayAudioFrame` / `onMixedAllAudioFrame`（trtc_define.d.ts:1032）
   - `TRTCAudioFrame{audioFormat, data(Buffer|ArrayBuffer), length, sampleRate, channel, timestamp}`（trtc_define.d.ts:246）
3. **运行时实测日志**（见 §4）：全部 API 探测 ✅，`setAudioFrameCallback` 注册成功，进房成功 163ms。

> ⚠️ 名称纠偏：官方文档目录页曾出现 `setAudioFrameListener` 字样（PC-INTEGRATION §3.2 曾写 `onRemoteUserAudioFrame`），**实测 API 名以已装包为准 = `setAudioFrameCallback` + `onPlayAudioFrame`**；PC-INTEGRATION §3.2/附录 A.2 需按此修正（见 §5）。

---

## 3. 哑对端进房 + 远端互见（A2/A3 实测）

本地双端回环（同一房间 `jax-<device_id>`，两个 userId）：

```
端 A：userId=jax-pc-sidecar（sidecar 角色）
端 B：userId=<device_id>（手机角色，架构师裁决：手机 userId = device_id，
      与后端 /api/v1/voice/session 签发一致；原 pc-phone 定值废弃）
```

实测结果（`sidecar-smoke/logs/`；下例 device_id=loop-dev-9，故手机 userId=loop-dev-9）：

```
[BOOT] SDK_APP_ID=1600155678 ROOM=jax-loop-dev-9 USER=loop-dev-9
[BOOT] trtc-electron-sdk getSDKVersion() = 13.3.0.17949
[ROOM] enterRoom(roomId=jax-loop-dev-9, userId=loop-dev-9, scene=audio_call)
[ROOM] 进房成功（elapsed=192ms）
[PEER] 远端加入 userId=jax-pc-sidecar        ← A3 远端互见 ✅
[PEER] 远端离开 userId=jax-pc-sidecar reason=0
[ROOM] 冒烟窗口结束，exitRoom
[ROOM] 退房 reason=0
```

另一次单端进房（sidecar 角色）：

```
[BOOT] trtc-electron-sdk getSDKVersion() = 13.3.0.17949
[R1]   setAudioFrameCallback: ✅ 存在
[R1]   enableCustomAudioCapture: ✅ 存在
[R1]   sendCustomAudioData: ✅ 存在
[R1]   startLocalAudio: ✅ 存在
[R1]   setAudioFrameCallback 注册成功（等待远端进房后应有 PCM 帧日志）
[ROOM] enterRoom(roomId=jax-smoke-dev-1, userId=jax-pc-sidecar, scene=audio_call)
[ROOM] 进房成功（elapsed=163ms）
```

---

## 4. 冒烟环境复现步骤

```bash
# 1. 依赖（版本已锁 package.json；本机 npm registry 需用官方源 + fresh cache）
cd sidecar-smoke
npm install --registry=https://registry.npmjs.org --legacy-peer-deps --cache=./.npm-cache-smoke
# 若 electron 二进制未随 install 下载：cd node_modules/electron && ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ node install.js
# trtc-electron-sdk 原生 addon：cd node_modules/trtc-electron-sdk && node scripts/download.js

# 2. 哑对端单端进房（SecretKey 从项目根 .env 读取，不落代码）
unset ELECTRON_RUN_AS_NODE   # 本机环境变量陷阱：置 1 会让 electron 当 node 跑
node_modules/electron/dist/electron.exe --no-sandbox --disable-gpu . --device=smoke-dev-1 --user=jax-pc-sidecar --hold=40
# 日志：sidecar-smoke/logs/smoke-<userId>.log

# 3. 本地双端回环（验证远端互见；手机端 userId = device_id，架构师裁决）
node_modules/electron/dist/electron.exe --no-sandbox --disable-gpu . --device=loop-dev-9 --user=jax-pc-sidecar --hold=60 &
node_modules/electron/dist/electron.exe --no-sandbox --disable-gpu . --device=loop-dev-9 --user=loop-dev-9 --hold=50 &
```

> 无头环境注意：Electron 需 `--no-sandbox --disable-gpu`；`ELECTRON_RUN_AS_NODE` 若置 1 会使其按纯 Node 运行（`document is not defined` / `app.whenReady` 报错）。

---

## 5. 对实施文档的修正建议（落地 Phase B 时更新 PC-INTEGRATION.md）

| 位置 | 原文 | 修正 |
|---|---|---|
| §3.2 上行 | `onRemoteUserAudioFrame` / `setAudioFrameListener` | `setAudioFrameCallback({ onPlayAudioFrame })` |
| 附录 A.2 | `cloud.on('onRemoteUserAudioFrame'...)` 伪代码 | `cloud.setAudioFrameCallback({ onPlayAudioFrame: (frame, userId) => ... })` |
| R1 风险表 | "若 SDK 不暴露 PCM 帧则 Web Audio 兜底" | ✅ 实测暴露，Web Audio 兜底**降级为备用**（仅在需要 16k 强制采样时用 AudioContext({sampleRate:16000})） |
| 用户签名 | 附录 A.1 简化 GenUserSig（不压缩、标准 base64） | ❌ **与官方 TLSSigAPIv2 不符**，必须改为官方算法（HMAC-SHA256 四行原文 + zlib.compress + `*`/`-`/`_` base64），已按官方实现落地（backend/app/voice/usersig.py），字节级对照官方 Python/Node 实现通过 |
| §2.3 契约 | 响应 `user_id: "pc-phone"`（定值） | ❌ **架构师裁决 2026-08-06：user_id = 请求 device_id**（手机用自己的 device_id 作 TRTC userId 进房；PC sidecar 另一条路径 userId=jax-pc-sidecar），已按裁决落地并同步测试/文档 |

---

## 6. 遗留 / 待联调项（不阻塞 Phase A，列入 Phase B）

- [ ] A4 数据级：手机 RtcClient（fe-mobile）说话 → sidecar `onPlayAudioFrame` 收帧计数/能量增长（QA-PLAN §4.2 方法 A/B）
- [ ] A5 下行推流：sidecar `sendCustomAudioData` 推测试音 → 手机听到
- [ ] 采样率链路：SDK 回调默认 48k → sidecar/rtc_bridge 重采样 16k（PC-INTEGRATION §3.5 备选）
- [ ] 手机 userId 冲突：device_id 作 userId 在两台手机并发时会互踢（MVP 单用户可接受；device_id 白名单鉴权升级时一并处理）

---

## 7. 版本锁定记录（写入 ADR，禁 latest）

| 依赖 | 锁定 | 实测 |
|---|---|---|
| `trtc-electron-sdk` | `13.3.801`（package.json 写死） | 已装；`getSDKVersion()=13.3.0.17949` |
| `electron` | `31.7.7`（≥22 LTS 线，写死） | 已装；无头需 `--no-sandbox --disable-gpu` |
| Node | ≥16.20.2（官方要求） | 本机 v22.22.2 |
