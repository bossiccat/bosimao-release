# 语音控制面上云迁移方案（B 方案：会话签发 API 上 CloudBase）

日期：2026-09-07 ｜ 状态：待用户审批 ｜ 纪律：preflight → build → deploy → verify → 切流 → 回滚预案

## 0. 用户决策（2026-09-07 21:25 明确）

用户拍板：**B（云端）才是商业形态，一直要的就是云端**。
今晚 PC 侧已做的改造（证书重签/0.0.0.0 绑定/防火墙）保留为本地开发态，不回滚；
adb reverse 隧道与 localhost:8443 彻底废弃（reverse 规则已删空）。

## 1. 架构事实（代码实锤，非推测）

| 事实 | 出处 |
|------|------|
| v0.6.0 起音频统一走 TRTC，自研 WS 中继（LAN/云端 relay/配对码/E2EE）已废弃删除 | `mobile-app/.../config/VoiceConfig.kt:11-14` |
| 手机端不持有 SecretKey，**唯一存云函数环境变量** | `VoiceConfig.kt:12` |
| 手机填 `session_base_url`，拼 `/api/v1/voice/session` 拉 room_id + userSig 进房 | `VoiceConfig.kt:13-14`、`net/VoiceSessionApi.kt:90` |
| 音频路径：手机 ←TRTC 云→ PC sidecar（跨网可用，无需同网段） | ADR-012 / master-roadmap 锁定架构 |
| PC 端签发 API 现状：FastAPI `backend/app/api/routes_voice*.py`，跑在 jax-backend.exe :8000 | jax-services.ps1:303 |
| CloudBase 环境已存在且可用：`jax-relay-283963-7-1436773060.sh.run.tcloudbase.com`（/relay/health ok） | relay_server 部署 + 21:30 探活 |

**结论**：商业化的唯一缺口 = 把「会话签发 + 设备注册/配对」控制面搬到云，手机填云端域名。
音频路径不动（TRTC 本来就是云），PC sidecar 不动（桌宠本体在 PC）。

## 2. 云端组件设计（CloudRun 容器，最大化复用现有代码）

选型：**CloudBase CloudRun 容器**（不是云函数）——直接复用 FastAPI 代码，
支持 WebSocket/长连接，与已部署的 jax-relay 同环境同体系。

```
手机 App（任意网络：蜂窝/Wi-Fi）
   │  HTTPS /api/v1/voice/*（配对、注册、会话签发）
   ▼
CloudBase CloudRun「jax-voice-api」容器
   │  · 复用 backend/app：routes_voice* + guarded_route + TLS 终结（平台默认 HTTPS）
   │  · TRTC SDKAppID/SecretKey → CloudRun 环境变量（唯一存放处，符合 ADR-012）
   │  · 数据层：CloudBase MySQL（devices/sessions/pairing_codes 三张表，替代本地 SQLite/文件）
   ▼
腾讯云 TRTC（音频，已就绪）
   ▲
PC jax-rtc-sidecar（桌宠本体，只进 TRTC 房间，不再承载控制面）
```

### 范围内接口（从 routes_voice*.py 迁移）
- `POST /api/v1/voice/devices/pairing-code`（owner Bearer + X-Request-Nonce）
- `POST /api/v1/voice/devices/register`（配对码一次性消费，409 语义保留）
- `GET  /api/v1/voice/session`（room_id + userSig 签发；nonce 契约保留）
- 健康检查 `/health`
- guard 语义：云上无需 mTLS client 绑定那套（那是 PC 内部桥接用的），改用设备凭证 + nonce；
  **FastAPI 守卫顺序铁律沿用**（自定义 APIRoute，避免 422 泄露 schema）。

### 明确不上云（范围外）
- rtc_bridge / MiniCPM-o 链路（在 PC，属桌宠本体）
- jax-model 本地大模型（PC GPU）
- relay_client（v0.6.0 已废；云端 relay 实例保留但不再是语音路径）

## 3. 迁移管线（六步，每步有验收）

| 步骤 | 内容 | 验收 |
|------|------|------|
| P1 preflight | 盘点 routes_voice* 依赖（DB 层、配置、nonce 库）；云环境确认（用 jax-relay 同一 env） | 依赖清单 + 云环境就绪截图 |
| P2 build | Dockerfile（python3.11-slim + backend/app 语音子集）；MySQL schema 迁移脚本 | 容器本地起 + /health ok |
| P3 deploy | 部署 CloudRun「jax-voice-api」；注入 TRTC_SDKAPPID/TRTC_SECRET/OWNER_TOKEN；绑定默认域名 | 公网 /health 200；旧数据不迁移，全新起始 |
| P4 verify（真机） | 手机 session_base_url → 云端域名；配对→注册→签发→进房全链路 | TRTC 进房 + 桥日志 rooms=1 |
| P5 切流验收 | 「停止监听→再点立即对话」×10 循环自动化；**手机切蜂窝（关 Wi-Fi）重复全链路** | 10/10 PASS + 蜂网 PASS |
| P6 回滚预案 | 手机 URL 可随时切回 Tailscale 直连（已就绪）；云端仅新增无删除 | 回滚 = 改一个 URL |

## 4. 风险与决策点

| # | 风险/决策 | 说明 | 需要用户确认 |
|---|-----------|------|--------------|
| R1 | CloudBase 环境复用 | jax-relay 所在 env（283963-7-1436773060）是否同意再部署一个 CloudRun 服务 | ✅/❌ |
| R2 | TRTC SecretKey 入云 | 从 PC .env 迁到 CloudRun 环境变量；PC 侧保留（sidecar 进房也要 userSig？——sidecar 走桥/后端签发，需在 P1 盘点清楚） | 知悉 |
| R3 | 设备/配对数据 | 本地已注册设备（f32e502b…，10-04 到期）不迁移，手机在云端重新配对一次 | 知悉 |
| R4 | 费用 | CloudRun 按量 + MySQL；语音量小阶段成本可忽略 | 知悉 |
| R5 | 域名 | 一期用 CloudRun 默认域名（*.tcloudbase.com）；自有域名二期 | 知悉 |

## 5. 验收总口径（对应 G 系门禁）

- 手机在**蜂窝网络**（PC 不在同一网络）完成 配对→签发→TRTC 通话 全链路
- 「停止监听→立即对话」×10 循环 0 失败
- PC 后端 :8000 宕机不影响手机配对/签发（控制面独立性证明）
- 现有 NO-GO 纪律不变：云端验收 PASS 前，本地 Tailscale 形态仍是可回退安全态
