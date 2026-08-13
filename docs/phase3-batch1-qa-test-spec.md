# Phase 3 Batch 1 QA 验收测试审计规格

> 角色：mvp-dev-expert-team-qa（独立测试作者 / checker）  
> 日期：2026-08-07  
> 唯一产品契约：`docs/commercial-upgrade-SPEC.md` v1.1  
> 实施边界：`docs/plans/2026-08-07-commercial-duplex-voice-upgrade.md` Task 1-3  
> HTTP 契约：`docs/api/commercial-voice-openapi.yaml`  
> 商业目标档：Silver；本批高风险安全路径应具备 Gold 所需的独立作者、畏惧缺陷和杀手测试基础。

## 1. RoleVerdict

```yaml
RoleVerdict:
  verdict: fail
  scope: Phase 3 Batch 1, Task 1-3 acceptance-test readiness
  blocking:
    - id: QA-P0-001
      rule: SPEC v1.1 AC-04 and section 9.1
      evidence: Current POST /api/v1/voice/session accepts an unauthenticated request and returns HTTP 200/code 0.
      expected: Missing or invalid Bearer credential returns HTTP 401 with code 40101; production missing validator, nonce storage, rate limit, TLS, or TRTC secret fails closed.
    - id: QA-P0-002
      rule: Plan Task 1-3 red-test gate
      evidence: All four required test files are absent; the prescribed pytest command exits 4 with file not found.
      expected: Independent, executable red tests exist and fail for the intended missing commercial behavior, not for import or collection errors.
    - id: QA-P0-003
      rule: SPEC sections 4 and 12.3 exact dependency/lock requirement
      evidence: requirements.txt uses >= ranges and pet-ui/package.json uses caret ranges; Python environment versions differ from the minimum-looking declarations and no reproducible Python lock is present in the inspected baseline.
      expected: Exact versions and committed lock material pass the contract test without treating an installed environment as the lock source.
    - id: QA-P0-004
      rule: SPEC sections 5, 9.1 and EARS AC-01 through AC-04
      evidence: Current voice routes have no commercial pairing-code/register/devices/revoke implementation, no principal isolation, no nonce consumption, and no device/IP route limiter.
      expected: Required black-box and storage/concurrency tests pass against real route/service/storage composition.
  advisory:
    - Legacy session tests currently assert HTTP 200 and scene audio_call. The locked OpenAPI requires HTTP 201 and scene trtc_full_duplex; these tests must be migrated deliberately, not silently weakened or deleted.
    - FastAPI TestClient emits a StarletteDeprecationWarning about httpx. Resolve through an exact, verified dependency set; do not suppress the warning as a substitute.
  evidence:
    - command: C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/contract/test_commercial_contract.py backend/tests/unit/test_voice_storage.py backend/tests/unit/test_voice_auth.py backend/tests/integration/test_voice_security_routes.py -q
      result: exit 4, first required file not found
    - command: PYTHONPATH=backend ... TestClient POST /api/v1/voice/session without Authorization or X-Request-Nonce
      result: HTTP 200, code 0, user_sig issued
    - command: pytest backend/tests/unit/test_rtc_session_sign.py backend/tests/unit/test_voice_session_qa.py -q
      result: 21 passed, 1 deprecation warning
```

P0 未归零，本批当前不得声称完成，更不得上线。本文定义必须落盘并先红后绿的独立验收测试；QA 未修改生产代码，也未修改后端工程师可能同时创建的四个测试文件。

## 2. 必读标准与采用原则

已读取并应用：

- `test-discipline.md`：先做影响图；测试来自 Spec/OpenAPI，不复述实现；回归率与解决率并列；高风险路径要求畏惧缺陷杀手测试。
- `test-integrity-anti-gaming.md`：测试/门禁 diff 独立检查；禁止删弱断言、skip/xfail、框架篡改、可见样例特判和 mock-only 绿灯。
- `verifier-critic-pattern.md`：QA 作为独立 checker，只按 diff、验收标准、契约和可执行证据给出 pass/fail。
- `generated-code-failure-modes.md`：覆盖错误路径、主体隔离、并发、依赖存在性、生产上下文和性能/索引，不接受只跑 happy path。
- `production-readiness-scorecard.md`：商业生产最低 Silver；总档取七维最低档。本批只提供 Task 1-3 局部证据，不能冒充全项目生产评级。

## 3. 改动影响分析

### 3.1 本次改动范围

计划新增或修改：

- 契约/依赖：OpenAPI、ADR、`requirements.txt`、`pet-ui/package.json` 及 lockfile。
- 安全存储：SQLite 九张商业语音表、迁移、凭证与 pairing code 哈希、nonce、限流桶。
- 身份/路由：owner、device、sidecar、session 四类主体；pairing、register、session、sign、status、stream；nonce、防重放、限流和生产 fail-closed。

改动类型：新增共享安全基础设施，并替换现有匿名/开发态语音入口的核心安全语义。

### 3.2 下游影响面

- 直接调用方：Android 会话签发、Windows sidecar 签发、设备管理 UI、WebSocket 握手、状态查询、现有 `build_session_router()` 相关测试。
- 共享状态：SQLite credential/pairing/nonce/rate-limit/session 撤销数据；生产配置；路由依赖注入。
- 旧行为风险：
  - 匿名 `/session`、`/session/sign` 当前成功，改为必须鉴权；风险高，且旧测试会由绿转红，但这是锁定契约的预期行为迁移，必须显式更新。
  - `scene=audio_call`、HTTP 200、响应缺 `session_id/expires_at` 与 OpenAPI 冲突；风险高。
  - status/stream 的现有网关路径可能因统一鉴权和主体隔离发生回归；风险高。
  - 开发态 `VOICE_TOKEN` 为空自动放宽不能泄漏到 production；风险高。
  - SQLite 新迁移可能影响启动、事务与并发；风险高。

### 3.3 回归测试优先级

1. 必测：现有 userSig 独立 HMAC 验签、TTL、设备隔离；商业鉴权接入后仍应通过其核心密码学断言。
2. 必测：stream/status 在正确凭证下仍可用，同时跨主体/跨设备不可见。
3. 必测：生产配置完整时可启动/签发，缺任一项立即关闭失败。
4. 应测：既有非商业 voice gateway 测试的预期迁移，禁止为保绿保留匿名旁路。
5. 抽测：与本批不共享状态的 capture、push、brain 模块。

## 4. 当前测试与依赖事实基线

### 4.1 现有测试

- `backend/tests` 当前有 45 个 Python 测试文件。
- 指定的四个 Batch 1 测试文件均不存在。
- 现有签发相关两文件共 21 个用例通过，但证明的是旧契约：无 Bearer/nonce、HTTP 200、`scene=audio_call`。
- 当前工作树相对上一提交：测试文件数 45 对 45；`assert/expect` token 计数 864 对 859；未发现测试文件删除。
- 相对上一提交未发现新增 skip/xfail/.only/focus 行，未发现 `pyproject.toml` 等测试框架配置改动。
- 上述只是当前 integrity 基线；后端提交后必须重新执行并保存 diff 证据。

### 4.2 实际依赖

当前指定 Python 环境：

| 包 | 实际版本 |
|---|---:|
| fastapi | 0.141.1 |
| pydantic | 2.13.4 |
| pytest | 9.1.1 |
| httpx | 0.28.1 |
| PyYAML | 6.0.3 |

当前声明不符合精确锁定：`requirements.txt` 存在大量 `>=`；`pet-ui/package.json` 的生产和开发依赖使用 `^`。测试必须检查声明和 lockfile，不能只检查本机 `import` 成功。

## 5. 四个测试文件的强制测试规格

### 5.1 `backend/tests/contract/test_commercial_contract.py`

#### 端点计数必须避免 stream GET 重复计数

必须以 OpenAPI `paths` 下的 HTTP method operation 集合做集合相等断言，不得把“8 个 path”与“8 个 operation”分别相加，也不得因 stream 是 GET 而另加一次。准确期望集合：

```python
EXPECTED_OPERATIONS = {
    ("post", "/api/v1/voice/devices/pairing-code"),
    ("post", "/api/v1/voice/devices/register"),
    ("get", "/api/v1/voice/devices"),
    ("post", "/api/v1/voice/devices/{device_id}/revoke"),
    ("post", "/api/v1/voice/session"),
    ("post", "/api/v1/voice/session/sign"),
    ("get", "/api/v1/voice/status"),
    ("get", "/api/v1/voice/stream"),
}
```

测试从 `paths` 仅抽取合法 HTTP method key，断言 `actual == EXPECTED_OPERATIONS` 且 `len(actual) == 8`。不得用子串次数、YAML 文本 grep 或 `len(paths)+stream`。

#### 必须证明的行为

1. OpenAPI 是 3.0.x，可解析，operation 集合严格等于上表。
2. 全部错误码严格存在：40001、40101、40102、40103、40401、40801、40901、41301、42901、50300、50401。
3. pairing-code POST 必须 owner Bearer + nonce；成功 schema 必含 `pairing_code/expires_at/max_uses`，`max_uses` 唯一值为 1。
4. pairing TTL 不能只靠自然语言。契约必须出现机器可判定的最大 TTL，例如 `ttl_seconds.maximum: 300`，或等价 vendor extension。仅有 `expires_at` 与描述“<=300”不足以证明动态差值。本项若 OpenAPI 无机器可判定约束，测试应红并推动契约修正。
5. register 必须将 pairing_code 作为 bootstrap secret 输入、nonce 必填、成功只在 201 返回 `credential_secret`；该字段 `readOnly: true` 且列表 schema 不得含 secret/hash。
6. `/session` 只接受 deviceBearer；`/session/sign` 只接受 sidecarBearer；不得互换或出现匿名安全项。
7. stream 是受保护的 GET upgrade，要求 sessionBearer + nonce，并锁定 hello 超时、允许控制指令、40901、41301、640-byte 帧和三维背压。
8. 生产 server 只能为 HTTPS；文档明确缺 TLS/validator/rate-limit/TRTC secret fail-closed。
9. Python运行依赖全部使用 `==` 等精确、可重复形式；不得出现 `>=`, `~=`, 无版本。环境 marker 可保留，但每个可安装分支必须精确。
10. `pet-ui/package.json` 所有 dependencies/devDependencies 不得以 `^`, `~`, `latest`, `*`, 范围比较符开头；`lucide-react` 精确为 0.469.0，且依赖中不存在第二图标库。
11. 所需 lockfile 实际存在且版本与 manifest 对齐；不能把 `node_modules` 当锁文件。

#### 畏惧缺陷 / 杀手测试

- 删除 stream operation：端点集合测试必须红。
- 错误地将 stream GET 计数两次：集合长度仍只能是 8，测试代码自身应有一个小型 fixture 自检去证明不会重复计数。
- 将 `max_uses` 改 2、TTL 上限改 301、漏 40102、把 sign security 改 deviceBearer、把依赖改回 caret：对应测试必须各自变红。

### 5.2 `backend/tests/unit/test_voice_storage.py`

#### 必须证明的行为

1. 在临时真实 SQLite 文件执行正式迁移，九表全部存在：settings、device_credentials、pairing_codes、revoked_sessions、session_events、transcripts、privacy_audit_events、consumed_nonces、rate_limit_buckets。
2. 按 Spec 校验关键 unique/index，不只检查表名：device_id 唯一、code_hash 唯一、session_id 唯一、subject+nonce_hash 唯一、subject+route+window 唯一，以及 expires/consumed/status/device/created_at 相关索引。
3. 事务失败回滚：在同一事务制造第二步约束错误，第一步写入不得残留。
4. `create_pairing_code` 返回一次性明文给调用者，但库中只有不同于明文的 `code_hash`；数据库字节扫描不得出现 pairing_code。
5. pairing 的 `expires_at-created_at` 必须满足 `0 < ttl <= 300s`。测试使用受控 clock 或允许极小时间误差，不能只断言配置常量。
6. `consume_pairing_code` 原子 compare-and-update：第一次成功并绑定一个 device；第二次为已消费；过期值失败；失败不得创建半成品 device credential。
7. 并发消费必须使用真实文件数据库、独立连接和同步起跑；N 个线程/任务消费同一码时恰好一个成功，其余明确冲突，最终只有一个 `consumed_device_id` 和一个 device credential。
8. 保存 device credential 时只存抗离线攻击哈希；存取 API 不返回明文。数据库文件扫描不得出现 credential_secret。
9. nonce 只存主体绑定哈希；同主体同 nonce 唯一，不同主体不能互相污染；过期清理不删除未过期记录。
10. 日志、审计 metadata 拒绝 secret、nonce、userSig、原始音频、完整转写等敏感键/值；不能仅测试字段名白名单而放过值泄漏。
11. SQLite 连接设置必须支持事务和并发语义；测试不得用内存 dict/fake repository 替代正式迁移与数据库。

#### 畏惧缺陷 / 杀手测试

- 把 `<=300` 写成 `<300` 或允许 301：边界 300 应成功、301 应失败。
- 先 SELECT 后 UPDATE 导致竞态：并发测试必须得到恰好一个成功。
- 哈希函数误存输入原文：数据库字节扫描必须红。
- pairing 已消费后仍创建 credential：行数/绑定断言必须红。
- 事务中途异常仍部分提交：回滚测试必须红。

### 5.3 `backend/tests/unit/test_voice_auth.py`

#### 必须证明的行为

1. `CredentialPrincipal` 至少包含不可客户端自报的 `type/subject_id/credential_id`，主体来自服务端验证器结果。
2. owner、device、sidecar、session credential 相互隔离：交叉矩阵逐项拒绝，不能只测一个错误组合。
3. 缺 Authorization、非 Bearer、空 token、无效 token、过期 token 返回 auth_failed/40101。
4. revoked credential 返回 40103，而不是泛化为 40101；敏感 token 不进入异常文本或日志。
5. nonce 绑定主体并原子消费：首次成功；同主体重放、过期、主体不匹配均为 40102；不同主体碰巧使用相同 nonce 字符串不应被错误全局串扰。
6. nonce 存储与 credential validator 不可用时生产关闭失败，不能退化为“跳过验证”。
7. 限流同时按 device/主体与 IP/route 生效；达到阈值后的下一请求返回 42901 和正数 Retry-After；窗口恢复边界由受控 clock 验证。
8. userSig TTL 必须由独立解析器验证 `0 < expire <= 600`，且主体绑定 session/device/user/room；不能只检查配置值。
9. production 配置矩阵逐一移除 validator、sidecar credential、nonce store、rate limiter、TLS endpoint、SDKAppID、SecretKey，每个组合都必须拒绝启动或令全部 session 签发返回 50300。
10. production 不得接受 http/ws endpoint，不得自动启用匿名 WS 或本地 SecretKey fallback；development 的便利配置不能影响 production。

#### 畏惧缺陷 / 杀手测试

- 主体类型判断取反或只比 token 是否存在：交叉主体矩阵必须红。
- nonce key 只用 nonce、不含 subject：不同主体同 nonce 测试必须红。
- 阈值 off-by-one：精确验证第 N 次和 N+1 次。
- fail-open 默认值：参数化缺项矩阵每一项都必须红。
- userSig TTL 改 601：独立解析测试必须红。

### 5.4 `backend/tests/integration/test_voice_security_routes.py`

#### 组合方式

必须用真实 FastAPI router + 正式 auth/nonce/rate-limit/storage service + 临时 SQLite。允许 fake clock、确定性 secret hasher 或假的 TRTC 签名外部适配器，但不可把整个 auth/router/store mock 掉。每个请求必须断言 HTTP 状态、统一 envelope `code/data/message`、存储副作用和敏感日志。

#### 必须证明的行为

1. `/session` 无 Bearer 返回 HTTP 401/code 40101；正确 device credential + 新 nonce 返回 201；响应满足 OpenAPI 字段并且 scene 为 `trtc_full_duplex`。
2. device A credential 请求 body 中的 device B 必须拒绝 40001 或明确授权错误，且绝不签发 B 的 userSig；主体隔离以存储副作用为零佐证。
3. device credential 调 `/session/sign`、sidecar credential 调 `/session` 均拒绝；正确 sidecar credential 只可为授权设备/用户签发。
4. 同一个 nonce 首次成功，第二次返回 HTTP 401/code 40102；并发相同 nonce 请求恰好一个成功，其余 40102。
5. 超过 device 或 IP 路由限额返回 HTTP 429/code 42901；换 token 不得绕过 IP 限流，换 IP 不得绕过 device 限流。
6. status 按 principal 过滤：device A 看不到 device B 的 session/指标；sidecar 只能看到其授权范围。
7. stream upgrade 缺 session credential、nonce 或主体绑定错误必须拒绝；已撤销/不匹配 session credential 不能握手。
8. production 缺 TLS、validator、rate-limit 或 TRTC Secret 的应用装配必须 fail-closed。若设计选择“拒绝启动”，测试断言 app factory 抛明确配置错误；若选择“拒绝全部会话”，对 session/sign/stream 全部断言 50300/拒绝握手。不得只测一个端点。
9. 错误响应和 caplog 不含 Authorization token、nonce、credential_secret、TRTC SecretKey、userSig。
10. 旧匿名旁路 `/ws/voice`、`/api/v1/voice/pair` 若仍被 production app 装配，必须证明受同等安全策略约束；否则这是 fail-open P0。

#### 畏惧缺陷 / 杀手测试

- 只校验 Bearer 存在不校验主体：交叉路由矩阵必须红。
- nonce 在业务执行后才消费：并发路由测试会出现多于一个 201，必须红。
- 限流只按 token：换 token/同 IP 测试必须红。
- status 未过滤：A/B 两套真实记录的黑盒响应测试必须红。
- production 仅 `/session` 关闭而 `/sign` 或 stream 放开：全端点参数化测试必须红。

## 6. 测试完整性反作弊门

后端提交后，以下任一成立即 P0 阻断：

1. 删除测试文件/用例或测试总行数异常下降。
2. 既有断言减少或从等值/集合相等弱化为真值/包含关系。
3. 新增 skip、xfail、pytest.importorskip、条件 return、`.only`、focus 或吞异常。
4. 断言值从实现返回值动态生成，而非 Spec/OpenAPI 常量；例如 `expected = service.issue(...)` 后再比较 route 响应属于自证。
5. 修改 `pyproject.toml`、pytest 配置、conftest、test script、coverage threshold 以绕过失败。
6. 单元层仅验证 mock 被调用，未验证真实哈希、SQLite bytes、事务或并发最终状态。
7. 集成层替换整个 auth/store/router，导致只测 mock 编排。
8. 将缺生产 secret/TLS 的用例 skip，或以“本机无生产环境”为由判 pass。

精确门禁命令（Git Bash）：

```bash
# 测试删除与文件面
git diff --name-status <BASE_SHA> -- 'backend/tests/' '**/*.test.*' '**/*.spec.*'

# 新增静音标记；命中后人工区分单词语义，任何真实测试跳过均阻断
git diff <BASE_SHA> -- 'backend/tests/' '**/*.test.*' '**/*.spec.*' \
  | C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -c \
  "import sys,re; print(''.join(x for x in sys.stdin if x.startswith('+') and re.search(r'skip|xfail|importorskip|\\.only|focus|pytest\\.mark\\.skip', x, re.I)))"

# 测试框架与阈值 diff
git diff <BASE_SHA> -- pyproject.toml pytest.ini setup.cfg tox.ini package.json pet-ui/package.json requirements.txt '**/conftest.py'

# 四个文件单独执行，防全套输出掩盖 collection/skip
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/contract/test_commercial_contract.py -q -ra
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/unit/test_voice_storage.py -q -ra
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/unit/test_voice_auth.py -q -ra
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/integration/test_voice_security_routes.py -q -ra
```

`-ra` 输出中出现 skipped/xfail/xpass 均需阻断审查。测试通过但未执行真实用例、collection 数量意外下降，也不得放行。

## 7. 准确执行顺序与预期

### 7.1 RED 门

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/contract/test_commercial_contract.py -q -ra
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/unit/test_voice_storage.py -q -ra
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests/unit/test_voice_auth.py backend/tests/integration/test_voice_security_routes.py -q -ra
```

合格 RED 的定义：用例已成功 collection，因缺失商业行为或断言不满足而 FAIL；`ModuleNotFoundError`、fixture 错误、语法错误、文件不存在不是有效红灯。

### 7.2 GREEN 局部门

同上命令必须全绿、无 skip/xfail，再运行受影响旧行为：

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest \
  backend/tests/unit/test_rtc_session_sign.py \
  backend/tests/unit/test_voice_session_qa.py \
  backend/tests/unit/test_voice_gateway.py \
  backend/tests/unit/test_voice_session.py -q -ra
```

若旧测试因锁定契约发生预期变化，必须在独立 test diff 中逐条说明由哪一条 Spec/OpenAPI 替代，不得删除以换绿。

### 7.3 Batch 1 回归门

```bash
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest backend/tests -q -ra
```

要求：新验收全部通过；回归率为 0；无 skip/xfail；日志不泄密；无警告被吞。依赖/契约测试通过不等于后续 Android 真机或全双工商业门已通过。

## 8. 失效模式核对

| 失效模式 | 本批防线 | 当前结论 |
|---|---|---|
| Happy-path 偏差 | 40101/40102/40103/42901/50300、过期、并发、跨主体、跨设备 | 未有要求测试，阻断 |
| 沉默逻辑错误 | operation 集合相等、TTL 边界、并发恰好一个、主体矩阵、参数化 fail-closed | 规格已定义，待落盘 |
| 幻觉依赖接口 | 实际版本核对、精确 manifest/lock、真实 FastAPI/SQLite | 当前依赖未精确锁定 |
| 缺失系统上下文 | production、TLS、validator、nonce、device/IP 限流、数据隔离 | 当前实现缺失，阻断 |
| 性能盲区 | pairing/nonce/rate-limit 索引、真实并发、路由限流窗口 | 待实现与验证 |
| 静默缺失 | 单文件 collection、全套 pytest、后续 lint/type/build | 四文件不存在，阻断 |

## 9. 局部生产就绪记分卡

本评分只针对 Phase 3 Batch 1 当前事实，不替代全项目 Phase 4 评级。

| 维度 | 当前档位 | 证据/缺口 |
|---|---|---|
| 测试 + 回归 | Bronze 以下 | 四个强制测试缺失；只有旧匿名签发测试绿 |
| 契约 | Bronze | OpenAPI 有 8 个 path/8 个 operation，但 TTL 缺机器可判定上限；实现尚未对齐 |
| 安全 | Bronze 以下 | 无认证签发成功；nonce、主体隔离、限流、TLS fail-closed 未实现 |
| 无障碍 | 未评 | 不在 Task 1-3 范围，不能推断通过 |
| 性能 | Bronze 以下 | 新索引、并发消费、限流容量尚无证据 |
| 可观测 | Bronze 以下 | 安全审计/脱敏错误与限流证据尚无验收测试 |
| 发布安全 | Bronze 以下 | 无本批完整回滚/发布证据，且 P0 未归零 |
| 总档 | Bronze 以下 | 取最低档；禁止商业生产 |

## 10. 本批完成判据

只有全部满足才可把本 RoleVerdict 改为 pass：

- 四个测试文件均存在、有效先红、后绿，且无 skip/xfail/mock-only。
- 8 个 operation 以严格集合相等验证，stream GET 仅计一次。
- pairing 动态 TTL 不超过 300 秒、原子单次消费、并发恰好一个成功、明文 pairing/credential 不入库。
- owner/device/sidecar/session 主体隔离，40101、40102、40103、42901 均有黑盒和副作用证据。
- production 缺 TLS/validator/nonce/rate-limit/sidecar credential/TRTC SDKAppID 或 SecretKey 任一项都 fail-closed。
- 现有 userSig 独立验签和受影响 voice 回归全绿，回归率为 0。
- integrity diff 无删除/弱断言/静音标记/框架篡改。
- P0 缺陷数为 0；局部测试、契约、安全至少具备 Silver 证据。
