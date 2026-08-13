# jax-rtc-sidecar — TRTC 无头对端（Electron + trtc-electron-sdk 13.4.802-beta.3）

PC 端 RTC 对端（PC-INTEGRATION §2.2 / ARCHITECTURE §5.2）：进房收手机音频 → localhost WS 推
`rtc_bridge`（Python）→ `apm_bridge`（MiniCPM-o）回复音频 → `sendCustomAudioData` 回传手机。
**无 UI**（隐藏窗口）；**不开本地麦克风/扬声器**（无回声，上行只走 rtc_bridge 注入）。

## 目录结构

```
sidecar/
├── package.json     # trtc-electron-sdk 精确版本 13.4.802-beta.3 + electron 31.7.7（禁 latest）
├── main.js          # 隐藏窗口 + 生命周期 + 参数透传
├── index.html       # 渲染进程入口（加载 rtc.js）
├── rtc.js           # 主逻辑：拉 userSig → 进房 → 音频双向桥接（sidecar 角色）
├── phone.js         # 联调用手机模拟器（TRTC 对端，推 wav / 收回复写 wav）
├── bridge.js        # localhost WS 客户端（连 rtc_bridge :19092）
├── audio.js         # PCM 工具：48k→16k 3:1 抽取 / 下行帧构造 / wav 读写
├── config.js        # 命令行参数 + 受保护宿主运行时注入的控制面凭证
├── security.js      # Bearer 控制面认证 + 单请求随机 nonce
└── logs/            # sidecar-<role>.log
```

## WS 契约（sidecar ↔ rtc_bridge，127.0.0.1:19092）

| 方向 | 消息 |
|---|---|
| sidecar→bridge | `{type:"hello", role, sdk_version, device_id, room_id, user_id}` |
| sidecar→bridge | `{type:"up_audio", pcm_b64}`（手机远端音频 16k s16 mono） |
| sidecar→bridge | `{type:"peer_state", state:"enter"\|"leave", user_id}` |
| bridge→sidecar | `{type:"ready"}` |
| bridge→sidecar | `{type:"down_audio", pcm_b64}`（回复音频 16k s16 mono） |
| bridge→sidecar | `{type:"ctrl", action:"exit", reason}` |

## 运行

依赖（复用 sidecar-smoke 已装 node_modules，或在本目录重装）：

```bash
# 方式 A：共享 sidecar-smoke 的 node_modules（Windows junction，避免重复下载 electron）
cmd /c mklink /J sidecar\node_modules sidecar-smoke\node_modules
# 方式 B：本目录独立安装
cd sidecar && npm install --registry=https://registry.npmjs.org --legacy-peer-deps

# sidecar 角色（先起 rtc_bridge，见 backend/rtc_bridge/）
node_modules/electron/dist/electron.exe --no-sandbox --disable-gpu . \
  --role=sidecar --device=dev-001 --sign-url=http://127.0.0.1:8000 --bridge-url=ws://127.0.0.1:19092 --hold=300
# 手机模拟器（联调）
node_modules/electron/dist/electron.exe --no-sandbox --disable-gpu . \
  --role=phone --device=dev-001 --sign-url=http://127.0.0.1:8000 \
  --wav=..\tmp\poc_b3_ask_16k.wav --out-wav=..\tmp\phase_b_phone_reply.wav --hold=90
```

- 无头环境：`--no-sandbox --disable-gpu`；`ELECTRON_RUN_AS_NODE` 若置 1 会按纯 Node 运行（报
  `document is not defined`），需 `unset ELECTRON_RUN_AS_NODE`。
- `--sign-url`：生产为受保护的签发服务；本地联调为 backend `http://127.0.0.1:8000`
  （`POST /api/v1/voice/session` 手机 / `POST /api/v1/voice/session/sign` PC）。
- 生产 sidecar 仅接受受保护宿主运行时注入的 `VOICE_SIDECAR_CREDENTIAL`，并以 Bearer
  凭证访问控制面；凭证缺失或签发失败时 fail-closed，不从 `.env` 读取控制面凭证，也不持有或
  派生 TRTC SecretKey。
- Tauri 的 OS-bound credential provider 与受保护注入链尚未完成，属于商业发布 P0 阻断项；
  完成前不得把凭证放入命令行、普通文件或源码。

## 音频格式

- 全链路 **16k s16 mono**（与 apm_bridge 上行/下行一致，happy path 零重采样）。
- SDK 远端回调默认 48k → sidecar 内 `frameToS16Mono16k` 做多声道平均 + 3:1 线性抽取。
- 下行 `sendCustomAudioData` 直接推 16k（官方 d.ts 明确支持 16000/24000/32000/44100/48000）。
- 日志：进房成功 elapsed / 远端加入离开 / 首帧 sampleRate+channel / PCM 帧统计（每 5s）。

## 版本锁定（禁 latest）

| 依赖 | 锁定 |
|---|---|
| `trtc-electron-sdk` | `13.4.802-beta.3` |
| `electron` | `31.7.7`（≥22 LTS 线） |
