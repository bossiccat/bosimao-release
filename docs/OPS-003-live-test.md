# OPS-003: 真机链路装配与联调（手机 App → 公网中继 → 电脑贾克斯）

> 状态：**已装配（2026-08-04 卜宕机实测）** | 链路：手机 App → wss://公网中继/relay/ws → PC relay_client → 本地 voice 网关(ws://127.0.0.1:8000/ws/voice)
> 前置：OPS-002 已部署公网中继（jax-relay）；手机 App 已安装（中继模式）
> 对应契约：docs/specs/mobile-voice-spec.md §6/§7、docs/OPS-002-relay-deploy.md

## 1. 三端约定（本次对齐）

| 项 | 值 | 说明 |
|---|---|---|
| 公网中继 WS | `wss://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com/relay/ws` | OPS-002 已上线 |
| 配对码（开发） | **JAX2026** | PC relay_client 与手机 App 都用它；正式使用前轮换 |
| E2EE 开发密钥 | `jax-voice-dev-e2ee-20260803-0001` | 手机 App 默认开发密钥（VoiceConfig.DEFAULT_E2EE_KEY） |
| RELAY_E2EE_KEY（PC/云端环境变量） | `Q4Q/xnJEixH81+11EAyXwXTqn1+vgPMsxaWf9FQzutw=` | = base64(SHA-256(开发密钥))，与手机 SHA-256 派生出的 32 字节 AES 密钥一致 |
| RELAY_TOKEN | relay-secrets.local 中的值（不落 repo） | 手机/PC 连接中继均需带 |

### 1.1 密钥对齐说明（为什么 RELAY_E2EE_KEY 是 base64 而不是明文串）

- 手机端 `VoiceCipher.deriveKey(keyString)` = **SHA-256(字符串)** → 32 字节 AES 密钥；
- PC/中继端 `relay_protocol.load_e2ee_key` = **base64 解码** → 要求 32 字节 base64；
- 因此两端的**同一个 32 字节密钥**的表示不同：手机用字符串，PC 用其 SHA-256 的 base64。
  `RELAY_E2EE_KEY = base64(SHA-256("jax-voice-dev-e2ee-20260803-0001")) = Q4Q/xnJEixH81+11EAyXwXTqn1+vgPMsxaWf9FQzutw=`
- ⚠️ 不要把明文串直接填进 `RELAY_E2EE_KEY`：`load_e2ee_key` 会因非法 base64 抛错，导致中继/客户端启动失败。
- 轮换密钥：改字符串 → 手机 App 填新串 → `RELAY_E2EE_KEY = base64(SHA-256(新串))` 同步到云端 + 本地 .env → 重部署。

## 2. 手机 App 配置（用户侧）

1. 打开 App → 设置（VoiceConfig）：
   - 连接模式：**中继（relay）**
   - 中继地址：`wss://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com/relay/ws`
   - 配对码：`JAX2026`
   - E2EE 密钥：`jax-voice-dev-e2ee-20260803-0001`（默认值即此，无需改）
2. 连接 URL 需带 token（中继强制鉴权）：`wss://.../relay/ws?token=<RELAY_TOKEN>`
   - ⚠️ 当前 App `VoiceWsClient` 握手发的是 `hello` 帧，而中继首帧要求 `pair` 帧 → **真机直连中继需 App 侧实现 pair 握手（含 token）**，见 §5 已知问题。

## 3. 电脑端启动步骤（一键）

```powershell
# 一键启动：模型(:19080) → 后端(:8000) → relay_client（公网中继桥接）
powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1
# 强制重启（杀旧进程后重启）
powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1 -Restart
```

- 幂等：模型/后端已在监听则跳过；relay_client 已运行则跳过。
- 单服务启动：`scripts/start-model.ps1`（模型）、`scripts/start-relay.ps1`（本地中继，联调用）。
- relay_client 实际命令（start-all.ps1 内自动组装）：
  ```
  .venv\Scripts\python.exe -m backend.relay.relay_client \
      --relay wss://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com/relay/ws \
      --gateway ws://127.0.0.1:8000/ws/voice \
      --pairing-code JAX2026 --token <RELAY_TOKEN> --e2ee-key <RELAY_E2EE_KEY>
  ```
- 日志：`logs/relay_client.log(.err)` / `logs/backend.log(.err)` / `logs/llama-server-19080.log(.err)`

## 4. 验收步骤（全链路）

```bash
# 0) 预检：中继健康
curl -sS https://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com/relay/health   # {"status":"ok","port":19090}
# 1) 电脑端启动
powershell -ExecutionPolicy Bypass -File scripts/start-all.ps1
# 2) 模拟手机走公网中继（E2EE on, token on, 3 轮）
cd <项目根>
RELAY_E2EE_KEY='Q4Q/xnJEixH81+11EAyXwXTqn1+vgPMsxaWf9FQzutw=' \
RELAY_TOKEN='<RELAY_TOKEN>' \
.venv/Scripts/python.exe scripts/mock_phone_client.py \
    --relay-url 'wss://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com/relay/ws' \
    --pairing-code JAX2026 --rounds 3 --gateway mock
# 期望：
# [round 1] 配对 ~40-240ms | 端到端 ~120-140ms | 下行 6400B
# [OK] 3 轮全链路联调通过（目标 <5s）
# 3) 中继侧统计佐证（paired/forwarded 增长、replays=0）
curl -sS https://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com/relay/stats
```

### 2026-08-04 实测结果

```
[relay] 使用外部中继 wss://jax-relay.../relay/ws token=on e2ee=on
[round 1] 配对 238ms | 端到端 119ms | 下行 6400B
[round 2] 配对 39ms  | 端到端 139ms | 下行 6400B
[round 3] 配对 32ms  | 端到端 137ms | 下行 6400B
[OK] 3 轮全链路联调通过（目标 <5s）
```
中继统计：`paired=10, forwarded=325, replays=0`（E2EE 防重放 0 违例）。

## 5. 已知问题与坑（重要）

1. **代理坑（必踩）**：本机 `HTTP_PROXY/HTTPS_PROXY=127.0.0.1:7890`，`websockets` 默认信任代理导致连不上公网中继（报错为空）。
   已修：`backend/relay/relay_client.py` 与 `scripts/mock_phone_client.py` 的 `websockets.connect(..., proxy=None)`（对应 spec §11-9）。
   **重装依赖/换环境后需确认该参数仍在**。
2. **手机 App 握手协议缺口**：当前 App `VoiceWsClient` 连接后发 `hello` 帧，且未带 token；中继首帧要求 `pair`（含 token）。
   真机直连中继需要 App 侧实现 pair 握手（`backend/relay/relay_protocol.make_pair_frame` 同构）+ URL 带 token。→ 归属 fe-app4。
3. **手机 App E2EE 未接线**：`VoiceWsClient.sendAudio` 目前发明文 PCM（VoiceCipher 存在但未在发送路径调用）。
   若手机端保持明文，PC relay_client 需以 `--e2ee-key` 为空启动（明文模式）才能互通；若手机端启用 E2EE，PC 必须用 §1.1 对齐密钥。
4. **relay_client 空闲重连抖动**：`_connect_relay` 期望 10s 内收到 `paired` ack，但中继只在两端都接入时才回 `paired`；
   手机未接入时 relay_client 会周期性重连（kicked 计数增长）。手机接入后自动配对，功能不受影响。
5. **真实网关半双工**：`--gateway real`（走 backend /ws/voice + sherpa STT + edge-tts）需要**真实人声**音频；
   mock_phone 的合成 PCM 会让 STT 返回空文本 → 网关回 error 而非 reply_done（测试挂起属预期）。STT 模型已就绪（`/api/v1/voice/status → stt_model:"ok"`）。
6. **start-model.ps1 编码**：原文件无 BOM，Windows PowerShell 5.1 按 GBK 解析中文导致语法错误；已补 UTF-8 BOM。新建 .ps1 建议纯 ASCII 或带 BOM。

## 6. 回滚

- 密钥回滚：云端控制台 → 云托管 jax-relay → 环境变量改回旧 RELAY_E2EE_KEY → 重部署（OPS-002 §7）；本地 .env / relay-secrets.local 同步。
- 服务回滚：OPS-002 §7（版本切回）。
