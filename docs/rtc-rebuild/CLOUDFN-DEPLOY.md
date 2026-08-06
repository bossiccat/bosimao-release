# CLOUDFN-DEPLOY — trtc-sign 云函数部署方案（Phase B）

> 版本：v1.0 Accepted（2026-08-06，team-lead 裁决 O1/O2）
> 作者：architect（首席架构师）
> 状态：**已定稿**（O1 默认域名 ✅ 接受；O2 意图存储 NoSQL ✅ 接受；ADR-012 变更记录已回写）
> 依据：docs/decisions/ADR-012-rtc-transport.md（决策 #7 云函数代签）、docs/rtc-rebuild/ARCHITECTURE.md（§3.4 会话签发与进房协调）、docs/rtc-rebuild/PC-INTEGRATION.md（§2.3 会话契约）、AUDIT.md（D2 安全维度）
> 环境实况（2026-08-06 CloudBase MCP 查询）：已连环境 **`jinhong-d2g55ycl591208475`**（别名 `jinhong`，地域 `ap-shanghai`，套餐「体验版」至 2027-01-07，已开通 NoSQL + PostgreSQL + 云函数）。**无需新建环境，复用现有环境。**

---

## 0. TL;DR（30 秒结论）

- **部署位置**：复用现有 CloudBase 环境 `jinhong-d2g55ycl591208475`（ap-shanghai），新建云函数 `trtc-sign`（**事件函数 + Python 3.10**），经「HTTP 访问服务」绑定路由 `/api/v1/voice`，公网 URL：
  `https://jinhong-d2g55ycl591208475-1436773060.ap-shanghai.app.tcloudbase.com/api/v1/voice/session`
  > ⚠️ **URL 域名后缀说明**：默认域名 host = `EnvId-<AppId>`（AppId=1436773060），**EnvId 本身不带后缀**（CLI/MCP/控制台用 `jinhong-d2g55ycl591208475`）；只有公网 URL 的 host 含 `-1436773060`。手机/PC 配置 base URL 时**必须带全后缀**。
- **代码形态**：纯 Python 标准库（`usersig.py` 已实现官方 GenUserSig 算法，零第三方依赖），**不需要** Flask / scf_bootstrap / 端口 9000。函数入口 `main(event, context)`，按 SCF API 网关事件结构解析 HTTP 请求，返回 `{statusCode, headers, body}`。
- **环境变量**：`TRTC_SECRETKEY`（唯一持有方，控制台配置后自动加密）、`TRTC_SDKAPPID=1600155678`、`TRTC_ROOM_PREFIX=jax-`、可选 `TRTC_DEVICE_WHITELIST`。**零明文落 repo / git / 日志 / cloudbaserc.json。**
- **会话意图状态**：持久化于 CloudBase NoSQL collection `voice_intents`（每 device 一文档），供 PC 轮询发现手机发起的会话意图。
- **进房时序**：手机先进房等 PC + PC 常驻轮询（每 ~2s）→ 发现意图 → 签 PC 自身 userSig → 进同一房间。PC 通常 ≤2s 加入，无"谁等谁"死锁。

---

## 1. 部署目标与决策

### 1.1 目标

手机（深圳 4G 公网）直调云函数获取 TRTC userSig 进房；PC（衡阳 NAT 后，无公网入站）由主动外呼轮询协调进房。**全程不依赖 PC 公网可达**（ARCHITECTURE §3.4 裁决）。

### 1.2 选型：事件函数 + HTTP 访问服务（推荐）

| 方案 | 说明 | 结论 |
|------|------|------|
| **事件函数 + HTTP 访问服务** | `exports.main(event, context)`（Python 为 `main(event, context)`），经控制台「HTTP 访问服务」绑定触发路径 | **推荐**：trtc-sign 是纯标准库逻辑（HMAC-SHA256 计算），无 Web 框架依赖；绑定路径 `/api/v1/voice` 后 URL 与契约完全一致 |
| HTTP 云函数（Web 函数，Flask） | 监听 9000 端口 + scf_bootstrap，Flask 应用形态 | 备选：仅当 be-pc 需要标准 Web 框架路由时；多出 scf_bootstrap / 依赖打包 / 端口管理成本 |

> 关键约束（CloudBase 官方）：**事件函数**入口为 `main(event, context)`，HTTP 请求经访问服务网关注入 `event`；**HTTP 云函数**才是"Web 服务监听 9000 端口"形态。二者不要混用（cloud-functions skill：Mixing Event Function code shape with HTTP Function code shape 是常见坑）。本项目取**事件函数**。

### 1.3 已确认环境（MCP envQuery，2026-08-06）

| 项 | 值 |
|----|----|
| EnvId | `jinhong-d2g55ycl591208475` |
| 别名 | `jinhong` |
| 地域 | `ap-shanghai` |
| 套餐 | 体验版（baas_trial，2027-01-07 到期；含云函数免费调用额度） |
| 后端 | NoSQL（FlexDB）+ PostgreSQL 均已开通（RuntimeMode=postgresql） |
| 云函数命名空间 | `jinhong-d2g55ycl591208475`（ap-shanghai） |

> 体验版免费额度：云函数调用次额度足够 MVP（单用户 1v1，QPS 远低于限额）。HTTP 访问服务默认域名适用于开发/测试；**生产上线前**建议绑定已备案自定义域名（MVP 阶段可接受默认域名，风险见 §7）。

---

## 2. 部署步骤

### 2.1 控制台部署（主路径，确定性最高）

1. **开启 HTTP 访问服务**：云开发控制台 → 当前环境 → 左侧「环境管理 → HTTP 访问服务」→ 打开全局开关。
2. **创建云函数**：云开发控制台 → 「云函数」→ 「新建云函数」：
   - 函数名称：`trtc-sign`
   - 运行环境：**Python 3.10**（如控制台仅列 3.6/3.9 可选 3.9；代码为标准库，兼容）
   - 提交方式：本地上传文件夹 → 选择 `deploy/trtc-sign/` 目录
   - 自动安装依赖：不开启（**纯标准库，无 requirements.txt**；若 be-pc 引入 tcb-admin-python 访问 NoSQL，则需开启并在 requirements.txt 声明）
3. **配置环境变量**：函数详情 → 「函数配置」→「环境变量」（控制台保存后自动加密）：
   - `TRTC_SECRETKEY` = （从项目根 .env 复制，仅此处持有；**禁止进任何文档/git/日志**）
   - `TRTC_SDKAPPID` = `1600155678`
   - `TRTC_ROOM_PREFIX` = `jax-`
   - `TRTC_DEVICE_WHITELIST` = （可选，逗号分隔，见 §4.2）
4. **配置超时/内存**：函数配置 → 超时 **3s**、内存 **128MB**（默认值即可，GenUserSig 纯 CPU 计算 <10ms，意图读写 <100ms）。
5. **绑定 HTTP 路由**：「HTTP 访问服务」→「路由管理」→「添加路由」：
   - 关联资源类型：云函数
   - 关联云函数：`trtc-sign`
   - 域名：默认域名
   - 触发路径：`/api/v1/voice`
6. **验证**：
   ```bash
   curl -X POST https://jinhong-d2g55ycl591208475-1436773060.ap-shanghai.app.tcloudbase.com/api/v1/voice/session \
     -H "Content-Type: application/json" -d '{"device_id":"jax-xxxxxxxx"}'
   # 预期：{"room_id":"jax-jax-xxxxxxxx","user_id":"jax-xxxxxxxx","user_sig":"...","sdk_app_id":1600155678,"scene":"audio_call"}
   ```

### 2.2 CLI 部署（备选，可进 CI）

```bash
npm i -g @cloudbase/cli
tcb login
# 部署代码
tcb fn deploy trtc-sign -e jinhong-d2g55ycl591208475 --force
# 绑定 HTTP 路由（/api/v1/voice → trtc-sign）
tcb service create -f trtc-sign -p /api/v1/voice -e jinhong-d2g55ycl591208475
```

> CLI 各版本参数略有差异，执行前先 `tcb fn --help` / `tcb service --help` 核对。**环境变量禁止写进提交 git 的 cloudbaserc.json**——secret 用控制台配置，或使用 `cloudbaserc.local.json`（加入 .gitignore）并在 CI 用 envsubst 注入占位符。

### 2.3 环境变量清单

| 变量 | 必填 | 说明 | 持有方 |
|------|------|------|--------|
| `TRTC_SECRETKEY` | ✅ | TRTC 控制台 SDK 密钥（签名用 HMAC-SHA256） | **唯一存云函数环境变量**；PC .env 生产路径置空 |
| `TRTC_SDKAPPID` | ✅ | `1600155678` | 云函数环境变量（与手机/PC 共用同一 SDKAppID） |
| `TRTC_ROOM_PREFIX` | ✅ | `jax-`；room_id = 前缀 + device_id | 云函数环境变量 |
| `TRTC_DEVICE_WHITELIST` | 可选 | 逗号分隔的合法 device_id；空 = 放行任意格式合法 device_id | 云函数环境变量 |

### 2.4 超时 / 内存 / 最小权限

| 项 | 建议 | 理由 |
|----|------|------|
| 超时 | 3s | GenUserSig 纯 CPU <10ms；意图 NoSQL 读写 <100ms；无外部调用 |
| 内存 | 128MB | 默认即可，无大对象 |
| 权限 | **不绑定任何额外资源** | 函数只做 HMAC 计算 + 单 collection 读写。不需要 VPC、不需要 CAM 角色、不需要存储、不调用 TRTC 管理 API。NoSQL 经云函数内置环境凭证访问（事件函数免密钥运行时路径） |
| 网络 | 默认出站 | 函数不调用外部公网 API（仅内部 DB），无需特殊网络配置 |

---

## 3. 云函数契约（给 be-pc 的接口契约）

### 3.1 入口与事件结构

```python
# deploy/trtc-sign/index.py（入口文件；usersig.py 从 backend/app/voice/ 原样复制，零修改）
def main(event, context):
    method = event.get("httpMethod", "")            # POST / GET
    path = event.get("path", "")                    # /api/v1/voice/session 等（含绑定前缀）
    headers = event.get("headers", {})
    query = event.get("queryStringParameters") or {}
    body = event.get("body", "")                    # 字符串；POST 请求体
    # 路由分发 → 返回 {"statusCode": int, "headers": {...}, "body": "json字符串"}
```

- `event` 由 HTTP 访问服务网关注入（SCF API 网关事件结构），**不是** CloudBase SDK 调用时的 `event`。
- `path` 包含绑定前缀（`/api/v1/voice/session`），按 `path.endswith("/session")` / `"/pending"` / `"/sign"` 分发即可（前缀匹配路由，见 §2.1 步骤 5）。
- `body` 为字符串，`json.loads` 前判空。

### 3.2 响应结构

```python
return {
    "statusCode": 200,
    "headers": {"Content-Type": "application/json; charset=utf-8", "Access-Control-Allow-Origin": "*"},
    "body": json.dumps({...}, ensure_ascii=False),
}
```

统一错误码沿用 REST 约定：`code=0` 成功、非 0 错误；HTTP 状态码按语义（200/400/401/404/409/500）。

### 3.3 端点清单（对齐 PC-INTEGRATION §2.3 / ADR-012 决策 #7）

| 端点 | 方法 | 请求 | 响应（成功） | 说明 |
|------|------|------|--------------|------|
| `/api/v1/voice/session` | POST | `{"device_id":"jax-xxxxxxxx"}` | `{"room_id":"jax-jax-xxxxxxxx","user_id":"jax-xxxxxxxx","user_sig":"...","sdk_app_id":1600155678,"scene":"audio_call"}` | 手机入口：校验 whitelist → room_id=前缀+device_id → 签手机 userSig（userId=device_id，expire=600s）→ 写意图（consumed=false）→ 返回凭证 |
| `/api/v1/voice/session/pending` | GET | query `device_id=xxx` | `{"pending":true,"room_id":"jax-jax-xxxxxxxx"}` | PC 轮询：读意图文档，返回是否未消费 |
| `/api/v1/voice/session/sign` | POST | `{"device_id":"jax-xxxxxxxx","user_id":"jax-pc-sidecar"}` | 同 session 返回结构（userId=jax-pc-sidecar） | PC 取自身 userSig：校验意图未消费 → 签 PC userSig → **原子置 consumed=true** → 返回凭证 |

错误语义：
- `TRTC_SECRETKEY` 缺失 / SDKAppID=0 → 500 `ConfigMissingError`
- device_id 非法（非 `[A-Za-z0-9_-]{1,64}`）→ 400
- device_id 不在白名单（若配置）→ 401
- sign 时意图不存在或已消费 → 404/409 `{"code":1,"message":"intent not pending"}`（PC 收到后跳过本轮，继续轮询）

### 3.4 会话意图存储（CloudBase NoSQL collection `voice_intents`）

- collection 需**先在控制台创建**（云开发 NoSQL 不会自动建集合）：数据库 → 新建集合 `voice_intents`。
- 文档结构（`_id` = device_id，每设备一文档）：

```json
{
  "_id": "jax-xxxxxxxx",
  "device_id": "jax-xxxxxxxx",
  "room_id": "jax-jax-xxxxxxxx",
  "phone_ts": 1754550000,
  "consumed": false,
  "pc_ts": null,
  "pc_user_id": null
}
```

| 操作 | SDK 动作 |
|------|----------|
| session（手机） | `db.collection("voice_intents").doc(device_id).set({...})`（upsert，consumed=false，phone_ts=now） |
| pending（PC 轮询） | `doc(device_id).get()` → `{pending: not consumed}` |
| sign（PC 消费） | 读文档 → 未消费 → 签 PC userSig → `where({consumed:false}).update({consumed:true, pc_ts, pc_user_id})`（**条件更新保证原子性**，防双 PC 竞态） |

- Python SDK：`tcb-admin-python`（官方 CloudBase Python 管理 SDK，`db.collection().doc().set/get` + `where().update()`）。若 be-pc 评估 SDK 引入成本过高，可先在函数内用**模块级内存 dict + TTL（600s）**实现意图存储（单用户 1v1 下 warm 实例命中率高），但需在 AUDIT D3 注明该简化与冷启动风险——**推荐 NoSQL 持久化**。

---

## 4. 安全审计（对齐 AUDIT D2，满分 20）

### 4.1 SecretKey 零明文（P0）

| 检查项 | 要求 | 验证方法 |
|--------|------|----------|
| SecretKey 唯一存云函数环境变量 `TRTC_SECRETKEY` | 代码零默认值（`os.environ["TRTC_SECRETKEY"]` 缺失即抛错，参照 `usersig.py` 现有逻辑） | grep 扫描 `deploy/trtc-sign/ backend/ mobile-app/` 无 `TRTC_SECRETKEY=xxx` 明文 |
| 不进 repo / git / 日志 | .gitignore 已含 `.env`；**新增**：`cloudbaserc.local.json`、`deploy/trtc-sign/.env*` 入 .gitignore；函数日志只打 `room_id/device_id`，**不打 user_sig/secret_key** | `git log -S TRTC_SECRETKEY` 零命中；日志代码 grep |
| 不进文档 / 上报 | 本文档与 ADR 一律不出现明文 | qa 独立 grep |
| 控制台加密存储 | 环境变量值保存后由平台加密（控制台不可见明文） | 部署后控制台核验 |

### 4.2 云函数鉴权

- **userSig 自校验**：本方案的安全边界是"凭证即能力"——云函数签发的 userSig 已绑定 `sdk_app_id + room_id(userId) + expire`，**有效期 ≤600s**（`gen_user_sig` 的 `expire_s=600` 契约）。即使云函数端点被匿名调用，攻击者拿到的也只是**限时、限房间、限用户**的一次性凭证，无法跨房间复用。
- **device_id 白名单（可选，建议 MVP 开启）**：环境变量 `TRTC_DEVICE_WHITELIST` 逗号分隔；配置后白名单外 device_id 一律 401。单用户项目设为自己手机 device_id 即可。
- **X-Device-Token**：后续正式态可升级（PC-INTEGRATION §2.3 提及），MVP 不实现，不增加复杂度。

### 4.3 防枚举 / 防滥用

| 风险 | 缓解 | 对应 QA-PLAN §6.3 |
|------|------|--------------------|
| 房间号枚举 | 房间鉴权依赖 **userSig 有效性**而非房间号不可猜（无合法 userSig 无法进房）；room_id 确定性（前缀+device_id）是定稿规则 | 房间号枚举遍历测试 → 无他人音频泄露 |
| userSig 泄露/复用 | expire ≤600s；每次会话重新签发 | token 泄露日志扫描 |
| 端点被刷 | 体验版环境 QPS 限额兜底（EnvQps 500）；可选函数内按 device 简单计数限流（内存态，MVP 可不做） | — |
| 陌生端进房 | TRTC 房间鉴权：无合法 userSig 进房被拒 | RTC 房间越权测试 → 鉴权错误码 |

### 4.4 审计检查清单（写进 AUDIT 记录）

- [ ] `grep -rn "TRTC_SECRETKEY" deploy/trtc-sign/ backend/ mobile-app/ docs/` 仅命中环境变量名引用，无明文值
- [ ] `git log -S "TRTC_SECRETKEY"` 零命中
- [ ] userSig `parse_user_sig` 校验 `TLS.expire ≤ 600`
- [ ] 函数日志不含 user_sig / secret_key 字段
- [ ] 未绑定 VPC / 未附加 CAM 角色 / 未申请存储
- [ ] 意图 collection 仅 `voice_intents` 一个，无全库权限

---

## 5. PC sidecar 拉取路径与进房时序

### 5.1 PC 轮询路径（be-pc 后端常驻轮询器）

```
┌─ 手机 ──────────────────────────────┐        ┌─ PC 后端（衡阳，无入站）──────────────┐
│ KWS 唤醒                            │        │ 常驻轮询器 每 ~2s                     │
│  POST /api/v1/voice/session         │  HTTPS │  GET /api/v1/voice/session/pending   │
│  └ 拿 room_id+user_sig → enterRoom  │ ─────▶ │  └ pending=true → POST /sign          │
│  （先进房等 PC）                     │        │      └ 拿 PC userSig → sidecar        │
│                                     │        │         enterRoom(同 room_id)        │
└─────────────────────────────────────┘        └─────────────────────────────────────┘
```

1. PC 后端常驻轮询器每 ~2s `GET <云函数>/api/v1/voice/session/pending?device_id=xxx`
2. 发现 `pending=true` → `POST <云函数>/api/v1/voice/session/sign` body `{"device_id":xxx,"user_id":"jax-pc-sidecar"}`
3. 云函数签发 PC userSig（userId=`jax-pc-sidecar`，expire=600s）+ 原子置 consumed → 返回凭证
4. PC 后端 → localhost 控制通道 → `rtc_bridge` → localhost WS → sidecar `enterRoom(room_id, userId="jax-pc-sidecar", userSig)`
5. sidecar `onEnterRoom` 成功 → 回报后端"已就绪"（对齐 PC-INTEGRATION §4.4）

### 5.2 进房时序确认（任务项：手机先进房 vs PC 常驻轮询）

**结论：手机先进房等 PC，PC 常驻轮询加入，两段并行无死锁。**

- 手机路径是**主动触发**：KWS 唤醒 → 立即 POST /session → 立即 enterRoom（不等 PC）。
- PC 路径是**被动发现**：常驻轮询（每 2s）发现意图 → 2s 内加入同一房间。
- TRTC 房间由**首个合法进房者隐式创建**（无显式创建 API，需有效 userSig），后加入者直接进同一房间——不存在"先到者建房失败"或"后到者被拒"问题。
- 窗口余量：userSig 有效期 600s ≫ PC 轮询周期 2s，手机先进房后即使 PC 轮询偶发抖动，也在有效期窗口内完成协调。
- **无需"PC 先建房间等手机"**：手机主动进房天然建立房间，PC 轮询是唯一需要的协调机制（PC 无公网入站，不能由手机反向通知）。

### 5.3 房间生命周期与幂等

| 场景 | 行为 |
|------|------|
| 会话开始 | 手机 POST /session（写意图）→ 手机 enterRoom 隐式建房 → PC ≤2s 内 enterRoom 进同房 |
| 会话期重复请求 | 同 device 幂等：room_id 确定性派生，重复 POST /session 返回同一 room_id，不重复拉 PC 进房（consumed=true 时 PC 不再进房） |
| 会话结束 | 任一端 `exitRoom`；TRTC 房间**末位用户退房云侧自动销毁**；sidecar `onRemoteUserLeave` → 退房回待命（对齐 ARCHITECTURE §5.2 房间生命周期不变） |
| 意图清理 | 新会话覆盖旧意图（upsert）；过期意图（phone_ts 超 600s）在 pending 读取时视为已失效并置 consumed=true，防僵尸意图 |

---

## 6. ADR-012 变更记录（已回写 2026-08-06）

云函数部署位置/域名已确认，ADR-012 变更记录已追加两行（详见 `docs/decisions/ADR-012-rtc-transport.md`）：
1. 云函数部署位置/域名确认（环境/函数形态/路由/URL/环境变量/最小权限/SecretKey 零明文）。
2. O1/O2 裁决（默认域名 ✅ 接受 + NoSQL 意图存储 ✅ 接受）。

> 上文表格为回写前草稿，正式记录以 ADR-012 变更记录为准（日期 2026-08-06，team-lead 裁决）。

---

## 7. 风险与开放项（需 team-lead 裁决）

| # | 项 | 说明 | 裁决 |
|---|----|------|------|
| O1 | 默认域名限制 | `*.app.tcloudbase.com` 默认域名仅限开发测试：有频率限制、部分高级功能不可用、浏览器直访有安全提示中间页 | **✅ 已裁决（2026-08-06）**：MVP 接受默认域名（手机 App 直调不走浏览器，无提示页问题）；生产上线前绑定已备案自定义域名（挂 OPEN-DECISIONS O-016，waiting-on-external-condition: 域名备案） |
| O2 | 意图存储实现选择 | NoSQL collection（推荐，可审计可测试）vs 函数内存 dict + TTL（轻量但冷启动有漏意图风险） | **✅ 已裁决（2026-08-06）**：用 CloudBase NoSQL collection `voice_intents`（每 device 一文档，sign 条件更新置 consumed 防竞态）；否决函数内存态（冷启动丢状态风险） |
| O3 | Python SDK 依赖 | `tcb-admin-python` 为官方但维护低频的 SDK | 若引入成本高，可改为 HTTP 云函数（Node.js）形态由 be-pc 择一；本文档以 Python 事件函数为主契约，端点/存储/安全契约与运行时无关 |
| O4 | 体验版到期 | 2027-01-07 到期，含免费额度 | Phase C 前评估按量付费或资源包，与 RTC 免费额度策略合并评估 |
| O5 | `deploy/trtc-sign/` 目录 | 任务描述 be-pc 正在写，当前目录尚不存在 | be-pc 按 §3 契约落地，目录结构：`index.py`（入口）+ `usersig.py`（复制自 backend/app/voice/）+ `rtc_session.py`（去 pydantic 依赖，改 dataclass 或直接读 os.environ） |
