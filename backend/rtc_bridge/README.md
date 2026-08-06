# backend/rtc_bridge — TRTC sidecar ↔ apm_bridge 本地桥（独立进程）

PC 侧 RTC 桥（PC-INTEGRATION §4.2 独立进程决策）：接收 sidecar 经 localhost WS 推来的
手机音频（16k s16）→ 停顿补静音 → `ApmBridge.feed_pcm`（MiniCPM-o，原样复用）；下行
`ApmBridge.on_audio_out`（16k s16）→ 整形器（拆 20ms 帧 + 节拍）→ WS 下发 sidecar。

```
sidecar(Electron) ──WS :19092──▶ rtc_bridge.py ──▶ ApmBridge ──▶ MiniCPM-o Realtime API
                                    ▲  │ on_audio_out(16k s16)
                                    └──┴── DownlinkShaper(20ms 帧 + 节拍)
```

## 目录

```
backend/rtc_bridge/
├── main.py      # 入口：装配 → WS 服务端 + 健康检查 → 常驻（只装配，零业务）
├── config.py    # 端口/APM 配置（环境变量，禁硬编码凭据）
├── server.py    # localhost WS 服务端 :19092（hello 注册 / up_audio / peer_state → session）
├── session.py   # PeerVoiceSession：1 房间 = 1 WS + 1 ApmBridge + EndDetectFeeder + DownlinkShaper
├── shaper.py    # 下行整形器（变长块 → 20ms 帧 + 消费时长节拍）
└── health.py    # 健康检查 HTTP :19093（/health + /metrics，asyncio 原生零依赖）
```

共享依赖：`app/voice/apm_bridge.py`（原样复用）、`app/voice/end_detect.py`（停顿补静音，从
session.py 抽取，旧 WS 网关与 rtc_bridge 共用）。

## WS 契约（sidecar ↔ rtc_bridge，127.0.0.1:19092）

| 方向 | 消息 |
|---|---|
| sidecar→bridge | `{type:"hello", role, sdk_version, device_id, room_id, user_id}`（首帧必发） |
| sidecar→bridge | `{type:"up_audio", pcm_b64}`（手机远端音频 16k s16 mono） |
| sidecar→bridge | `{type:"peer_state", state:"enter"\|"leave", user_id}` |
| bridge→sidecar | `{type:"ready"}` |
| bridge→sidecar | `{type:"down_audio", pcm_b64}`（回复音频 16k s16 mono，20ms 帧节拍） |
| bridge→sidecar | `{type:"ctrl", action:"exit", reason}` |

## 运行

```bash
cd backend
python -m rtc_bridge.main
# 健康检查
curl http://127.0.0.1:19093/health
curl http://127.0.0.1:19093/metrics
```

环境变量（可选）：`RTC_BRIDGE_WS_PORT=19092` / `RTC_BRIDGE_HEALTH_PORT=19093` /
`APM_API_URL` / `APM_SYSTEM_PROMPT` / `APM_TOKEN` / `RTC_BRIDGE_DOWN_FRAME_MS=20`。

看门狗：`scripts/jax-services.ps1 start rtc-bridge`（/health 判定，待命态也算健康，避免误杀）。

## 关键语义

- **懒初始化**：ApmBridge 首个音频块才建 MiniCPM-o 会话（空闲连接会被服务端回收，对齐
  2026-08-06 实锤）；hello 不建会话。
- **停顿补静音**：`EndDetectFeeder`（低能量 >1.2s → 补 2s 静音触发说完判定；能量回升重置）。
- **下行整形**：`DownlinkShaper` 按 20ms 帧 + 消费时长（len/32000 s）节拍推送，避免灌包卡顿。
- **远端离开**：释放 APM 会话（`close`），回待命；远端重进 → feeder/shaper 重置防串话。

## 单测

```bash
cd backend
python -m pytest tests/unit/test_end_detect.py tests/unit/test_rtc_bridge_server.py -q
# 10 passed（mock sidecar WS 客户端 + 假 ApmBridge，不触网）
```
