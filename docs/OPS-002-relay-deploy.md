# OPS-002: 云端中继部署与运维（M2，CloudBase CloudRun 容器）

> 状态：**已部署（2026-08-04 卜宕机实测）** | 服务 `jax-relay` | 环境 `jinhong-d2g55ycl591208475`（ap-shanghai）
> 对应契约：docs/specs/mobile-voice-spec.md §6/§7、docs/openapi.yaml `x-deploy: cloudbase`

## 1. 公网地址（已上线）

| 项 | 值 |
|---|---|
| 服务名 | jax-relay（容器型 CloudRun） |
| 公网 HTTPS | `https://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com` |
| 中继 WS（手机/PC 连接） | `wss://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com/relay/ws` |
| 健康检查 | `GET https://…/relay/health` → `{"status":"ok","port":19090}` |
| 统计 | `GET https://…/relay/stats` |
| 签发配对码 | `POST https://…/relay/pair` |
| 控制台 | 云托管 → 服务 jax-relay（环境 jinhong） |

## 2. 部署方式（CloudBase CloudRun 容器型）

选型理由：中继是 **WebSocket 长连接 + 全双工透传**，需要常驻容器（非无状态 FaaS）；spec §6.2 已否决云函数方案（长连接成本/复杂度双劣）。

- **类型**：container（Dockerfile 构建，镜像 273MB，python:3.11-slim）
- **代码**：`deploy/relay/`（自包含 relay 包 + Dockerfile，由 `backend/relay/` 同步构建快照）
- **规格**：CPU 0.25 核 / 内存 0.5GB / MinNum=1 / MaxNum=1（单用户，保活防冷启动）
- **端口**：`Port=19090`（容器监听 19090，与 `RELAY_PORT=19090` 一致）
- **访问**：OpenAccessTypes=`PUBLIC`（公网 HTTPS/WSS）；InternalAccess=close

### 2.1 控制台操作路径（人工/回滚用）

腾讯云控制台 → 云开发 CloudBase → 环境 `jinhong` → **云托管** → 新建服务 `jax-relay`（容器型）：
1. 上传 `deploy/relay/`（含 Dockerfile）
2. 端口填 `19090`；CPU 0.25/内存 0.5；最小实例 1、最大实例 1
3. 公网访问：开启 PUBLIC
4. 环境变量（见 §3）
5. 部署 → 等待镜像构建 + Pod 就绪（约 1-2 分钟）

## 3. 环境变量（生产密钥，不落 repo）

| 变量 | 值 | 说明 |
|---|---|---|
| `RELAY_TOKEN` | 32 字节 base64 强随机 | 中继鉴权；未配置=开发态放行（**生产必须配置**） |
| `RELAY_E2EE_KEY` | 32 字节 base64 强随机 | AES-256-GCM 预共享密钥，手机/PC 两端必须相同 |
| `RELAY_PORT` | `19090` | 容器监听端口（与云托管 Port 配置一致） |

生成方式（生产密钥由运维生成，仅写入云托管环境变量与本地 `.env`，**不提交 git、不进文档明文**）：

```bash
python -c "import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())"   # 分别生成两次
```

- 云托管侧：控制台「服务设置 → 环境变量」粘贴，或部署时 EnvParams 传入
- PC/手机侧：写入项目 `.env`（`RELAY_TOKEN=` / `RELAY_E2EE_KEY=`），`scripts/start-relay.ps1` 自动加载
- **轮换**：改密钥 → 云托管更新环境变量 + 两端 `.env` 同步 → 重部署；旧会话自动断开

## 4. 健康检查与验证

```bash
# 健康检查
curl -sS https://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com/relay/health
# 期望: {"status":"ok","port":19090}

# 服务器统计（paired/forwarded/kicked/replays）
curl -sS https://jax-relay-283963-7-1436773060.sh.run.tcloudbase.com/relay/stats
```

### 4.1 跨网络联调（2026-08-04 实测通过）

本地 `scripts/mock_phone_client.py --relay-url wss://公网/relay/ws`（新增参数）跑通：
手机模拟端 → 公网中继（WSS+TLS）→ PC relay_client → mock voice 网关 → 回传。

```
[round 1] 配对 41ms | 端到端 125ms | 下行 6400B
[round 2] 配对 35ms | 端到端 121ms | 下行 6400B
[round 3] 配对 35ms | 端到端 147ms | 下行 6400B
[OK] 3 轮全链路联调通过（目标 <5s）
```

服务端统计佐证：`paired=8, forwarded=228, replays=0`（E2EE 防重放 0 违例）。

## 5. 本地启动（开发/联调）

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-relay.ps1        # 幂等启动
powershell -ExecutionPolicy Bypass -File scripts/start-relay.ps1 -Restart # 强制重启
# 健康: http://127.0.0.1:19090/relay/health
```

## 6. 安全注意事项

1. **密钥不入库**：RELAY_TOKEN/RELAY_E2EE_KEY 仅存云托管环境变量 + 本地 `.env`（gitignore 已覆盖）；`deploy/relay/` 不含任何密钥
2. **WSS 强制**：公网入口为 HTTPS/WSS（CloudBase 托管 TLS）；中继本身只透传，不解析不落盘
3. **E2EE 默认开启**：`e2ee_enabled: true`；中继只见密文；AAD 含 seq 防重放（`replays=0`）
4. **token 鉴权**：`token_required: true`；未带 token 连接在 pair 阶段被拒（1008）
5. **最小暴露**：单实例、单用户配对（`max_sessions_per_code=1`）；无数据库、无持久卷（无状态）
6. **访问控制**：控制台可随时切 PUBLIC → VPC/关闭公网

## 7. 更新与回滚

- **更新**：改 `deploy/relay/relay/` → 控制台重新部署（或 MCP `manageCloudRun deploy`）→ 新版本 FlowRatio 100%
- **回滚**：控制台「版本」页选择上一版本（如 jax-relay-001）→ 流量切回；无状态服务回滚即时生效
- **删服务**：控制台云托管删除（数据无，纯透传无状态）

## 8. 成本备注

体验版（baas_trial）环境，0.25C/0.5G 单实例按量计费；MinNum=1 常驻约等于小流量成本（远低于 spec §6.2 的 ¥40-80/月自建服务器）。超出体验版配额时控制台会提示升配。
