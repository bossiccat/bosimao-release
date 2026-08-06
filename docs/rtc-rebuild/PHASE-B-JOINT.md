# Phase B 本地联调结果 — TRTC sidecar + rtc_bridge + MiniCPM-o 全链路闭环

> 版本：v1.0（2026-08-06）
> 作者：be-pc（后端工程师）
> 状态：**✅ 音频闭环全通**（桥回环 + 全链路 TRTC 双端，多轮复现）
> 依据：PC-INTEGRATION §2.3/§3/§4、ARCHITECTURE §3.4/§5.2、ADR-012；腾讯云 TRTC 官方文档核对
> 环境：Windows 10 x64，Node v22.22.2，Electron 31.7.7，trtc-electron-sdk 13.3.801，Python 3.11.9

---

## 0. TL;DR（30 秒结论）

| 项 | 结论 |
|---|---|
| 音频闭环 | ✅ **通**：手机音频 → sidecar → rtc_bridge → apm_bridge → MiniCPM-o → 回复 → 手机 |
| 首包回复延迟 | **~4.5s**（自手机上行开始；含 2.84s 提问 wav + 2s 说完判定静音 + 模型排队/推理） |
| sidecar 进房 | 133ms；手机进房 172ms；双方互见 |
| sidecar 收帧格式 | SDK 回调 **48k 立体声** → sidecar 内 16k mono 抽取（3:1 + 声道平均）✅ |
| 下行注入 | sendCustomAudioData 16k 直推（官方 d.ts 确认支持 16k）✅ |
| bridge 指标 | up_frames=256 / apm_state=listening（联调期采样） |
| 回复音频 | 真实语音（RMS≈2.1k~3.0k，peak≈18k~20k，33%~55% 非静音） |

---

## 1. 联调目标与链路

```
手机模拟器(TRTC 16k 自定义采集)
   │ POST 127.0.0.1:8000/api/v1/voice/session → room_id/user_sig（或 .env 兜底）
   ▼ enterRoom(jax-<device_id>, userId=device_id)
TRTC 云 ◀──▶ sidecar(trtc-electron-sdk, userId=jax-pc-sidecar)
                   │ setAudioFrameCallback(onPlayAudioFrame 48k) → 16k mono → localhost WS :19092
                   ▼
            rtc_bridge.py（独立进程）
                   │ EndDetectFeeder（停顿补静音）→ ApmBridge.feed_pcm
                   ▼
            MiniCPM-o Realtime API（wss://minicpmo45.modelbest.cn）
                   │ on_audio_out(16k s16) → DownlinkShaper(20ms 帧+节拍)
                   ▼
            localhost WS :19092 → sidecar sendCustomAudioData(16k) → TRTC 云 → 手机 onPlayAudioFrame
```

## 2. 工具与脚本

| 文件 | 用途 |
|---|---|
| `sidecar/rtc.js` | sidecar 角色：拉 userSig → 进房 → 音频双向桥接 |
| `sidecar/phone.js` | 手机模拟器：进同房 → 推 wav（tmp/poc_b3_ask_16k.wav）→ 收回复写 wav |
| `backend/rtc_bridge/main.py` | rtc_bridge 独立进程（WS :19092 + 健康 :19093） |
| `tmp/phase_b_phone.py` | 联调编排器：`--bridge-loopback`（桥回环）或默认（全链路 TRTC） |

## 3. 执行结果

### 3.1 桥回环（不经 TRTC，验证 bridge→apm→MiniCPM-o 链路，2026-08-06）

```
python tmp/phase_b_phone.py --bridge-loopback --device=bl-1 --hold=70
[loopback] ready={'type': 'ready'}
[loopback] 首包回复 @5478ms
[loopback] 收到回复 250 帧 / 160000B
[PASS] 桥回环音频闭环通：已保存 tmp/phase_b_loopback_reply.wav
# 回复 wav 能量：RMS=2959.6 peak=17867 非静音占比 54.7%（真实语音）
```

### 3.2 全链路 TRTC 双端（run1 / run3 / run-final2，3 次干净复现）

```
python tmp/phase_b_phone.py --device=joint-final2 --hold=100
[PASS] 音频闭环通：手机→sidecar→rtc_bridge→apm_bridge→MiniCPM-o→回复→手机
       回复 wav: tmp/phase_b_phone_reply.wav（89644B）
       bridge 指标: up_frames=256 down_frames=2 apm_state=listening
```

sidecar 日志（logs/sidecar-sidecar.log）：
```
[ROOM] 进房成功（elapsed=133ms）
[PEER] 远端加入 userId=joint-final2
[AUDIO] 远端音频可用 userId=joint-final2 available=1
[PCM] 首帧: userId=joint-final2 sampleRate=48000 channel=2 length=3840   ← 48k 立体声，抽取路径生效
[STAT] up=136帧/85KB down=0帧/0KB ws=true
[PEER] 远端离开 userId=joint-final2 → 退房回待命
```

手机日志（logs/sidecar-phone.log）：
```
[PHONE] 进房成功 172ms / 远端加入 jax-pc-sidecar
[PHONE] wav 推完（142帧），补 2s 静音
[PHONE] 首包回复 @4502ms（自上行开始）
[PHONE] 回复已保存: tmp/phase_b_phone_reply.wav（89600B）
```

回复 wav 能量（run-final2）：RMS=2065.1 peak=19754 非静音占比 33.5%（真实语音 ✅）

### 3.3 延迟拆解（本地联调口径）

| 段 | 数值 |
|---|---|
| sidecar 进房（enterRoom→onEnterRoom） | 133ms |
| 手机进房 | 172ms |
| 双方互见（远端加入） | 进房后 ~2s（受手机推 wav 启动时序影响） |
| 提问 wav 上行（142 帧 × 20ms 节拍） | ~2.84s |
| 说完判定补静音 | 2s（EndDetectFeeder，低能量 >1.2s 触发） |
| **首包回复（自手机上行开始）** | **~4.5s**（含上行 2.84s + 静音 2s 交叠 + 模型排队/推理） |

> 说明：首包延迟是"从手机开始说话到模型首个音频包回传"的端到端值；其中大部分为提问音频
> 本身 + 说完判定静音（模型需听到完整问题 + 停顿才回复）。真实手机场景（KWS 唤醒后进房
> 说话）与本地口径一致；QA 首音 P50≤2.0s 的门禁以手机端打点为基准，建议 fe-mobile 在真机
> 联调时同步打点（手机说话时刻 → 首包播放时刻），排除模拟器节拍与本地进房时序差异。

### 3.4 全链路组件确认

| 组件 | 验证点 | 结果 |
|---|---|---|
| sidecar 拉 userSig | 优先 POST 127.0.0.1:8000 /api/v1/voice/session/sign；不可用回退 .env 本地签发（本地后端旧进程未含 /sign 路由） | ✅（.env 兜底路径；重启后端后走 /sign） |
| sidecar 进房 | onEnterRoom elapsed=133ms；SDK 13.3.0.17949 | ✅ |
| sidecar 收 PCM | onPlayAudioFrame 48k/2ch → 16k mono 抽取 | ✅ |
| sidecar 上行 | up=136帧/85KB 推给 rtc_bridge | ✅ |
| rtc_bridge | WS :19092 hello→ready；up_audio → EndDetectFeeder → ApmBridge | ✅ |
| 停顿补静音 | 低能量>1.2s → 补 2s 静音（说完判定） | ✅（模型如期回复） |
| MiniCPM-o | 懒初始化建会话；on_audio_out 下行 | ✅ |
| 下行整形 | DownlinkShaper 20ms 帧 + 节拍 | ✅（手机侧 143 帧无爆音） |
| sidecar 下行注入 | sendCustomAudioData 16k 直推（官方 d.ts 支持 16k） | ✅ |
| 手机收回复 | onPlayAudioFrame(userId=jax-pc-sidecar) → 写 wav | ✅ |

## 4. 健康检查与看门狗

```
curl 127.0.0.1:19093/health   → {"status":"ok","sidecar_connected":true,"rooms":1}
curl 127.0.0.1:19093/metrics  → {...,"up_frames":256,"apm_session_state":"listening",...}
scripts/jax-services.ps1 start rtc-bridge   # /health 判定；待命态也算健康，避免误杀
```

## 5. 已知限制与后续（Phase C / 运维）

1. **单 sidecar 顶替语义**：MVP 单用户，新连接顶替旧连接。联调时若有**残留 sidecar 进程**
   （BridgeClient 断线重连）会反复顶替，导致会话被劫持（实测 run-final 失败即因此）。
   **操作规范**：联调前 `taskkill /IM electron.exe /F` 清理残留；生产由 rtc_bridge/看门狗
   单实例拉起，不存在多实例残留。
2. **首包延迟含说完判定静音**：模型"听完+停顿"才回复是设计语义（QA 首音口径需对齐，
   见 §3.3 说明）。
3. **本地后端旧进程**：运行中的 backend :8000 未含新 `/api/v1/voice/session/sign` 路由，
   联调走 .env 兜底；重启 backend 后 sidecar 走 /sign 端点（生产统一云函数 trtc-sign）。
4. **云函数未实际部署**：trtc-sign 部署代码 + 14 单测已交付（deploy/trtc-sign/），需
   CloudBase 环境操作权限后按 README 部署（SecretKey 唯一存云函数环境变量）。
5. **跨网真机**：本地闭环（模拟器）已通；深圳手机 4G ↔ 衡阳 PC 跨网真机联调属 Phase C
   （QA-PLAN 六道门），依赖 fe-mobile RtcClient 就绪。

## 6. 复现步骤

```bash
# 0) 依赖
cd sidecar && npm install --registry=https://registry.npmjs.org --legacy-peer-deps
# 或复用 sidecar-smoke node_modules：cmd /c mklink /J sidecar\node_modules sidecar-smoke\node_modules

# 1) 起 rtc_bridge
cd backend && python -m rtc_bridge.main

# 2) 桥回环（可选，快）
python tmp/phase_b_phone.py --bridge-loopback --device=bl-9 --hold=70

# 3) 全链路 TRTC 双端（sidecar + 手机模拟器；先确保无残留 electron）
taskkill /IM electron.exe /F
python tmp/phase_b_phone.py --device=joint-x --hold=100
# 结果：tmp/phase_b_phone_reply.wav（真实回复语音）；日志 sidecar/logs/sidecar-*.log
```
