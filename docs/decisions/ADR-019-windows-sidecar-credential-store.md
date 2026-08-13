# ADR-019: Windows sidecar credential 使用当前用户范围 Credential Manager

## Status: Proposed (2026-08-08)

在主理人完成契约门禁前不得视为 Accepted。

## Background

生产 `--role=sidecar` 通过受保护的 `/api/v1/voice/session/pending` 枚举由 Android 建立的待处理会话，再把 intent 的目标 Android `device_id` 传给 `/api/v1/voice/session/sign`，以返回的 `room_id` 进房。`sidecar/rtc.js` 已明确“PC 不知道手机 device_id”；因此生产 sidecar 启动时不应知道、持久化或接收某台 Android device。

当前 `sidecar/config.js` 的默认 `sidecar-dev-1`、`rtc.js` 对所有角色执行的 `ARGS.device` 启动 guard，以及启动时用该值发送空房间 bridge hello，均是迁移到 pending 轮询后未同步清理的陈旧逻辑，不构成 Windows sidecar identity。只有 `--role=phone` 测试模拟器需要显式 `--device=<target>`。

真正需要 OS-bound 持久化的是独立 opaque `VOICE_SIDECAR_CREDENTIAL`。它不得与 Android device credential 复用，不得持有或派生 `TRTC_SECRETKEY`。当前 Tauri setup 只注册 supervisor/watchdog、没有执行首次 `start`；watchdog 又跳过 `Stopped`，所以还必须明确首次启动与重启职责。

`backend/rtc_bridge/server.py` 仍有第二套 Python sidecar watchdog 和硬编码 `sidecar-dev-1`。它是旧生命周期逻辑，不是身份来源；生产 sidecar 进程生命周期只能由 Tauri `externalBin` supervisor 拥有。

## Decision

本项目选用 Windows Credential Manager Generic Credential 保存一个逻辑 opaque sidecar credential。为补偿 Credential Manager 不提供跨 target 事务，允许三个固定事务槽；staging/backup 只是同一 credential 的短期事务副本，不是第二 sidecar identity、第二长期 credential store 或后端注册记录：

- active：`JaxPet/com.jax.pet/voice-sidecar/v1`。
- staging：`JaxPet/com.jax.pet/voice-sidecar/v1/txn/staging`。
- backup：`JaxPet/com.jax.pet/voice-sidecar/v1/txn/backup`。
- 三个 target 均固定，不接受前端、argv、环境变量或网络响应覆盖；`Type = CRED_TYPE_GENERIC`，`Persist = CRED_PERSIST_LOCAL_MACHINE`。该 persist 表示同一 Windows 用户在本机后续登录会话可见，不表示机器范围共享。
- 每个 blob 只允许保存同一形状的 UTF-8 `VOICE_SIDECAR_CREDENTIAL`；不得保存 `device_id`、`registration_id`、事务 metadata 或任何 sidecar identity。
- 通过 Win32 `CredWriteW`、`CredReadW`、`CredDeleteW`、`CredFree` 访问。active/staging/backup 及 readback 的每份明文都进入独立零化包装；临时 byte buffer 也必须 RAII 零化，所有错误路径不得留下普通 `Vec<u8>` 明文。
- 所有 `status/load_active/provision/rotate/revoke` 先获取进程内 mutex，再获取当前用户与 SYSTEM 限权的 Windows named mutex：`Global\\JaxPet.VoiceSidecarCredential.v1.<current-user-SID-hash>`。锁等待有上限；超时返回 `SIDECAR_CREDENTIAL_BUSY`。`WAIT_ABANDONED` 表示已取得锁，但必须先执行 `recover_locked()`。
- fresh install 的安全供给当前未实现，是商业 P0 blocking。唯一有条件推荐方案是受保护安装/部署通道把同一 CSPRNG opaque secret 写入后端受保护配置，并通过只允许目标用户与父安装进程访问的匿名 pipe/继承 handle，交给运行在最终交互用户上下文的一次性 provisioner；不得经过 argv、普通文件、日志、WebView IPC、URL、父进程全局环境或安装包内嵌 secret。没有 provisioner、installer/custom action、pipe ACL/handle 证据和干净机 E2E 前不得放行。
- 不复用 Android pairing/register。未来若新增 sidecar claim API，必须先按 Spec 大改流程另立任务，更新 Spec/OpenAPI/数据模型/一次性 token、TTL、单次消费、owner 授权、审计、撤销和服务端 hash 语义。
- Tauri setup 首次启动顺序固定为：externalBin 存在与 SHA-256 验证 -> 加锁并 `recover_locked()` -> `CredentialProvider::load_active()` -> child-only `VOICE_SIDECAR_CREDENTIAL` -> 以固定参数 `--role=sidecar` spawn -> 标记本进程曾成功运行 -> 启动 watchdog。缺失、损坏、忙、恢复失败或无权读取时不 spawn，但 Tauri 主应用继续运行并只暴露脱敏状态。
- 生产 sidecar 启动参数固定为 `--role=sidecar`，不得要求或接受目标 Android `--device`。`--role=phone` 测试模拟器才必须显式提供 `--device`。
- watchdog 只在本进程内 sidecar 曾成功运行后处理意外退出；每次重启重新验证 binary/hash，加锁恢复并重读 active。首次启动失败、从未成功运行、显式停止、事务不确定或撤销后禁止从 `Stopped` 自启。
- 撤销先设置 `restart_allowed=false` 并停止 child，再使后端部署侧 credential hash 失效；随后在同一锁内恢复未决事务并删除 active/staging/backup。任一删除失败均继续禁止重启。
- 后端无中断轮换所需的 `current_hash + next_hash` 最长 10 分钟窗口是独立 OPEN 项；本地事务成功只证明 OS store 不丢旧值，不代表新值已被服务端接受。
- `Debug`、`Display`、错误、事件和日志不得包含 secret、CredentialBlob、Authorization、完整 target、SID 或环境映射。
- `windows` crate 是本项目实现选择，必须在实现时按现有 Rust 工具链锁定精确可解析版本；不构成团队固定规范。

生产会话数据流固定为：

```text
Android device principal
  -> POST /api/v1/voice/session
  -> pending {session_id, target_android_device_id, room_id}

Windows sidecar bearer
  -> GET /api/v1/voice/session/pending
  -> POST /api/v1/voice/session/sign
       body.device_id = pending.target_android_device_id
       body.user_id = jax-pc-sidecar
  -> verify returned room_id matches claimed intent
  -> establish/refresh bridge hello for this session only
       {session_id, target_android_device_id, room_id, user_id}
  -> enter room
```

bridge hello 不得在 sidecar 启动时用空房间和假 device 发送；必须在领取 intent 并取得当前会话的 `session_id/device_id/room_id` 后建立或刷新。

### Credential transaction and crash recovery

`rotate(replacement)` 必须在同一双层锁内执行：先 `recover_locked()`；确认 active 旧值可读；写并验证 staging；把旧 active 写并验证 backup；用 replacement 覆盖 active 并验证。active 验证成功后按 backup -> staging 顺序清理。promote 写入或验证失败时用 backup 恢复 active并验证；恢复成功后按 staging -> backup 清理并返回 rotation failed，恢复失败则保留所有可读槽、返回 recovery failed并禁止 spawn/restart。旧 child 在本地事务完成前不得先停止。

`recover_locked()` 只依据三个槽的存在性与常量时间相等关系恢复：

| staging | backup | active 关系 | 恢复动作 |
|---|---|---|---|
| absent | absent | 任意 | 正常；active 缺失由调用方按 provision required 处理 |
| present | absent | `A == S` | promote 已提交且 backup 已清；删除 staging |
| present | absent | `A != S` | 仅 staging 完成；删除 staging，保留 active |
| present | absent | active 缺失/不可读 | 不信任 staging；保留现场并 fail-closed |
| present | present | `A == S` | commit；按 backup -> staging 清理 |
| present | present | `A == B` | 未 commit 或已 rollback；按 staging -> backup 清理 |
| present | present | 三者均不等或 active 缺失 | 用 backup 恢复 active并验证；成功后按 staging -> backup 清理，失败保留现场并 fail-closed |
| absent | present | `A == B` | 清理 backup |
| absent | present | `A != B` 或 active 缺失 | 用 backup 恢复 active并验证；失败保留 backup并 fail-closed |

最坏失败语义：绝不在 active 未 readback 验证时返回成功；若 Windows 连续拒绝读写或三个槽关系无法确定，`status/load_active` 返回 `SIDECAR_CREDENTIAL_RECOVERY_FAILED`，Tauri 主应用继续，sidecar 不首次启动、不重启，事务槽保留供后续受锁恢复。系统不能承诺在 Credential Manager 自身损坏时自动保住可用值。

详细 trait、状态机、错误码、文件边界和测试矩阵由 `docs/windows-sidecar-credential-contract.md` 锁定。

## Options considered

评分：5 为最好；安全与发布可行性为硬门，其余用于 MVP 取舍。

| 方案 | 当前用户 OS-bound | 无普通文件密文 | 轮换/删除 API | 实现与测试成本 | 总分 / 20 | 裁决 |
|---|---:|---:|---:|---:|---:|---|
| Windows Credential Manager Generic Credential | 5 | 5 | 5 | 4 | 19 | 采用 |
| DPAPI current-user + 应用数据文件保存密文 blob | 5 | 1 | 3 | 4 | 13 | 拒绝：仍引入文件、ACL、原子写、损坏恢复和备份残留面 |
| DPAPI-NG `NCryptProtectSecret` + 应用数据文件 | 5 | 1 | 3 | 2 | 11 | 拒绝：单用户单机没有跨主体收益，仍需文件容器且 API 更复杂 |
| Windows Credential Locker `PasswordVault` | 4 | 5 | 4 | 2 | 15 | 拒绝：WinRT/打包身份为当前桌面交付增加不确定性 |

不采用 `CRYPTPROTECT_LOCAL_MACHINE`，因为同机其他用户可解密。也不采用环境变量持久化、注册表明文、自制加密格式或第二套 fallback store。

## Consequences

正面后果：

- 只引入解决 P0 所需的一个逻辑 OS-bound secret，不发明 Spec/OpenAPI 未要求的 Windows sidecar 身份系统。
- active 被覆盖前，replacement 和旧值分别经过 staging/backup readback 验证；已定义中途崩溃的确定性恢复。
- credential 不产生普通文件密文，也不进入 argv、父进程全局环境或前端。
- setup 承担首次启动，watchdog 只承担 prior-success 后的意外退出恢复。
- 生产 sidecar 与具体 Android device 解耦，可持续领取不同设备建立的 pending intent。

负面后果：

- Credential Manager 没有跨 target 原子事务；三个槽、双层锁和恢复状态增加 Windows adapter 与真机故障注入成本。
- 当前同一 Windows 用户权限下的恶意进程原则上仍可能读取 Generic Credential；本方案不声称抵御已完全控制该用户会话的攻击者。
- 凭证库损坏、named mutex ACL 错误或用户配置文件迁移时可能无法自动恢复，必须保持 fail-closed 并重新 provision。
- fresh-install 安装通道和后端双值窗口都尚未实现；它们分别阻断首次供给和无中断轮换的商业放行。
- sidecar/bridge 中陈旧的启动 device guard、启动 hello 和 Python watchdog 必须在实现阶段清理，否则数据流仍会沉默出错。

## Migration and rollback

迁移不读取或复制 `sidecar-dev-1`，不导入旧 `.env`、普通文件或注册表明文。升级后 active 缺失或损坏时状态为 `PROVISION_REQUIRED`/`ERROR`，sidecar 不启动，主应用继续运行；升级发现辅助槽时必须先加锁恢复，不能直接清空。

fresh install 当前保持商业 P0 blocking。只有受保护 installer/custom action、运行在最终交互用户上下文的 provisioner、限权匿名 pipe/继承 handle、后端同值受保护配置和干净机 E2E 全部有证据后，才可把该条件通道升级为 Accepted 能力。

实现阶段必须删除生产 `--role=sidecar` 的 device 默认值依赖和全角色 guard；保留 `--role=phone` 显式 device 校验。必须停用/删除 `backend/rtc_bridge/server.py` 的 sidecar spawn/watchdog，使 Tauri 成为唯一进程 owner。

回滚前先禁止 watchdog、停止 child、使后端部署侧 sidecar credential 失效；随后在同一事务锁内执行恢复并删除 active/staging/backup。禁止回滚到 `.env`、硬编码 credential 或 Python 第二 watchdog 后仍宣称满足本 ADR。

## Explicitly not doing

- 不创建第二个 sidecar identity、`SidecarDeviceIdentity`、`DeviceIdentityProvider` 或长期 credential store；staging/backup 仅是同一 opaque credential 的固定事务槽。
- 不新增 `X-Sidecar-Device-Id`、`windows_sidecar` registration API/数据库表或隐藏端点。
- 不给生产 `--role=sidecar` 注入、要求或持久化目标 Android `--device`。
- 不改变 OpenAPI 中 `PendingSessionIntent.device_id` 与 `CreateSidecarSessionRequest.device_id` 的目标 Android device 语义。
- 不在 Tauri/Rust 中实现 RTC、TRTC SDK、PCM 或音频队列。
- 不让 Node/Electron sidecar 读取 Credential Manager；它只消费单次 child env。
- 不把 `TRTC_SECRETKEY` 放进 Tauri、sidecar、Credential Manager 或 child env。
- 不让前端读取、展示、导出、provision 或回显 credential。
- 不保留 Python 与 Tauri 两个 sidecar 进程 owner。
- 不把具体 Rust 库或 Windows 存储方案提升为团队级固定规范。

## Design-discipline references

- `references/01-standards/spec-as-contract.md`：以现有 Spec/OpenAPI 和真实数据形状为契约；原地删除被代码证据证伪的 sidecar identity 扩展，点名文件/接口、out-of-scope 与 E2E。
- `references/01-standards/context-engineering.md`：清除二轮错误 identity 结论造成的上下文污染，只保留已核验的数据流、改动文件和验证命令。
- `references/01-standards/generated-code-failure-modes.md`：把“启动 guard 看起来存在即推断为身份需求”视为沉默逻辑错误；通过跨层数据流测试和接口存在性核验阻断复发。

## Related ADRs

ADR-014、ADR-017。
