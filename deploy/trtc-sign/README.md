# trtc-sign 云函数（部署用代码）

TRTC UserSig 代签云函数（ADR-012 决策 #7 / PC-INTEGRATION §2.3 / ARCHITECTURE §3.4）。
手机（深圳公网）直调本云函数拿 `room_id + user_sig` 进 TRTC 房间；PC（NAT 后无公网入站）
由主动外呼轮询 `pending` + `sign` 取自身 userSig 进同一房间。

**SecretKey 唯一存云函数环境变量 `TRTC_SECRETKEY`**，不进代码、不进 repo、不进日志。

## 目录结构

```
deploy/trtc-sign/
├── index.py          # SCF 入口：API Gateway 事件解析 + 路由 + 统一响应 {code,data,message}
├── signing.py        # 签名服务：issue / pending / sign_for_sidecar（幂等房间 + 会话意图）
├── usersig.py        # TLSSigAPIv2 官方算法（与 backend/app/voice/usersig.py 字节级一致）
├── config.py         # 环境变量配置（TRTC_SDKAPPID / TRTC_SECRETKEY / TRTC_ROOM_PREFIX ...）
├── serverless.yaml   # SCF/CloudBase 部署配置（触发器 + 环境变量说明）
└── tests/
    └── test_trtc_sign.py   # 单测（假 key 验签 / 契约字段 / 幂等 / 有效期 / 错误码）
```

## 端点契约

| 方法 | 路径 | 请求 | 响应 data |
|---|---|---|---|
| POST | `/api/v1/voice/session` | `{"device_id":"jax-xxxx"}` | `{room_id, user_id, user_sig, sdk_app_id, scene:"audio_call"}` |
| GET | `/api/v1/voice/session/pending?device_id=` | — | `{device_id, room_id, ts}` 或 `data:null`（无/过期） |
| POST | `/api/v1/voice/session/sign` | `{"device_id":"...", "user_id":"jax-pc-sidecar"}` | 同 session（PC 自身 userSig，同一 room_id） |

统一响应 `{"code":0,"data":{...},"message":""}`；错误 `{code,data:null,message}`：
`40001` device_id 非法 / `40002` user_id 非法 / `40400` 无会话意图（或不在白名单）/
`50300` 凭据未配置 / `50000` 内部错误。

- **幂等**：`room_id = TRTC_ROOM_PREFIX + device_id`（确定性派生），同 device 重复请求复用同一房间；
  userSig 每次重签（短时效）。
- **userSig 有效期 ≤600s**：环境变量 `TRTC_USER_SIG_EXPIRE_S` 配置超限自动回退 600。
- **会话意图**：进程内内存存储（MVP 单用户/单实例足够）；`sign` 成功后消费意图，防止 PC 重复拉 sidecar 进房。

## 部署步骤

1. **创建云函数**（腾讯云控制台 → 云函数 SCF 或 CloudBase → 云函数）：
   - 函数名：`trtc-sign`；运行时：**Python 3.x**（代码纯标准库，无 requirements）
   - 函数代码根：**本目录**（`index.py` 为入口，`main_handler` 为处理器）
   - 超时 3s、内存 128MB 即可
2. **配置环境变量**（不可省略）：
   - `TRTC_SDKAPPID` = TRTC 控制台应用 ID（如 `1600155678`）
   - `TRTC_SECRETKEY` = TRTC 控制台 SDK 密钥（**唯一存此处**）
   - `TRTC_ROOM_PREFIX` = `jax-`（默认）
   - 可选：`TRTC_DEVICE_WHITELIST`（逗号分隔白名单）、`TRTC_USER_SIG_EXPIRE_S`
3. **API Gateway 触发器**：新建触发器，路径 `/api/v1/voice/session`，方法 **ANY**（POST+GET+OPTIONS），
   **开启「启用 CORS」**（手机 App 跨域直调需要；代码侧已带 CORS 头）。
   记录触发器的公网访问 URL（`https://<env-id>.service.tcloudbase.com/api/v1/voice/session`）。
4. **验证**：
   ```bash
   curl -X POST https://<env-id>.service.tcloudbase.com/api/v1/voice/session \
        -H 'Content-Type: application/json' -d '{"device_id":"jax-test-001"}'
   # → {"code":0,"data":{"room_id":"jax-jax-test-001","user_id":"jax-test-001",
   #      "user_sig":"...","sdk_app_id":1600155678,"scene":"audio_call"},"message":""}
   ```

## 安全说明

- **SecretKey 唯一持有方 = 云函数环境变量**；PC `.env` 生产路径置空（Phase A 本地冒烟例外）、手机 App 不持有。
- 房间鉴权依赖 userSig（无合法签名无法进房），防枚举依赖此而非房间号不可猜。
- `TRTC_DEVICE_WHITELIST` 建议配置（MVP 单用户）；后续可升级正式 device 注册/绑定 + `X-Device-Token`。
- 代码不落任何密钥默认值；日志只打 device_id / room_id，不打 user_sig / secret_key。

## 本地单测

```bash
cd deploy/trtc-sign
python -m pytest tests -q
```

覆盖：假 key 验签（parse_user_sig 解包字段 / HMAC 确定性）、契约字段、同 device 幂等、
userSig 有效期 ≤600s、非法 device 40001、凭据缺失 50300、pending/sign 意图流、CORS 预检。

## 已知限制

- 会话意图为内存存储：多实例部署时 PC `pending` 可能命中非上次签发实例。
  MVP 单用户单实例足够；多实例需换 Redis/TencentDB 共享（部署代码预留 `TrtcSignService` 可注入存储）。
- `TRTC_USER_SIG_EXPIRE_S` 配置 >600 自动回退 600（契约硬约束）。
