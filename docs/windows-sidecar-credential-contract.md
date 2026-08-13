# Windows Tauri sidecar OS-bound credential 实现契约

## 1. 目标与完成定义

本契约只解决 Windows Tauri 对一个逻辑 opaque `VOICE_SIDECAR_CREDENTIAL` 的安全持久化、首次启动、child-only 注入、重启重读、可恢复本地轮换和撤销。active/staging/backup 是该值的事务槽，不是多个身份或多个业务凭证。完成后：

1. fresh-install供给仍是P0 blocking；条件安装通道通过放行证据后，最终Windows交互用户可由一次性受信provisioner把credential写入固定active槽。
2. Tauri setup 主动执行首次启动，不依赖 watchdog 从 `Stopped` 拉起。
3. 每次首次启动或watchdog重启均先校验externalBin/hash，再取得双层锁、恢复未决事务并从active重读credential。
4. credential 只进入该次 child process 的 `VOICE_SIDECAR_CREDENTIAL`，不进入 argv、父进程全局环境、普通文件、日志或前端。
5. credential 缺失、损坏、读取失败或已撤销时不创建 child；主 Tauri 应用继续运行并给出脱敏状态。
6. watchdog 只处理本进程内曾成功运行后的意外退出；显式停止或撤销后不得重启。
7. 生产 `--role=sidecar` 不要求也不接受目标 Android `--device`；它通过 pending intent 获取每个会话的 Android `device_id/session_id/room_id`。只有 `--role=phone` 测试模拟器要求显式 device。
8. bridge hello 只在领取 intent 且 sidecar sign 成功后，以当前会话数据建立或刷新；禁止启动时发送空 `room_id` 与默认 `sidecar-dev-1`。
9. Tauri 是 externalBin 唯一进程 owner；`backend/rtc_bridge/server.py` 的旧 Python watchdog/spawn 必须停用或删除。
10. sidecar 继续负责 RTC/PCM，Tauri 不处理媒体。

当前架构阶段不修改生产代码。实现阶段必须先补 RED 测试，再按本契约改代码。

## 2. 已核验的数据流与陈旧逻辑

### 2.1 现行契约数据流

- Android device principal 调用 `POST /api/v1/voice/session`。`backend/app/api/routes_voice_secured.py::voice_session` 校验 `principal.subject_id == req.device_id`，签发后将 `principal.subject_id + room_id` 写入 pending。
- `backend/app/voice/repositories/pending_sessions.py::enqueue/claim_one` 原样持久化并返回 `session_id + device_id + room_id`；其中 `device_id` 是发起会话的目标 Android device。
- 独立 sidecar Bearer 只用于 `GET /api/v1/voice/session/pending` 和 `POST /api/v1/voice/session/sign`。`CreateSidecarSessionRequest.device_id` 来自 pending intent，`user_id` 固定 `jax-pc-sidecar`。
- `sidecar/rtc.js::pollAndJoin` 从 `intent.device_id` 调用 `fetchSigForDevice()`，再以返回的 `cred.room_id` 进房。生产 PC 启动时不知道具体 Android device。
- OpenAPI 当前没有 `X-Sidecar-Device-Id` 或 sidecar registration endpoint；本任务不得新增。

### 2.2 必须清理的陈旧逻辑

- `sidecar/config.js::parseArgs` 的默认 `device: 'sidecar-dev-1'` 与 `--device` 是旧启动参数，不是 Windows sidecar identity。
- `sidecar/rtc.js::main` 当前在角色分支前对所有角色要求 `ARGS.device`；应改为仅 `--role=phone` 要求显式 device，生产 `sidecar` 反而拒绝 `--device`。
- `sidecar/rtc.js::runSidecar` 当前启动时用 `ARGS.device` 和空 `currentRoom` 调 `bridge.start(hello)`；必须推迟到 pending + sign 成功之后。
- `backend/rtc_bridge/server.py::_SIDECAR_DEVICE/_schedule_sidecar_respawn/_spawn_sidecar` 是第二套旧 watchdog 和硬编码假 device；必须停用/删除，不能与 Tauri 争夺进程所有权。

这次纠正遵循：

- `references/01-standards/spec-as-contract.md`：真实 Spec/OpenAPI、接口和数据形状优先，发现冲突先修规格；明确点名文件、接口、out-of-scope 与 E2E。
- `references/01-standards/context-engineering.md`：二轮的 sidecar identity 推断属于上下文污染，已删除其类型、target、API 和测试传播链。
- `references/01-standards/generated-code-failure-modes.md`：不能因陈旧 guard“看起来需要 device”就生成一套身份系统；跨层数据流与真实接口存在性必须作为阻断门。

## 3. 分层与文件组织

```text
Tauri setup / tray status                    表现与装配
        |
        v
SidecarCredentialService                     credential 生命周期编排
        |
        +--> CredentialProvider trait         OS-bound 存储端口
        |       |
        |       +--> WindowsCredentialStore   一个逻辑credential的A/S/B事务槽 Win32 adapter
        |
        +--> SidecarSupervisor                externalBin 唯一进程 owner
                    |
                    v
          child-only VOICE_SIDECAR_CREDENTIAL
          fixed argv: --role=sidecar
                    |
                    v
Node/Electron sidecar -> pending intent -> sign -> room-scoped bridge hello
```

实现阶段点名文件：

```text
pet-ui/src-tauri/src/
├── main.rs                         只装配 provider/service/supervisor/watchdog
├── credential.rs                   CredentialProvider、SecretString、状态与错误
├── credential_windows.rs           A/S/B事务槽、双层锁与CredWriteW/CredReadW/CredDeleteW adapter
├── sidecar_credential.rs            首次启动、重启、轮换、撤销编排
├── sidecar.rs                       externalBin 校验与单次 credential launch
└── lib.rs                           只导出模块
pet-ui/src-tauri/src/bin/
└── provision_sidecar_credential.rs  一次性无回显 provisioner
pet-ui/src-tauri/tests/
├── sidecar_credential_service.rs    fake provider 状态机、首次启动与注入
└── windows_credential_store.rs      #[cfg(windows)] Win32 集成测试
sidecar/
├── config.js                        role-scoped 参数解析；无生产 sidecar 默认 device
├── rtc.js                           credential fatal、pending/sign、会话后 bridge hello
├── bridge.js                        当前会话 hello 建立/刷新，不缓存假身份
└── test/                            参数、fatal、会话数据流回归测试
backend/rtc_bridge/
├── server.py                        删除/禁用 Python sidecar spawn/watchdog
└── tests/                           唯一 owner 与 session hello 回归测试
```

每个源文件不超过 300 行；`main.rs` 只装配且目标小于 100 行。禁止把 Win32 FFI、轮换编排和 supervisor 进程逻辑堆入 `main.rs` 或 `sidecar.rs`。

## 4. 精确 Rust 契约

### 4.1 常量与 secret 类型

```rust
pub const SIDECAR_CREDENTIAL_TARGET: &str =
    "JaxPet/com.jax.pet/voice-sidecar/v1";
pub const SIDECAR_CREDENTIAL_STAGING_TARGET: &str =
    "JaxPet/com.jax.pet/voice-sidecar/v1/txn/staging";
pub const SIDECAR_CREDENTIAL_BACKUP_TARGET: &str =
    "JaxPet/com.jax.pet/voice-sidecar/v1/txn/backup";
pub const SIDECAR_CREDENTIAL_MUTEX_PREFIX: &str =
    "Global\\JaxPet.VoiceSidecarCredential.v1";
pub const SIDECAR_CREDENTIAL_ENV: &str = "VOICE_SIDECAR_CREDENTIAL";
pub const SIDECAR_CREDENTIAL_MIN_BYTES: usize = 32;
pub const SIDECAR_CREDENTIAL_MAX_BYTES: usize = 512;

pub struct SecretString(zeroize::Zeroizing<String>);

impl SecretString {
    pub fn parse_utf8(bytes: Vec<u8>) -> Result<Self, CredentialError>;
    pub fn expose(&self) -> &str;
}
```

`SecretString::Debug` 固定输出 `SecretString([REDACTED])`；禁止实现 `Display`、`Serialize` 或 `Clone`。`parse_utf8` 拒绝空值、非 UTF-8、NUL、CR/LF，以及 UTF-8 byte length 不在 `32..=512`。

实现时先核验项目实际 Rust 工具链和现有依赖，再将 `zeroize` 与 `windows` 写成可解析精确版本并更新 `Cargo.lock`；不得凭印象编 crate feature 或 Win32 签名。`windows` 只开启 Credential Manager 与错误映射需要的最小 namespaces。

### 4.2 存储端口

```rust
pub trait CredentialProvider: Send + Sync {
    fn status(&self) -> CredentialStatus;
    fn load_active(&self) -> Result<SecretString, CredentialError>;
    fn provision(&self, secret: SecretString) -> Result<(), CredentialError>;
    fn rotate(&self, replacement: SecretString) -> Result<(), CredentialError>;
    fn revoke(&self) -> Result<(), CredentialError>;
}

pub struct WindowsCredentialStore {
    targets: CredentialTargets,
}

pub struct CredentialTargets {
    active: &'static str,
    staging: &'static str,
    backup: &'static str,
}

impl WindowsCredentialStore {
    pub fn sidecar() -> Self;
}
```

生产 `WindowsCredentialStore::sidecar()` 的 active/staging/backup 固定为上述三个常量；只有测试构造器可给整组 target 加同一随机 suffix。不得从前端、argv、环境变量或网络响应决定 target。三个槽只保存同一个逻辑 opaque credential；staging/backup 是事务副本，不是第二 sidecar identity或长期 store。

Win32 adapter 必须提供内部窄接口，公开 `CredentialProvider` 方法不暴露槽：

```rust
enum CredentialSlot { Active, Staging, Backup }

impl WindowsCredentialStore {
    fn with_transaction_lock<T>(
        &self,
        op: impl FnOnce(&Self) -> Result<T, CredentialError>,
    ) -> Result<T, CredentialError>;
    fn recover_locked(&self) -> Result<(), CredentialError>;
    fn read_slot(&self, slot: CredentialSlot) -> Result<Option<SecretString>, CredentialError>;
    fn write_slot(&self, slot: CredentialSlot, secret: &SecretString) -> Result<(), CredentialError>;
    fn delete_slot(&self, slot: CredentialSlot) -> Result<(), CredentialError>;
    fn verify_slot(&self, slot: CredentialSlot, expected: &SecretString) -> Result<(), CredentialError>;
}
```

锁契约：

- 所有 `status/load_active/provision/rotate/revoke` 都必须先取得进程内 mutex，再取得 Windows named mutex；禁止读取绕过恢复。
- mutex 名为 `Global\\JaxPet.VoiceSidecarCredential.v1.<current-user-SID-hash>`；安全描述符只授予当前用户与 SYSTEM。不得记录原始 SID 或完整 mutex 名。
- 等待必须有上限；超时映射 `CredentialBusy`。`WAIT_ABANDONED` 表示锁已取得但上次持有者异常退出，必须先执行 `recover_locked()`。
- provisioner 必须在最终交互用户 token 下加锁和写凭证，不能把管理员/安装器账户的凭证集误当目标用户存储。

Win32 与零化映射：

- 三槽均使用 `CredWriteW(CRED_TYPE_GENERIC, CRED_PERSIST_LOCAL_MACHINE)`；每次写后必须 readback 并常量时间比较。
- `CredReadW` 后只复制一次 blob，立即 `CredFree`；复制 buffer 使用 `Zeroizing<Vec<u8>>` 或等价 RAII 零化，再转入不实现 `Clone/Serialize/Display` 的 `SecretString`。
- active、replacement、staging readback、backup readback 的每份明文均独立零化；比较只借用 byte slice，所有 return/error 路径均由 RAII 清除。
- `CredDeleteW` 的 `ERROR_NOT_FOUND` 视为幂等成功。Windows 错误只映射分类和数值 code，不得包含 blob、用户名、完整 target、SID 或 secret。

恢复状态表（A=active、S=staging、B=backup）：

| S | B | A 与事务槽关系 | `recover_locked()` 动作 |
|---|---|---|---|
| absent | absent | 任意 | 正常；A 缺失由调用方按 ProvisionRequired 处理 |
| present | absent | `A == S` | promote 已提交；删除 S |
| present | absent | `A != S` | 仅 staging 完成；删除 S，保留 A |
| present | absent | A 缺失/不可读 | 不信任 S；保留现场并 recovery failed |
| present | present | `A == S` | commit；按 B -> S 清理 |
| present | present | `A == B` | 未 commit/已 rollback；按 S -> B 清理 |
| present | present | 三者均不等或 A 缺失 | 用 B 覆盖 A并验证；成功后按 S -> B 清理，失败保留现场 |
| absent | present | `A == B` | 删除 B |
| absent | present | `A != B` 或 A 缺失 | 用 B恢复 A并验证；失败保留 B |

任何关系不确定、槽损坏、恢复写入/readback失败均返回 `CredentialRecoveryFailed`，保留所有可读事务槽并禁止 spawn/restart。不得添加 device target、identity JSON、registration fingerprint 或额外 identity provider。

### 4.3 service、状态与 supervisor 窄接口

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialStatus {
    ProvisionRequired,
    Ready,
    Rotating,
    Revoked,
    Error(CredentialErrorCode),
}

pub struct SidecarCredentialService<C: CredentialProvider> {
    credential_provider: C,
    run_policy: RunPolicy,
}

pub struct LaunchCredential {
    credential: SecretString,
}

pub struct RunPolicy {
    pub has_run_successfully: bool,
    pub restart_allowed: bool,
    pub revoked: bool,
}

impl<C: CredentialProvider> SidecarCredentialService<C> {
    pub fn validate_binary(
        &self,
        supervisor: &SidecarSupervisor,
    ) -> Result<(), SidecarError>;

    pub fn prepare_launch(&self) -> Result<LaunchCredential, CredentialError>;

    pub fn start_initial(
        &mut self,
        supervisor: &mut SidecarSupervisor,
    ) -> Result<(), SidecarLaunchError>;

    pub fn restart_after_unexpected_exit(
        &mut self,
        supervisor: &mut SidecarSupervisor,
    ) -> Result<(), SidecarLaunchError>;

    pub fn provision(&self, secret: SecretString) -> Result<(), CredentialError>;

    pub fn rotate_and_restart(
        &mut self,
        replacement: SecretString,
        supervisor: &mut SidecarSupervisor,
    ) -> Result<(), SidecarLaunchError>;

    pub fn revoke_and_stop(
        &mut self,
        supervisor: &mut SidecarSupervisor,
    ) -> Result<(), SidecarLaunchError>;
}

impl SidecarSupervisor {
    pub fn validate_binary(&self) -> Result<(), SidecarError>;
    pub fn spawn_with_credential(
        &mut self,
        launch: LaunchCredential,
    ) -> Result<(), SidecarError>;
}
```

`SidecarSpec.env` 不再长期保存 secret，`SidecarSpec.args` 的生产值只能是非敏感固定项 `--role=sidecar`；不得包含 `--device`。`spawn_with_credential` 调用 `.env(SIDECAR_CREDENTIAL_ENV, launch.credential.expose())` 后创建 child，函数退出即销毁本地明文；不得调用 `std::env::set_var`，不得把 env map 输出到状态、日志或 `Debug`。

首次启动由 Tauri `.setup` 完成装配后同步调用 `start_initial`，顺序严格为：

```text
SidecarSupervisor::validate_binary
-> CredentialProvider::load_active
-> Command fixed argv = [--role=sidecar]
-> child-only credential env
-> spawn
-> has_run_successfully=true, restart_allowed=true
-> spawn watchdog thread
```

任一步失败均保存脱敏状态并让 `.setup` 返回 `Ok(())`，主 Tauri 应用继续运行；不得 `.expect` 终止主应用，也不得让 watchdog 猜测首次启动。

错误优先级：已 Running 时返回 `AlreadyRunning` 且不读 credential；binary/hash 不合法时不读 credential；只有静态校验通过后才读取。`restart_after_unexpected_exit` 必须满足 `has_run_successfully && restart_allowed && !revoked`，并每次重跑 binary/hash 与 credential load。首次失败、显式 stop、rotate 中间态和 revoke 均不得从 `Stopped` 自启。

## 5. sidecar 参数与会话 bridge 契约

### 5.1 role-scoped 参数

`sidecar/config.js::parseArgs` 必须区分“是否显式提供 device”而不是给所有角色默认值：

```typescript
type ParsedArgs =
  | { role: 'sidecar'; device?: never; signUrl: string; bridgeUrl: string; holdS: number }
  | { role: 'phone'; device: string; signUrl: string; bridgeUrl: string; holdS: number };
```

可执行行为：

- `--role=sidecar` 且出现 `--device`：在网络请求和 bridge 连接前以稳定码 `SIDECAR_UNEXPECTED_DEVICE_ARG` 非零 fatal 退出；禁止把它当身份或目标 Android fallback。
- `--role=sidecar` 且 credential 缺失：以 `SIDECAR_CREDENTIAL_MISSING` 非零 fatal 退出。
- `--role=phone` 缺显式非空 `--device`：以 `PHONE_DEVICE_REQUIRED` 非零 fatal 退出。
- 未知 role 或重复冲突参数：以 `SIDECAR_INVALID_ARGS` 非零 fatal 退出。
- fatal 必须通过 Electron 主进程退出路径返回非零码，不得只从 `rtc.js::main` return 后留下空壳。

### 5.2 pending/sign 与 bridge hello

生产 sidecar 流程锁定为：

```text
start sidecar process with credential only
-> poll pending with sidecar Bearer + fresh nonce
-> select intent {session_id, device_id, room_id, expires_at}
-> POST sign body {device_id: intent.device_id, user_id: "jax-pc-sidecar"}
-> verify response room_id == intent.room_id
-> establish or refresh bridge hello:
   {type:"hello", role:"sidecar", sdk_version,
    session_id:intent.session_id,
    device_id:intent.device_id,
    room_id:intent.room_id,
    user_id:"jax-pc-sidecar"}
-> enter returned room
```

`sidecar/rtc.js::runSidecar` 不得在启动时调用 `bridge.start`。`pollAndJoin` 只有在 sign 成功且 room 匹配后，才调用点名接口：

```javascript
bridge.startSession({
  session_id: intent.session_id,
  device_id: intent.device_id,
  room_id: cred.room_id,
  user_id: cred.user_id,
  sdk_version: getSdkVersion(),
});
```

`BridgeClient::startSession(hello)` 的实现要求：

- 首次有效会话建立 localhost WS 并发送 hello；已有 WS 时刷新当前 session，禁止继续使用旧 hello。
- WS 自动重连只能重发仍处于 active 状态的完整 session hello；没有 active session 时不得连接或发送空 hello。
- 退房、peer leave、sign 失败或 session 到期时清除 active hello；下一 intent 必须重新建立/刷新。
- `backend/rtc_bridge/server.py::handler` 必须拒绝缺失/空 `session_id/device_id/room_id` 的 hello，不以 `sidecar-dev-1` 补默认。

该 bridge hello 是当前 Android 会话上下文，不是 Windows sidecar 的持久身份。

## 6. provision、轮换与撤销生命周期

### 6.1 首次 provision

fresh install 安全供给当前未实现，是商业 P0 blocking，不得把下图写成现有能力：

```text
ABSENT
  -> 受保护部署编排产生独立 CSPRNG opaque sidecar credential
  -> 同一值写入后端受保护 secret setting
  -> 安装器内存中的限权匿名 pipe/继承 handle
  -> 最终交互用户 token 下的一次性 provisioner
  -> validate SecretString
  -> 加双层锁并 recover_locked()
  -> active 写入 + readback验证
  -> 确认 staging/backup absent
  -> READY
```

唯一有条件推荐通道必须同时满足：installer/custom action 与 provisioner 是真实发布产物；pipe/handle 只允许目标用户和父安装进程访问；secret 不进入安装包、argv、普通文件、日志、WebView IPC、URL/query、父进程全局环境；helper 不回显，只返回稳定状态码；结束时关闭 handle并零化；后端和本机实际同值；干净机用该凭证成功调用 pending。缺任一证据继续 blocking。

禁止复用 Android pairing/register：现有 OpenAPI `platform` 仅允许 Android，返回的是 device credential。未来 sidecar claim API 必须另立大改任务，先更新 Spec/OpenAPI/数据库，定义一次性 bootstrap token hash、TTL、单次消费、owner授权、审计、撤销与服务端只存 hash；不得塞进当前 Task #16。

### 6.2 轮换

`rotate(replacement)` 在同一双层锁内执行：

1. `recover_locked()`；active 缺失返回 missing，恢复失败直接退出。
2. 写 replacement 到 staging并 readback常量时间验证；失败不得触碰 active。
3. 把旧 active写 backup并 readback验证；失败清理 staging，active保持旧值。
4. 用 replacement覆盖 active并 readback验证。
5. promote成功后按 backup -> staging 清理并返回成功。
6. promote写入或验证失败时用 backup恢复 active并 readback验证；成功后按 staging -> backup清理并返回 rotation failed；恢复失败保留所有可读槽，返回 recovery failed。

服务层在 rotate开始前设置 `restart_allowed=false`，旧 child 在本地事务完成前不得先停止。事务成功后才停止旧 child、完整重读active并启动新 child；健康探测成功后恢复 watchdog。事务不确定时旧 child可继续使用其进程内旧值，但不得再启动新 child。

最坏失败语义：绝不在 active 未验证时返回成功；Credential Manager 连续读写失败或槽关系不确定时，主 Tauri 应用继续，sidecar不首次启动、不重启，事务槽保留待后续受锁恢复。不能承诺在凭证库自身损坏时自动恢复。

后端 `current_hash + next_hash` 最长 10 分钟双值窗口按 §6.3 的内部部署契约实现。本地事务成功只证明 OS store不丢旧值；必须完成服务端双值进入、健康确认、promote 与旧值删除后，才可声称无中断线上轮换。失败不得回退匿名或使用 `TRTC_SECRETKEY`。

### 6.3 后端 `current_hash + next_hash` 轮换窗口

#### 6.3.1 边界与配置来源

这是现有 `sidecarBearer` 的内部认证与部署契约，不改变 Bearer wire format、HTTP path、请求/响应 schema、稳定 HTTP 错误码或主体语义，因此本轮不修改 `docs/commercial-upgrade-SPEC.md` 与 `docs/api/commercial-voice-openapi.yaml`。如果以后新增轮换 API、客户端可见字段、sidecar registration 或数据库记录，必须先走 Spec 大改并更新 OpenAPI；当前实现一律禁止。

后端运行时只持有一个不可变配置快照：

```python
from dataclasses import dataclass
from datetime import datetime

MAX_SIDECAR_ROTATION_WINDOW_SECONDS = 600

@dataclass(frozen=True)
class SidecarCredentialHashSet:
    current_hash: str
    next_hash: str | None = None
    next_enabled_at: datetime | None = None
    next_expires_at: datetime | None = None
    config_revision: str = ""
```

生产 secret 来源固定为部署平台的受保护 secret setting 注入到后端进程环境；仓库 `.env` 只允许本地开发和测试，不作为生产供给证据。`Settings` 的实现输入点名为：

```python
voice_sidecar_credential: str                  # 兼容名；当前值，必需
voice_sidecar_credential_next: str = ""        # replacement，未轮换时为空
voice_sidecar_next_enabled_at: str = ""        # RFC 3339 UTC
voice_sidecar_next_expires_at: str = ""        # RFC 3339 UTC
voice_sidecar_config_revision: str = ""         # 非敏感部署版本，不含 secret/hash
```

`backend/app/main.py::_build_secured_session_router()` 只在进程装配时调用 `CredentialValidator.hash_credential()` 将 current 与 optional next 分别转成 hash，再构造 `SidecarCredentialHashSet`；不得在每个请求读取环境、文件或远端 secret store。生产不得把 secret 或 hash 放入 YAML、SQLite、普通文件、API、WebSocket、WebView IPC、URL/query、argv、日志、诊断包或前端状态。不得复用 `TRTC_SECRETKEY`、owner/device credential，也不得由这些值派生 sidecar credential。

配置原子性规则：未提供 next 时两个时间字段都必须为空；提供 next 时 `next_enabled_at` 与 `next_expires_at` 必须同时存在、为带时区 UTC，且满足 `enabled_at < expires_at` 与 `expires_at - enabled_at <= 600s`。`current_hash` 必须存在；current/next 明文必须不同，两个 hash 必须符合现有 `jax-static-v1$<64 lowercase hex>` 格式。缺失、半配置、时间倒置、超过 600 秒、hash 格式损坏或 current/next 相等均是 `SidecarCredentialConfigError`。生产在 router 装配时抛 `ProductionGateError` 拒绝启动；非生产不得安装匿名旁路，受影响的 sidecar 端点返回现有 `50300 credential_unavailable`。

#### 6.3.2 Python 接口、验证顺序与错误语义

实现只修改/新增以下后端文件，保持表现层只装配、认证逻辑单一职责且每个源文件不超过 300 行：

```text
backend/app/config.py                 声明上述五个 Settings 输入
backend/app/voice/config.py           SidecarCredentialHashSet、时间/窗口配置校验
backend/app/voice/auth.py             双 hash sidecar Bearer 验证
backend/app/main.py                   启动时一次性装配不可变 hash set
backend/tests/unit/test_voice_auth.py 单元矩阵
backend/tests/integration/test_voice_pending_control.py
backend/tests/integration/test_voice_security_routes.py
```

点名接口：

```python
class SidecarCredentialConfigError(ValueError): ...

def build_sidecar_credential_hashes(
    *,
    current_secret: str,
    next_secret: str = "",
    next_enabled_at: str = "",
    next_expires_at: str = "",
    config_revision: str = "",
) -> SidecarCredentialHashSet: ...

class CredentialValidator:
    def __init__(
        self,
        store: VoiceStore,
        owner_credential_hash: str = "",
        sidecar_credentials: SidecarCredentialHashSet | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None: ...

    def verify_sidecar(self, bearer: str) -> CredentialPrincipal: ...
```

`verify_owner()` 与 `verify_device()` 不变。`verify_sidecar()` 顺序固定为：

1. 空 Bearer 直接返回 `AuthError(40101)`，异常与日志不含输入。
2. 在进行任何 credential 比较前，使用安全解析函数校验快照中所有已配置 hash 与时间字段；不得让 `split()`、hex 或时区解析异常穿透为 500。运行时快照损坏返回 `50300`，生产正常启动路径应已提前阻断。
3. 计算一次 bearer 对应的 ASCII candidate digest。若状态是 `rotation_inactive`、`rotation_scheduled` 或 `rotation_expired`，只使用 `hmac.compare_digest()` 对等长 ASCII digest 比较 current；禁止比较未处于 active 窗口的 next。
4. 若服务器 UTC 当前时间位于半开区间 `[next_enabled_at, next_expires_at)`，状态为 `rotation_active`：必须无条件执行 `current_match = hmac.compare_digest(candidate, current_digest)` 与 `next_match = hmac.compare_digest(candidate, next_digest)` 两次等长 ASCII digest 比较；即使 current 已命中也不得短路、提前返回或跳过 next。两次比较完成后才计算 `accepted = current_match | next_match` 并统一分支。
5. `accepted` 为真时返回现有 `CredentialPrincipal("sidecar", "sidecar", "sidecar-credential")`；current 与 next 的 principal、credential_id、nonce/限流 bucket 完全相同。不得把命中槽记录到日志、指标、审计、异常、返回值或任何可观察状态，也不得用 `or` 的短路表达式包裹比较调用。
6. next 未启用、恰好到期或已过期时绝不接受 next；无匹配统一返回 `40101 auth_failed`。过期 next 不使仍有效的 current 失效，但健康状态必须为 `rotation_expired`，部署必须执行回滚或重新开始一个新窗口，不得延长原窗口。
7. 任何配置异常、clock 非 UTC/不可用或比较函数异常均 fail-closed；不得回退单值明文比较、匿名、device/owner credential 或第二验证器。

对外错误码保持稳定：无效、未启用或过期 Bearer 使用 `40101 auth_failed`；配置不可用使用已有 `50300 credential_unavailable`。不得新增能让调用者区分“current 错误”“next 未启用”“next 过期”或命中槽的 HTTP message。内部只允许稳定诊断状态 `rotation_inactive`、`rotation_scheduled`、`rotation_active`、`rotation_expired`、`rotation_config_error`；日志只含 `event/error_code/config_revision`，禁止 secret、hash、Authorization、时间字段原文、候选指纹或 matched slot。

#### 6.3.3 部署、promote、回滚与多实例一致性

无中断轮换必须严格按以下顺序，禁止把本地 `rotate()` 当成服务端 promote：

```text
1. 生成独立 CSPRNG replacement
2. 部署所有后端实例：current=旧值，next=replacement，窗口尚未启用或刚启用
3. 确认所有实例 config_revision 一致，且健康状态均为 rotation_active
4. 经受保护 installer/provisioner 在本机 rotate 到 replacement，并重启/重读 child
5. 用 replacement 完成真实 pending + sign 健康确认；旧值仍可通过
6. 在窗口内原子部署：current=replacement，删除 next 与两个时间字段
7. 确认所有实例 config_revision 一致、rotation_inactive，replacement 可用且旧值返回 40101
8. 销毁部署通道中的旧 secret；本地 backup/staging 已按事务契约清理
```

启用时间必须留出所有实例完成步骤 2 的传播预算；TTL 从 `next_enabled_at` 计算且绝不超过 10 分钟。多实例不得由各实例自行延长 TTL，比较统一使用服务器 UTC；滚动部署期间，只要存在实例尚未接受 next，就不得 provision/rotate 本机。健康检查不得回显 hash，只返回非敏感 `config_revision + rotation_state`；编排器必须确认全部目标实例一致，而不是只抽查一个实例。

进程重启必须从同一部署 revision 重建完整不可变快照；禁止依赖内存 promote。若实例重启后缺 current、只剩 next、时间字段缺半或 revision 不一致，该实例 fail-closed 且不得进入负载均衡。promote 必须是受保护部署配置的一次原子 revision 更新；禁止先删 old current 再逐字段补 next。

失败回滚：

- 步骤 3 前失败：删除 next 与时间字段，保持旧 current；不得触碰本机 active。
- 步骤 4 或 5 失败且窗口仍有效：本机按 A/S/B 契约恢复旧 active，后端保持双值直至确认旧 child 恢复；随后原子删除 next。
- 窗口到期仍未完成健康确认：next 自动不再被验证，本机必须恢复旧 active；后端删除过期 next 后再以新 replacement 与新窗口重试，禁止延长原 `expires_at`。
- promote 后部分实例失败：先从负载均衡摘除 revision 不一致实例；若 replacement 健康，完成剩余实例 promote。若 replacement 不健康，则在原窗口尚有效时原子回滚为 `current=旧值,next=replacement`，恢复本机旧 active；窗口已过期时不得重新接受旧配置中的 next，必须走新的受控 revision。
- 任一阶段不确定：禁止 watchdog 启动新 child、禁止删除最后已确认可用的后端 current、保持主应用可用并以稳定诊断码报告。

成本判断：复用 FastAPI、Pydantic Settings 与现有静态 credential validator，不新增数据库、队列、KMS SDK、管理 API 或 sidecar identity；工作量集中在配置校验、双值比较、部署编排与测试，属于当前 MVP 的最小可行安全增量。

#### 6.3.4 测试矩阵与 EARS 验收

| 层 | 场景 | 关键断言 |
|---|---|---|
| unit/config | only current | 构造 `rotation_inactive`；current 通过，其他值 40101 |
| unit/config | next 半配置/相同值/时间无时区/倒置/TTL 601s/hash 损坏 | `SidecarCredentialConfigError`；生产启动失败，非生产请求 50300 |
| unit/auth | next scheduled | current 通过；next 在 enabled_at 前返回 40101 |
| unit/auth | window boundaries | next 在 `enabled_at` 恰好通过，在 `expires_at` 恰好返回 40101 |
| unit/auth | active window | current 与 next 均通过且 principal 完全相同；第三值 40101 |
| unit/auth | expired window | current 继续通过；next 40101；状态 `rotation_expired` |
| unit/auth + compare spy | constant-time discipline | inactive/scheduled/expired 恰好一次 compare 且只比较 current；active 对 current/next 恰好各一次、顺序固定，即使 current 命中也执行 next compare；两次结果用非短路 OR 合并，不暴露命中槽；恶意 hash 不进入 compare 且不抛 500 |
| integration/pending+sign | current/next 交叉 | 两值在窗口内均能调用现有 pending/sign；响应与 OpenAPI 无变化 |
| integration/nonce+rate limit | 切换 credential | principal/credential_id 不变，换 current/next 不能绕过 nonce 与 sidecar 限流 bucket |
| integration/multi-instance | mixed revision | 未全量接受 next 前部署门禁阻断本机 rotate；不一致实例摘除 |
| integration/restart | same/new revision | 重启重建快照；完整 revision工作，半配置 revision fail-closed |
| deployment/E2E | full rollout | 双值部署、provision、真实健康、promote、旧值401、清理全部有机械证据 |
| deployment/E2E | failure rollback | provision失败、健康失败、窗口到期、部分实例失败均恢复确定旧值或隔离实例，无匿名旁路 |
| static scan | forbidden expansion/leakage | 无 device identity/header/registration/DB/API；无 secret/hash/Authorization 日志 |

EARS：

- WHEN 后端仅配置合法 current，系统必须只接受 current，并保持现有 sidecar principal、nonce、限流与 HTTP 响应契约。
- WHILE UTC 时间位于 `[next_enabled_at,next_expires_at)`，WHEN sidecar 提交任意非空 Bearer，系统必须无条件完成 current 后 next 两次等长 ASCII digest 的 `hmac.compare_digest`，即使 current 已命中也不得短路；两次完成后用非短路 OR 合并，并为任一命中返回完全相同的 principal，且不得记录或返回命中槽。
- WHILE rotation 状态为 inactive、scheduled 或 expired，WHEN sidecar 提交任意非空 Bearer，系统必须只比较 current，禁止比较 next。
- IF next 尚未启用、恰好到期或已过期，系统必须拒绝 next 为 `40101`，不得延长窗口或回退匿名；合法 current 必须继续可用。
- IF current 缺失、双值/时间半配置、TTL 超过 600 秒、hash 损坏、时钟不可判定或 revision 不一致，系统必须 fail-closed；生产拒绝启动，运行时返回 `50300`，不得暴露具体配置原因给客户端。
- WHEN 轮换进入本机 provision 前，部署编排器必须证明所有后端实例已接受同一 next revision；未证明时不得修改本机 active。
- WHEN replacement 完成真实 pending 与 sign 健康确认，部署编排器必须在窗口内把 next 原子 promote 为 current、删除旧值与窗口字段，并证明旧值已返回 `40101`。
- IF 轮换任一步失败或窗口到期，系统必须执行上述确定性回滚或隔离，进程重启后仍从同一受保护 revision 恢复，不得依赖进程内状态。
- ALWAYS 系统不得通过 API/WebView/argv/日志/普通文件传输 secret 或 hash，不得新增 sidecar device identity、`X-Sidecar-Device-Id`、registration API/数据库或第二 credential store。

实现阶段必跑本节单元、集成、部署故障注入，以及 §9 的现有回归。完成定义为：全部实例 revision 一致、窗口不超过 600 秒、new credential 真正通过 pending/sign、promote 后 old credential 机械验证为 40101、过期与配置损坏路径 fail-closed，且 OpenAPI diff 为空。

### 6.4 撤销

顺序锁定为：

```text
restart_allowed=false
-> stop child
-> 部署/控制面使 sidecar credential hash 失效
-> 取得双层锁并 recover_locked()
-> CredDeleteW(active)
-> CredDeleteW(staging)
-> CredDeleteW(backup)
-> REVOKED
```

本地删除失败时状态为 Error 且继续禁止启动；后端 hash 已失效时，即使本地残留也不能通过控制面。重复撤销幂等，重新启用必须显式 provision 新 credential。

## 7. 状态机与错误码

```text
PROVISION_REQUIRED --credential_ok--> READY
READY --setup_start_initial/full_validation--> RUNNING
READY --missing/corrupt/read_denied--> ERROR (main app alive; never-ran)
RUNNING --unexpected_exit--> RESTART_PENDING --full_reread_ok--> RUNNING
RESTART_PENDING --reread/spawn_fail--> ERROR/FUSED
RUNNING --explicit_stop--> STOPPED (restart_allowed=false)
READY/RUNNING --rotate--> ROTATING --success--> RUNNING
ANY --revoke--> REVOKED (restart_allowed=false)
REVOKED --explicit reprovision--> READY
```

| Rust enum | 稳定字符串码 | 含义与动作 |
|---|---|---|
| `CredentialMissing` | `SIDECAR_CREDENTIAL_MISSING` | 未 provision；不 spawn；fresh-install供给证据完成前保持P0 blocking |
| `CredentialCorrupt` | `SIDECAR_CREDENTIAL_CORRUPT` | 非 UTF-8、NUL/换行或长度非法；不 spawn |
| `CredentialReadDenied` | `SIDECAR_CREDENTIAL_READ_DENIED` | 当前 token 无权读取；不 spawn |
| `CredentialBusy` | `SIDECAR_CREDENTIAL_BUSY` | named mutex超时；不绕过锁读取或写入，不 spawn |
| `CredentialRecoveryFailed` | `SIDECAR_CREDENTIAL_RECOVERY_FAILED` | abandoned/incomplete事务无法确定恢复；保留事务槽，不 spawn/restart |
| `CredentialWriteFailed` | `SIDECAR_CREDENTIAL_WRITE_FAILED` | provision/rotate 写入或验证失败；按事务状态恢复/保留旧值 |
| `CredentialDeleteFailed` | `SIDECAR_CREDENTIAL_DELETE_FAILED` | 本地清理失败；保持启动禁用 |
| `CredentialRotationFailed` | `SIDECAR_CREDENTIAL_ROTATION_FAILED` | 重启/健康验证失败；sidecar 停止 |
| `CredentialRevoked` | `SIDECAR_CREDENTIAL_REVOKED` | 明确撤销；watchdog 不重启 |
| `UnsupportedPlatform` | `SIDECAR_CREDENTIAL_UNSUPPORTED_PLATFORM` | 非 Windows 正式构建；不 spawn |

这些是本地诊断码，不新增 HTTP API。前端最多收到 `{state, error_code, retryable}`，不得收到 Win32 原始消息、target 或 secret。

Node 参数错误码为 `SIDECAR_UNEXPECTED_DEVICE_ARG`、`SIDECAR_CREDENTIAL_MISSING`、`PHONE_DEVICE_REQUIRED`、`SIDECAR_INVALID_ARGS`；均不得包含传入值。

## 8. 日志脱敏、最小权限与唯一 owner

允许日志字段：`event`, `credential_state`, `error_code`, `win32_code`, `child_pid`, `restart_count`, `duration_ms`, `session_fingerprint`。禁止字段：credential、blob、Authorization、完整 env、完整 headers、TRTC SecretKey、userSig、nonce。

实现必须：

- 删除或替换任何 `{:?}` 打印 `SidecarSpec`、`Command`、env map、`SecretString` 的代码。
- sidecar `main.js/logger.js` 禁止输出 `process.env` 或 Authorization；fatal 只记稳定码。
- 生产 sidecar 日志不输出完整目标 Android device/session/room；只允许不可逆短指纹。
- Tauri capability 保持最小权限；credential 操作不注册为任意前端可调用 command。若 UI 展示，只暴露 status/retry。
- 不授予管理员权限，不使用 machine-wide secret，不修改 HKLM，不启动 shell。
- 删除/禁用 `BridgeServer._schedule_sidecar_respawn()` 与 `_spawn_sidecar()`；Python bridge 只接受 Tauri 所监督 child 的 localhost 会话连接。

## 9. 测试矩阵

| 层 | 场景 | 关键断言 |
|---|---|---|
| Rust unit / fake provider | setup first start success | 顺序严格 binary/hash -> credential -> child-only env -> spawn；参数仅 `--role=sidecar`；之后才启动 watchdog |
| Rust unit / fake provider | setup missing/corrupt credential | 无 spawn，主 setup 成功，has_run_successfully=false，watchdog 不从 Stopped 拉起 |
| Rust unit | credential empty/short/oversize/non-UTF8/NUL/CRLF | 全部 Corrupt；错误/Debug 不含输入片段 |
| Rust unit / spy process | valid launch | argv 无 secret、无 `--device`、无任意用户 arg；child env 恰有 credential；父 env 无该值 |
| Rust unit | binary missing/hash mismatch | provider 未读取且无 spawn |
| Rust unit | AlreadyRunning | provider 未读取且第二进程未创建 |
| Rust unit | watchdog after prior success | 每次 restart 重跑 binary/hash 并重读 credential；轮换后使用新值 |
| Rust unit | watchdog never ran/stopped/revoked | 不读 provider、不 spawn；撤销后 child exit 也不重启 |
| Rust unit | rotate write/readback failure | staging失败不触碰active；backup失败保留active；promote失败由backup恢复；旧 child不提前停止 |
| Rust unit | recovery state table | 覆盖 S/B存在组合及 A==S/A==B/三者不同/A缺失；动作和清理顺序与表一致 |
| Rust unit | revoke | 先禁watchdog并停止，恢复后删除A/S/B；重复调用幂等 |
| Windows integration | provision/read/replace/delete | 一组随机测试target；active可读回，成功rotate后辅助槽NotFound，revoke后三槽NotFound |
| Windows integration | crash injection | 在stage write、backup write、active write、active verify、delete backup、delete staging后模拟崩溃；新进程load先恢复并得到确定旧值或新值 |
| Windows integration | concurrent processes | 两进程同时rotate、rotate对revoke串行；没有撕裂，锁超时稳定返回busy |
| Windows integration | abandoned mutex | 持锁进程异常退出后下一进程收到WAIT_ABANDONED并先恢复 |
| Windows integration | relogin persistence | 同用户新进程可读且会清理已提交/回滚辅助槽 |
| Windows integration | user/mutex ACL boundary | 另一用户不能读取三槽或获取限权mutex；无法建用户则发布门禁人工验证 |
| Windows integration | slot corruption/read/delete failure | A/S/B分别损坏、read denied、delete失败；关系不确定时recovery failed、槽保留且不spawn |
| Windows integration | zeroization/leakage | fault injection覆盖所有return分支；临时buffer使用RAII zeroize，日志无secret/blob/SID/完整target |
| sidecar Node | sidecar no `--device` + credential | 可进入 pending 轮询；启动前不连接 bridge、不发送 hello |
| sidecar Node | sidecar receives `--device` | 请求 pending/sign 前以 `SIDECAR_UNEXPECTED_DEVICE_ARG` 非零退出，无空壳 |
| sidecar Node | sidecar missing credential | 请求控制面前统一 fatal 非零退出，无空壳 |
| sidecar Node | phone missing/valid device | 缺失 fatal；显式合法 device 才进入 phone 模拟流程 |
| sidecar Node | pending/sign session flow | sign body device 等于 intent Android device；user 固定；room 匹配后才发送含 session/device/room 的 bridge hello |
| sidecar Node | second intent / peer leave / reconnect | hello 刷新为当前会话；清除旧 active hello；无会话时不重发 |
| backend integration | existing secured API | Android principal 建 session并入 pending；sidecar Bearer 领取；sign 使用请求中的目标 Android device；无新增 header/registration |
| bridge integration | invalid/valid hello | 空 session/device/room 拒绝；完整当前会话 hello 建立 PeerVoiceSession；无 `sidecar-dev-1` fallback |
| process ownership | Python bridge disconnect | 不调用 subprocess、不生成第二 sidecar；Tauri prior-success watchdog 是唯一重启者 |
| static scan | overdesign/leakage | 无 device target/identity provider/X-Sidecar-Device-Id/windows_sidecar registration；无 secret/TRTC_SECRETKEY 泄露 |

实现阶段必跑：

```bash
cargo test --manifest-path pet-ui/src-tauri/Cargo.toml
node --test sidecar/test/*.test.js
C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pytest \
  backend/tests/unit/test_voice_auth.py \
  backend/tests/integration/test_voice_security_routes.py \
  backend/tests/integration/test_voice_pending_control.py \
  backend/rtc_bridge/tests -q
```

## 10. Windows 发布 E2E

1. 干净安装且无三个target：启动Tauri，确认主应用可用、sidecar未spawn、watchdog不从Stopped拉起；发布裁决保持fresh-install blocking。
2. 仅在真实受保护installer/provisioner存在时，验证同一secret经限权匿名pipe/继承handle进入最终交互用户Credential Manager；package/argv/env/普通文件/日志/WebView均无secret，后端同值且pending请求成功。缺此发布产物不得用手工写槽替代放行证据。
3. 对active/staging/backup事务执行真机故障注入：stage、backup、promote、active verify、删除backup、删除staging后逐点杀进程；重启后`load_active`先恢复，得到确定旧值或新值，辅助槽按状态表清理。
4. 两进程并发rotate、rotate对revoke、锁超时和WAIT_ABANDONED；确认只允许一个writer、超时返回busy、abandoned先恢复，任何不确定状态不spawn。
5. 分别损坏/拒读/拒删A/S/B；确认recovery failed保留现场，主应用继续，首次启动/watchdog重启均禁止，日志不含secret/blob/SID/完整target。
6. provision合法credential后重启Tauri，确认setup在watchdog前首次spawn；child argv仅固定sidecar参数且无`--device`，child env有credential，父env/日志/argv无credential。
7. 启动后无pending intent：确认不会连接bridge或发送空hello；Android建立session后，pending原样携带Android session/device/room，sign使用该device，room匹配后才发送完整session hello并进房。
8. 第二台Android建立下一session：确认sidecar不重启且bridge hello刷新为新session，不复用旧device/room。生产sidecar若传`--device`应在网络前非零fatal，phone缺device独立fatal。
9. 杀死曾成功运行的child：确认只有Tauri watchdog有限重启且每次加锁恢复并重读active；显式stop/revoke后永不重启，revoke清理三槽。
10. 扫描Tauri、sidecar、bridge、backend、安装产物与诊断日志：无credential、Authorization、userSig、完整会话标识；无第二identity/header/registration系统。

完成定义是本地事务成功流、崩溃/并发/损坏错误流全部通过，且现有OpenAPI无无意变更。fresh-install仍需第2步真实发布证据；后端无中断轮换必须实现并通过 §6.3 的 current/next 契约，二者不能由本地单测替代。

## 11. 迁移、回滚与部署输入

### 迁移

1. fresh-install供给当前未实现，商业P0保持blocking。发布侧如选择条件安装通道，须以CSPRNG生成至少32 bytes独立opaque credential；不能由Tauri/sidecar从`TRTC_SECRETKEY`派生。
2. 同一值必须写入后端受保护secret setting，并只经限权匿名pipe/继承handle交给最终交互用户上下文provisioner；完成第10节干净机证据前不得称为现有能力。
3. 首次写active前取得双层锁并恢复；成功后确认staging/backup不存在。禁止导入`.env`、README示例、普通文件或命令历史旧值。
4. 删除生产sidecar默认device与全角色guard：sidecar禁止device，phone测试角色才要求显式device。
5. 将bridge hello从进程启动移动到pending/sign成功后；hello使用当前intent/session数据，退房后清除。
6. 停用/删除Python bridge的spawn/watchdog与`_SIDECAR_DEVICE`；Tauri setup完成首次start，Tauri watchdog成为唯一重启者。
7. 跑第9节回归与第10节E2E；本地事务、fresh install供给、后端current/next分别独立判定，禁止用其中一项替代另一项。

本任务不新增后端 schema/header/registration API。未来 sidecar claim 属于 Spec 大改；后端无中断 current/next 只按 §6.3 的内部部署契约实现，不改变现有 OpenAPI。

### 回滚

- 代码回滚前先禁止Tauri watchdog、停止child、使后端部署侧credential失效；随后在双层锁内恢复并删除active/staging/backup。
- 不得恢复 Python 第二 watchdog、生产 `sidecar-dev-1`、启动空 hello、`.env` 或 argv secret。
- 如果旧版本仍要求生产 `--device`，不得将其作为安全回滚版本发布；应保持 sidecar fail-closed，直到数据流修复版本可用。

## 12. 明确不做

- 不创建第二个sidecar identity或第二长期credential store；仅允许固定staging/backup作为同一opaque credential的事务槽。
- 不实现 `SidecarDeviceIdentity`、`DeviceIdentityProvider`、`SidecarRegistrationService` 或 registration fingerprint。
- 不新增 `X-Sidecar-Device-Id`、`windows_sidecar` registration endpoint/database record 或隐藏 API。
- 不给生产 sidecar 注入/要求 `--device`，不持久化或猜测目标 Android device。
- 不改变 `/session/pending` 与 `/session/sign` 现有 OpenAPI 语义。
- 不在 Tauri 中处理 RTC/PCM，不改变 sidecar 的媒体职责。
- 不存储、读取或派生 `TRTC_SECRETKEY`。
- 不让前端 JavaScript 直接访问 Credential Manager或 provision secret。
- 不通过命令行、父进程全局环境、运行期 stdin、临时文件传 secret；仅一次性 provisioner可用无回显 stdin/匿名管道。
- 不保留 Tauri 与 Python 双 watchdog，不引入第二套 fallback credential store。
- 不把 `windows`、`zeroize` 或 Credential Manager规定为其他项目/团队固定选型。

## 13. 官方约束依据

- Microsoft `CredWriteW`：创建或修改当前 token 登录会话关联用户凭证集的 credential；同 target/type 写入替换。
- Microsoft `CREDENTIALW`：Generic Credential blob 由应用定义，`CRED_PERSIST_LOCAL_MACHINE` 对同用户本机后续登录会话可见；blob 系统上限 2560 bytes。
- Microsoft `CryptProtectData`：默认绑定同一用户与机器；`CRYPTPROTECT_LOCAL_MACHINE` 允许本机任一用户解密，因此拒绝 machine scope。
- Microsoft DPAPI-NG：适合描述符/跨主体场景；本单用户单机 MVP 不需要其额外复杂度。
