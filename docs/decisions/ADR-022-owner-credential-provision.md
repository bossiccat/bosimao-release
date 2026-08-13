# ADR-022: owner credential 下发链路（CM 单源 + 阶段 B .env 引导桥）

## Status: Accepted (2026-08-13)

项目总监（大湾区靓仔）裁决 Accepted。门禁补充实施红线：

- **provisioner 写 `.env` 必须用绝对路径定位**（相对可执行文件所在目录或 PROJECT_ROOT，不得依赖进程工作目录），否则在服务化/脚本编排下会写错位置。
- **secret 零回显**：provisioner 全程不回显 secret、不写任何日志文件；RAII Zeroize 覆盖生成 buffer。
- **幂等更新 `.env` 不得破坏现有其它行**：只增改 `VOICE_OWNER_CREDENTIAL=` 一行，保留注释与其余键值原样。
- **`get_owner_credential` fail-closed**：CM 读失败/缺失必须返回 `Err`（不得返回空串/降级），确保前端走「禁用开关」路径而非「空 Bearer」。

## Background

阶段 B「隐私开关 AC-17」已实现（`backend/app/api/routes_voice_privacy.py` 的 `PATCH /api/v1/privacy/{setting}` 为 owner-only），QA 验收 APPROVE，但留下一个上线前必须关闭的依赖缺口：owner Bearer 前端拿不到。

现状（代码已核验，非二次侦察结论）：

- 后端校验链路已就绪且 fail-closed：`backend/app/main.py:80-83` 用 `CredentialValidator.hash_credential(settings.voice_owner_credential)` 计算 owner hash（空串 → hash 空串）；`backend/app/voice/auth.py:94-98` 的 `verify_owner` 对空 hash 恒拒绝（`_verify_static` 空 hash 直接返回 False）；`backend/app/config.py:213` 的 `voice_owner_credential` 默认空，从 `.env` 读取。
- 前端已就绪且 fail-closed：`pet-ui/src/lib/privacy.ts:63-69` 的 `getOwnerToken()` 调 `invoke<string>("get_owner_credential")`，异常返回 `null` → 请求不带 Authorization → 后端 40101 → 开关禁用。
- 缺失项唯一在 Rust：`pet-ui/src-tauri/src/main.rs:56-61` 的 `invoke_handler` 只有 `set_ignore_cursor_events / get_sidecar_status / install_trusted_ca / is_ca_install_required` 四条命令，没有 `get_owner_credential`。
- sidecar 已有完整 CM 三槽机制可复用：`pet-ui/src-tauri/src/credential_windows.rs`（`WindowsCredentialStore` → `TransactionalCredentialStore<Win32CredentialBackend, Win32TransactionLock>`），target 前缀 `JaxPet/com.jax.pet/voice-sidecar/v1`；`pet-ui/src-tauri/src/bin/provision_sidecar_credential.rs` 是一次性无回显 provisioner（stdin 读 secret 写 CM）。ADR-019 锁定该模式。

## Decision

### D1. 威胁模型校准（先定安全等级，再定存储）

后端 uvicorn 以 `--host 127.0.0.1 --port 8000` 启动（`scripts/start-all.ps1:48`、`scripts/jax-services.ps1:169`），**只绑定本机回环**。Android 采集端不直连后端，而是经 `PublicRelay(wss) → relay_client → ws://127.0.0.1:8000/ws/voice` 中转（`start-all.ps1:5,66-67`）。故 owner credential 的防御面是**同机同一 Windows 用户会话下的其它本地进程**，不是远程/LAN 攻击者。

这与 sidecar credential（ADR-019 明言「不声称抵御已完全控制该用户会话的攻击者」）同级。因此 owner credential 的安全等级定为「同用户本地进程隔离」，**不是**网络保密级秘密。

该等级的推论：`.env` 明文落盘不构成实质回归——`.env` 已明文持有 `TRTC_SECRETKEY`（比 owner credential 更敏感，`config.py:208`）与 `DEEPSEEK_API_KEY`，且 `.env` 已 gitignore（`.gitignore:12`）。**真正必须杜绝的失败模式是「打包分发全员同值」**（固定值进安装包 → 所有装机共享同一 admin bearer），而非「明文落盘」。

### D2. 存储裁决：CM 单源 + 阶段 B 用 .env 作后端引导桥

- **canonical 存储 = Windows Credential Manager**，single active 槽，`Type=CRED_TYPE_GENERIC`，`Persist=CRED_PERSIST_LOCAL_MACHINE`（当前用户范围，含义同 ADR-019）。复用现有 `WindowsCredentialStore` / `TransactionalCredentialStore` 全套读/写/事务/恢复/锁代码，**不为 owner 新写一套单槽 store**（owner 在 MVP 内不可轮换，三槽机制对静态凭证是零成本的已有实现，不构成过度设计）。
- **前端（Tauri）读 CM**：新增 `get_owner_credential` 命令，`WindowsCredentialStore::owner().load_active()`，成功返回 `String`，任何失败返回 `Err`（fail-closed）。
- **后端（Python）阶段 B 读 `.env` 引导桥**：provisioner 在首启时把同一 secret 写入 `.env` 的 `VOICE_OWNER_CREDENTIAL=`（幂等：key 已存在则跳过）。后端零改动——启动脚本 `Load-Env`（`start-all.ps1:16-27`、`jax-services.ps1:68-78`）已把 `.env` 注入进程环境，pydantic-settings 读 `Settings.voice_owner_credential`。**不引入 keyring/win32cred 读 CM**（见 D7 与 Options considered）。
- **阶段 D/G 演进（终态）**：Rust 托管后端进程后，按 sidecar 的同一模式「Rust 读 CM → 注入 child env `VOICE_OWNER_CREDENTIAL`」替换 `.env` 引导桥，删除明文落盘。引导桥是「同一注入模式在『谁注入 env』上的阶段差异」，不是第二套机制。

### D3. 语义分离（target 命名，硬约束）

owner 与 sidecar 是两个独立主体：sidecar 是「桌宠 → 本地 sidecar 进程」的配对，owner 是「桌宠 → 后端」的管理员身份。**绝不复用 sidecar target**，owner 使用独立前缀：

| 槽 | target |
|---|---|
| active | `JaxPet/com.jax.pet/voice-owner/v1` |
| staging | `JaxPet/com.jax.pet/voice-owner/v1/txn/staging` |
| backup | `JaxPet/com.jax.pet/voice-owner/v1/txn/backup` |
| named mutex 前缀 | `Global\JaxPet.VoiceOwnerCredential.v1`（SID 哈希拼接，逻辑同 sidecar） |

owner credential 值复用 `SecretString::parse_utf8` 的 32–512 bytes、UTF-8、无 CR/LF/NUL 校验（`credential.rs:26-42`）。后端 env key 复用现成 `VOICE_OWNER_CREDENTIAL`（`config.py:213`）。

### D4. 首启 provision 流程：本机随机生成（非安装时生成）

owner 是「桌宠 UI 自己的身份」，由 UI/本机在**首启自生成**即正确，不需要 sidecar 那套「受保护安装通道 + 匿名 pipe + 继承 handle」的 P0 重型供给（ADR-019 仍 blocking 的那条）。

- **由谁生成**：一次性 Rust provisioner `provision_owner_credential`（`pet-ui/src-tauri/src/bin/`），CSPRNG 32 bytes → hex（64 chars，满足 32–512 bytes 且无 CR/LF/NUL）。与 sidecar provisioner 不同：**不自 stdin 读 secret，而是自生成**（owner 无外部投递方）。
- **存哪**：CM active（`WindowsCredentialStore::owner().provision`，readback 验证）→ 成功后写 `.env` `VOICE_OWNER_CREDENTIAL=<secret>`（幂等）。两处值必须逐字节相同。
- **后端如何首次读到**：provision 完成后，下一次后端启动由 `Load-Env` 注入进程环境 → pydantic-settings 读 `voice_owner_credential` → `main.py:81` 计算 hash。
- **幂等与 fail-closed**：provisioner 先 `status()`，`Ready` 则直接 exit 0；否则 `revoke()`（幂等清三槽，`ERROR_NOT_FOUND` 视为成功）→ `provision`。任一步失败返回稳定非零退出码，不部分成功、不回显、RAII 零化。provision 未执行或失败 → CM 空 + `.env` 空 → 前端命令 Err + 后端 hash 空 → 两端独立 fail-closed。

### D5. 完整数据流（生成 → 存储 → 读取 → 校验）

```text
[provision_owner_credential.exe (首启, 幂等)]
  1. status() Ready? → exit 0
  2. CSPRNG 32B → hex → SecretString::parse_utf8
  3. WindowsCredentialStore::owner().provision(secret)
       └ CredWriteW(TargetName="JaxPet/com.jax.pet/voice-owner/v1",
                    Type=GENERIC, Persist=LOCAL_MACHINE) + readback
  4. 写 .env: VOICE_OWNER_CREDENTIAL=<secret>  (key 已存在则跳过)

[前端读取 — 桌宠 UI]
  privacy.ts::getOwnerToken()
    → invoke("get_owner_credential")
    → main.rs::get_owner_credential
    → WindowsCredentialStore::owner().load_active() → String
    → Authorization: Bearer <secret>   (仅进本次请求头, 不落前端状态)
  失败 → null → 无 Authorization → 后端 40101 (fail-closed)

[后端读取 — 启动时]
  start-all.ps1/jax-services.ps1 Load-Env 注入 .env
    → config.py::Settings.voice_owner_credential
    → main.py::_build_secured_session_router (L80-83)
        hash_credential(voice_owner_credential)  (空 → hash 空串)

[校验 — 每次 PATCH]
  routes_voice_privacy.py::owner_or_error (L68-78)
    → auth.py::verify_owner(bearer) (L94-98)
        _verify_static(bearer, owner_hash)  空 hash → False → 40101
    → CredentialPrincipal(PRINCIPAL_OWNER, "owner", ...)
    → nonce 消费 → 限流 → privacy.set() (失败回滚 UI, ADR-021 D2)
```

### D6. fail-closed 语义（违反即退回）

- 任何一端读不到 credential（CM 缺失/损坏/拒绝、.env 缺 key、invoke 失败）→ **禁用开关**，绝不降级为可写、绝不匿名放行、绝不 verify=false。
- 后端已 fail-closed（空 hash 拒绝所有）；前端已 fail-closed（null → 40101 → UI 回滚）。本 ADR 只补 Rust 的读与生成两条，不触碰任何放行逻辑。
- 不新增任何「绕过校验」的路径；owner 与 sidecar 语义/target 严格分离。

### D7. 明确拒绝 keyring/win32cred 读 CM

后端 Python 用 keyring/win32cred 直读 CM 被拒绝：它引入新依赖（keyring + pywin32）与跨语言 CM 读写兼容面（target/UserName/Persist 三处必须逐字段对齐，`WinVaultKeyring` 以 service/username 映射 target/UserName），且与 sidecar 已确立的「Rust 读 CM → 注入 child env」模式形成第二套并行机制。阶段 D/G 用 child env 注入统一收口，届时 `.env` 引导桥删除。

## Options considered

评分 5 最好；「打包全员同值」与「阶段 B 可闭环」为硬门，其余用于 MVP 取舍。

| 方案 | 存储 | 明文落盘 | 打包全员同值 | 依赖 Rust 启动后端 | 新增 Python 依赖 | 阶段 B 可闭环 | 裁决 |
|---|---|---:|---:|---:|---:|---:|---|
| A. CM + Python keyring 读 | CM | 无 | 无 | 否 | 是(keyring+pywin32) | 是(后端需改) | 拒绝：跨语言耦合 + 新依赖 + 与 sidecar child-env 模式不一致 |
| B. 纯 `.env` 固定值 | .env | 有 | **是(致命)** | 否 | 否 | 是 | 拒绝：全员同值 |
| C. Rust 启动后端 env 注入 | CM | 无 | 无 | 是 | 否 | 否(阶段 D/G) | 终态（非本阶段） |
| **D. CM(前端) + .env 本机随机(后端引导桥)** | CM + .env | 有(仅引导桥) | 无(本机生成) | 否 | 否 | **是** | **采用（阶段 B）** |

## Consequences

正面后果：

- 阶段 B 立即闭环：owner Bearer 可 provision、可被前端读、可被后端校验，`PATCH /api/v1/privacy` 的 owner 门禁在真实环境下可验收，且不依赖阶段 D/G 的「Rust 启动后端」。
- 每机唯一（CSPRNG 首启生成），消灭「打包分发全员同值」这一致命失败模式。
- 复用 ADR-019 的 CM 事务/恢复/锁全套已验证代码，owner 不另起炉灶；语义与 target 与 sidecar 严格分离。
- 后端零改动、零新依赖；前端仅补 Rust 命令与 provisioner。

负面后果：

- `.env` 明文持有 owner secret（阶段 B 引导桥）。经 D1 校准为可接受：后端仅绑定回环、`.env` 已持有更敏感的 `TRTC_SECRETKEY`、owner 不可轮换所以引导桥不产生漂移。终态由阶段 D/G 的 child env 注入消除。
- 存在 CM 与 `.env` 两份物理副本。因 owner 在 MVP 内不可轮换/撤销，二者不会漂移；provisioner 幂等写保证「有值即不变」。
- provision 与后端启动有顺序约束：`.env` 必须在后端进程启动前写好；若后端已运行，需重启后端才加载新 `VOICE_OWNER_CREDENTIAL`（脚本按序编排 provisioner 在 backend 之前）。

## Implementation checklist

### in-scope（阶段 B 必做，可验证）

| # | 改动点 | 文件 / 函数 | 说明 |
|---|---|---|---|
| 1 | owner 常量 | `pet-ui/src-tauri/src/credential.rs` | 新增 `OWNER_CREDENTIAL_TARGET = "JaxPet/com.jax.pet/voice-owner/v1"`、`OWNER_CREDENTIAL_ENV = "VOICE_OWNER_CREDENTIAL"`；owner 复用 `SIDECAR_CREDENTIAL_MIN/MAX_BYTES`（32/512） |
| 2 | owner 三槽 + 锁常量 | `pet-ui/src-tauri/src/credential_windows.rs` | 新增 `OWNER_CREDENTIAL_STAGING/BACKUP_TARGET`、`OWNER_CREDENTIAL_MUTEX_PREFIX = "Global\JaxPet.VoiceOwnerCredential.v1"`；将 `build_inner` 参数化为接受「active/staging/backup target + mutex 前缀」，`sidecar()` 与 `owner()` 各传自己那套 |
| 3 | owner 工厂 | `pet-ui/src-tauri/src/credential_windows.rs::WindowsCredentialStore::owner()` | 与 `sidecar()` 并列，构建 owner target 的 `TransactionalCredentialStore`（含 `#[cfg(not(windows))]` 空实现） |
| 4 | 读命令 | `pet-ui/src-tauri/src/main.rs::get_owner_credential` | `#[tauri::command] fn get_owner_credential() -> Result<String, String>`：`WindowsCredentialStore::owner().load_active()` 成功返回 `expose().to_string()`，失败 `Err`；注册进 `invoke_handler`（`main.rs:56-61`） |
| 5 | provisioner | `pet-ui/src-tauri/src/bin/provision_owner_credential.rs` + `Cargo.toml` 加 `[[bin]]` | 自生成 CSPRNG 32B→hex；`status()==Ready` 幂等退出；否则 `revoke()`→`provision()`→写 `.env` `VOICE_OWNER_CREDENTIAL=`（幂等追加/更新，保持原文件其余行不变）；稳定非零退出码；Zeroize 全程 |
| 6 | 脚本编排 | `scripts/start-all.ps1`、`scripts/jax-services.ps1` | 在启动 backend（step 2 / `Start-BackendService`）之前调用 provisioner（`Start-Process` + 检查退出码，非零则中止并告警） |
| 7 | 模板文档 | `.env.example` | 追加 `VOICE_OWNER_CREDENTIAL=`（空模板 + 注释「首启由 provision_owner_credential 生成，禁止手工填固定值」）；**绝不提交真实值** |

### out-of-scope（阶段 D/G 或后续迭代，明确不做）

1. Rust 托管后端进程 + child env 注入（替换 `.env` 引导桥，删明文）——阶段 D/G。
2. owner credential 轮换/撤销（MVP 内 owner 不可变，无 rotation 需求）。
3. Python keyring/win32cred 直读 CM（见 D7，拒绝）。
4. 后端暴露非回环地址后的 owner 网络级加固（当前仅回环，见 D1）。
5. 多用户 / 非交互服务账户下的 owner 隔离（当前单交互用户模型）。

## Design-discipline references

- `spec-as-contract.md`：以已核验的代码事实为契约（后端 bind 回环、.env 已持 TRTC_SECRETKEY、sidecar CM 三槽），点名文件/函数、out-of-scope、E2E；不臆造「后端需要网络级 owner secret」的结论。
- `context-engineering.md`：清除「owner 必须与 sidecar 同级保密、必须走安装通道」的过度推断，只保留本机回环 + 同用户本地进程的威胁事实。
- `generated-code-failure-modes.md`：不把「privacy.ts 已调用 get_owner_credential」推断为「Rust 命令已实现」；以 invoke_handler 注册清单为准，缺失即 fail-closed 缺口。

## Related ADRs

ADR-014（fail-closed 与四主体）、ADR-018（本地最小隐私数据）、ADR-019（sidecar CM 三槽，本 ADR 复用其存储/事务/锁模式）、ADR-020（TLS 四端，回环承载）、ADR-021（隐私开关 AC-17，本 ADR 关闭其 owner 下发缺口）。
