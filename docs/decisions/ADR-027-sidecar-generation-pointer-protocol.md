# ADR-027: 使用 immutable generation 与 current pointer 发布 Sidecar Runtime

## Status: Accepted (2026-08-17)

## Background

现有发布器把 `runtimeDir` 先改名为 `runtimeDir.backup-<token>`，再把 staging 目录改名为 `runtimeDir`。这两个目录 rename 之间，固定路径不存在。`scripts/lib/sidecar-runtime-publish.js:121-125` 定义了这个顺序；`scripts/test/sidecar-runtime-publish.test.js:126-157` 甚至在子进程被杀死后断言 `runtimeDir` 不存在。该恢复测试证明下一次 publisher 可以补救，但不能保证并发消费者在崩溃窗口内可读。

Tauri 消费者目前从固定的资源路径读取可执行文件、hash 和 provenance：`pet-ui/src-tauri/src/main.rs:107-128`。随后 supervisor 在启动前校验二进制与整套 runtime，并以该 runtime 为 child working directory：`pet-ui/src-tauri/src/sidecar.rs:109-132`、`189-200`。因此 publisher crash 可把消费者暴露给 `ENOENT`，而不是一个可验证的旧版本或新版本。

此 ADR 定义发布/读取/回收协议。它是修复前的实施和测试契约，不是已完成的 packaged runtime 证据。

## Decision

### 1. 稳定根目录与 immutable generation 格式

`jax-rtc-sidecar-runtime` 是稳定锚点，发布器禁止 rename、删除或替换该根目录。它的安装及运行时布局固定为：

```text
<resource_dir>/jax-rtc-sidecar-runtime/
  current.json
  generations/
    g-<64-lowercase-hex>/
      generation.json
      jax-rtc-sidecar.exe
      jax-rtc-sidecar.exe.sha256
      jax-rtc-sidecar.provenance.json
      jax-rtc-sidecar.provenance.sha256
      resources/app/native/...
  staging/
  leases/
  publish.lock
  reader-gc.lock
```

`g-<64-lowercase-hex>` 必须等于该 generation 的 `jax-rtc-sidecar.provenance.json` 的 SHA-256。generation ID 不是递增版本号、时间戳或用户输入；它将选择的目录与受验证的 provenance 字节绑定。

`generation.json` 采用以下最小 schema，编码为 UTF-8、无 BOM、单个 JSON object：

```json
{
  "schema_version": 1,
  "generation": "g-<64-lowercase-hex>",
  "manifest_sha256": "<64-lowercase-hex>",
  "files": {
    "jax-rtc-sidecar.exe": "<64-lowercase-hex>",
    "jax-rtc-sidecar.exe.sha256": "<64-lowercase-hex>",
    "jax-rtc-sidecar.provenance.json": "<64-lowercase-hex>",
    "jax-rtc-sidecar.provenance.sha256": "<64-lowercase-hex>"
  }
}
```

`files` 必须覆盖运行所需的所有 regular file，包括 native 依赖；实际枚举集合由现有 `validate_runtime` 的受信 manifest 定义。所有路径必须相对 generation、使用 `/`、无空段、`.`、`..`、绝对路径、盘符、UNC 路径、symbolic link 或 Windows reparse point。generation 最终目录一经完成不得被修改；修复、升级和重建均创建新 generation。

### 2. current pointer 的写入、原子发布与持久化

`current.json` 是唯一可变的选择器，格式如下：

```json
{
  "schema_version": 1,
  "generation": "g-<64-lowercase-hex>",
  "manifest_sha256": "<64-lowercase-hex>"
}
```

发布器必须遵守以下顺序：

1. 在同一 volume 的 `staging/g-<id>-<random>` 建立完整 runtime，拒绝任何 link/reparse point。
2. 校验所有文件 hash、`generation.json`、provenance 及 sidecar 启动前完整性规则；失败时只清理 staging，不变更 `current.json`。
3. flush 每个文件，flush staging/generation 目录元数据；同卷 rename staging 到此前不存在的 `generations/g-<id>`。若目标已存在，只有完全相同的已校验内容可以重用，否则失败，禁止覆盖。
4. 创建同目录 `current.json.tmp-<random>`，写入 pointer，flush 文件内容。
5. 在单个系统调用中把临时 pointer 替换为 `current.json`。该调用成功是发布唯一线性化点。成功前读者只能选到旧 generation；成功后读者可选旧或新 generation，但不得选到缺失或混合 generation。
6. 仅在 pointer 成功替换且必要的持久化调用完成后，异步执行 GC。发布状态不得把 GC 失败报告为发布失败。

禁止 `unlink(current.json)` 后 rename，禁止 `runtime -> backup -> staging -> runtime`，禁止跨 volume rename，禁止写入原 generation。任何 Windows `ERROR_ACCESS_DENIED`、`ERROR_SHARING_VIOLATION`、`EPERM` 或 `EACCES` 发生在 pointer replace 时，必须保留旧 pointer、报告未发布，并清理临时 pointer；不得退化为 delete-and-rename。

Windows 实现必须使用能够在同一 NTFS volume 上替换单一文件的原子 API，并请求 write-through durability。优先采用 `ReplaceFileW`；首次创建且无旧 pointer 时采用带 `MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH` 的 `MoveFileExW`。不得把 Node/Rust 的跨平台 rename 包装当作 Windows replace/ACL 行为的证据。实现必须记录原生错误码并对短暂 sharing violation 采用有上限的重试；耗尽重试仍为失败，旧 pointer 保持可用。

在支持目录 fsync 的平台，发布器还必须 sync pointer 所在目录。在 Windows 无法获得等价目录 flush 时，write-through 调用和 Windows 断电测试是最低要求；不能声称断电持久化已证明，直到该测试完成。

### 3. Reader 读取、校验和启动快照

`resolve_sidecar_spec` 必须改为一个 resolver，禁止再拼接固定 `runtime_dir/<file>`。resolver 的输入是稳定根目录和 build-time `COMPILED_MANIFEST_SHA256`，输出一个不可变 generation 的 `binary_path`、`manifest_path` 与 `runtime_dir`。

Reader 的正确顺序如下：

1. 以一次 `read` 获取 `current.json` 的完整 bytes；解析时拒绝空文件、截断 JSON、未知 schema、未知字段、错误 generation 格式和错误 hash 格式。
2. 在 `reader-gc.lock` 的 shared/read mode 内，校验 generation ID 与 manifest digest 一致、将路径限制在 `generations/<id>`、拒绝任意 symlink/reparse point，并为该 ID 创建唯一 lease。随后读取 generation 文件并做完整验证。
3. `manifest_sha256` 必须同时匹配 pointer、generation metadata、实际 provenance bytes 和 `COMPILED_MANIFEST_SHA256`。二进制 SHA 文件和 `validate_runtime` 必须继续校验；pointer 不是对 runtime 的信任替代。
4. 验证成功后关闭 `reader-gc.lock`，但 lease 持有到 sidecar child 退出。reader 只向 child 传入这一次解析得到的 generation 路径；不得在验证和 spawn 之间重新解析 `current.json`。
5. pointer、lease 或 runtime 任一校验失败时 fail closed，返回可诊断错误且不 spawn。已启动 child 可继续使用其已验证 generation；新启动不会回退到未经校验的目录扫描结果。

一个 reader 在 pointer 切换附近可能运行旧 generation 或新 generation，这是允许的。它不得读到 `ENOENT`、partial runtime、generation 间混合文件或未校验 payload。

### 4. Crash、rollback 与恢复

| 故障点 | 恢复和可见性要求 |
|---|---|
| staging 建立或校验前 publisher crash | `current.json` 未变；下次 publisher 删除孤儿 staging；读者继续旧 generation。 |
| generation 完成、pointer replace 前 crash | 新 immutable generation 可留下但不可见；下次 publisher 可在重新校验后复用或回收；读者继续旧 generation。 |
| 临时 pointer 写入/flush 前 crash | 旧 `current.json` 保持有效；清理 `.tmp-*`；不得读取临时文件。 |
| pointer replace 成功、GC 前 crash | 新 pointer 是已发布版本；旧 generation 保留，之后由 GC 处理。禁止自动回滚 pointer。 |
| GC 中 publisher crash | 当前 generation、活跃 lease 和保留代 generation 必须不受影响；剩余垃圾下次在独占 GC 门内重试。 |
| reader 在取 pointer、建 lease 或 spawn 前 crash | 租约通过 owner PID 与进程创建时间识别后可被回收；不影响 `current.json`。 |
| sidecar child 运行中 reader crash | 不以 reader 已退出为由立即删除 generation。Windows 上 child 持有的 EXE/DLL handle 导致删除失败时必须保留；child identity/退出确认后才可回收。 |

回滚不是把旧目录 rename 回固定 runtime。若已发布 generation 被证明不可用，应构建并验证一个新的 generation，再以新的 `current.json` replace 指向已知良好 generation。历史 generation 不可原地修补。

### 5. 租约与垃圾回收

租约文件位于 `leases/g-<id>/<reader-uuid>.json`，以 create-new 写入，至少包含 schema、generation、reader PID、进程创建时间和创建时间。lease 目录是 generation 外的可变协调区，不破坏 generation immutability。

Reader 在持有 shared `reader-gc.lock` 时完成“pointer snapshot -> lease create -> generation validation”。GC 必须在同一锁的 exclusive/write mode 中扫描 pointer 和 leases。这样避免 reader 读到已不再 current 的旧 pointer、但尚未建立 lease 时被 GC 删除的 TOCTOU 竞态。pointer 发布不需要持有 GC exclusive lock，避免把短时 reader 启动与发布串行化。

GC 只能删除同时满足以下条件的 generation：

- 不是 `current.json` 指向的 generation；
- 不在最近保留集合中，默认至少保留当前之外的两个已验证 generation；
- 没有活跃、无法确定失效的 lease；
- 超过最小保留时间；
- 路径、metadata 和 ownership 全部符合本 ADR，未知或损坏项目一律保留并告警；
- 删除失败（尤其 Windows `EPERM`、`EACCES`、sharing violation）时保留并下次重试。

租约过期只可在确认 PID 不存在或进程创建时间不匹配时清除。无法取得可靠进程 identity 时必须保留租约；空间压力不能降低这一安全标准。GC 的失败不影响已提交 pointer，也不能导致 publisher 返回成功却把 current generation 删除。

### 6. Windows ACL、rename 与并发约束

- runtime root、`generations`、`current.json`、`staging`、`leases` 和 lock 文件必须位于同一用户可写的 NTFS volume；禁止 FAT、网络 share 和跨盘临时目录作为发布目标。
- 安装器创建 runtime root 后授予应用运行身份读取/执行权限，并只向 runtime publisher 身份授予在 root 下创建 generation、写 pointer、创建 lease 和 GC 的权限。拒绝普通低完整性进程写入 `current.json` 或 generation。
- Reader 打开 pointer/runtime 文件时不得请求会阻止原子 pointer 替换的共享模式；若应用使用原生 Windows handles，必须允许读取、删除共享以使 `ReplaceFileW` 生效。generation payload 不在运行时写入。
- Windows 目录删除不是原子发布机制，也不能依赖打开 EXE/DLL 时的删除行为。出现 ACL 或文件占用错误时 fail closed 或延迟 GC，绝不先删除可用 current pointer。
- 多个 publisher 继续使用 `publish.lock`，但锁 owner 数据必须包含随机 token、PID 和创建时间。stale lock 恢复不得删掉完成 generation 或 pointer，只能清理能证明属于中断事务的 staging/临时文件。

### 7. build.rs、打包与安装布局

构建输入从单层目录改为 stable root 加 selected generation：

- `scripts/lib/sidecar-runtime-publish.js` 的 `runtimeDir` 参数语义改为稳定根目录；staging/backup 扫描只处理 `staging`、`.tmp-*` 和明确的协议垃圾，删除 `runtimeDir.backup-*` 交换恢复路径。
- `scripts/lib/sidecar-package.js` 和 sidecar package verifier 必须生成 initial `generations/g-<manifest-sha>/`、`generation.json` 和 `current.json`，并先验证 resolver 能选择该 generation。
- `pet-ui/src-tauri/build.rs:37-41` 的 rerun inputs 必须监视 root 下 `current.json`、selected generation manifest、hash、payload 清单及 packaging sources；release verifier 必须解析 pointer 并拒绝缺失、越界或不匹配的 selected generation。
- `pet-ui/src-tauri/build.rs:77-82` 编译进二进制的 manifest SHA 必须来自 `current.json` 所选 generation 的实际 provenance bytes，不能从已废弃的 root-level manifest 路径读取。
- Tauri `bundle.resources` / `externalBin` 配置及 installer 必须保留 `jax-rtc-sidecar-runtime` 的完整层级，不得扁平化、跟随符号链接或只安装 `current.json`。clean install 验收须证明 `resource_dir` 下 pointer 和选中 generation 都存在。
- `pet-ui/src-tauri/src/main.rs:107-134` 与 `pet-ui/src-tauri/src/sidecar.rs:109-132` 必须消费 resolver 产物。`SidecarSpec.integrity.runtime_dir` 和 `Command::current_dir` 指向 generation，而不是稳定根目录。

这些改动是接口和安装布局不兼容变更；旧单层布局不做静默 fallback。发现旧布局时应给出迁移/重新安装错误，避免回退到不具备原子可见性保证的路径。

## Required RED Tests Before Implementation

以下测试先以“永不观察 missing/partial”为断言加入，必须在当前双 rename 实现上 RED。它们使用真实子进程、真实磁盘、协议目录和屏障；不得用注入 rename 抛错或单进程 fixture PASS 替代。

| ID | 父 reader / 子 publisher 场景 | 初始 RED 断言与通过条件 |
|---|---|---|
| RP-01 | 父 reader 循环执行 resolver、完整校验并打开 EXE/provenance；子 publisher 在旧 runtime 改名后、新 runtime 改名前被 kill。 | 当前实现会观测 `ENOENT`，故 RED。新协议下每次 reader 结果只能为完整 old 或完整 new，0 次 missing/partial。 |
| RP-02 | 子 publisher 分别在 staging 写入、generation finalize、pointer temp flush、pointer replace 后四个持久屏障 kill；父 reader 在每个屏障重复解析。 | 每个点都不得产生 malformed pointer、路径越界、缺失或混合 runtime；pointer replace 后允许 old/new，但启动前校验必须成功。 |
| RP-03 | 子 publisher 在 Windows 上持续更新 pointer，父 reader 以实际 Windows file sharing 打开并验证 pointer/runtime。 | 记录原生错误码；不得出现 delete-and-rename 空窗。sharing violation 只能使 publisher 未发布，不能使 reader 失去旧 generation。仅 Windows CI/现场能将此项转绿。 |
| RP-04 | 父 reader 读到 old pointer 后在 lease 前暂停；子 publisher 切换 new 并立即 GC。 | 在 reader-gc shared lock 保护下 GC 不得删除 old；恢复 reader 后能建立 lease 并验证 old。此测试证明 GC TOCTOU 已关闭。 |
| RP-05 | 子 reader 在 lease 创建后、spawn 前被 kill；随后 publisher GC。 | stale lease 可被安全清理，current/new 与任何活跃 child generation 不被删除；不可信 identity 时保留 lease。 |
| RP-06 | 父 reader 启动 sidecar child 后自身退出或被 kill；publisher 尝试 GC。 | Windows 文件占用/child identity 下删除失败必须保留；child 退出且 lease 可确认失效后才允许删除。 |
| RP-07 | 从 build 输出打包 installer，clean install 后以实际 `resource_dir` 执行 Tauri resolver 和 supervisor pre-spawn validation。 | 证明完整 generation 布局、compiled manifest SHA 和 child current_dir 一致；仅 build tree fixture 不可通过。 |

每个 crash 测试必须在子进程到达确定性 IPC barrier 后调用强制终止，父进程收集 exit code、时间线、每次 reader 观测、pointer bytes、generation ID 和文件 hash。测试接受 "old" 或 "new" 的集合性结果，不接受基于时间猜测的唯一版本断言。

## Acceptance Boundary

实施可进入 code review 的最低条件：协议 resolver、publisher、GC 和上述 RP-01 至 RP-06 的实现与测试都合入；Linux/Node 测试仅能证明逻辑和 crash 编排，不能证明 Windows rename/ACL 语义。

packaged sidecar Claim 只有在 RP-03 和 RP-07 在目标 Windows 环境通过，并且同一 candidate 的 installer hash、clean install、Tauri supervisor pre-spawn、Credential Manager、sidecar launch、TRTC 与 no-orphan 现场证据完整时才可从 `EvidencePending` 变为通过。任何一项缺失维持 fail closed。

## Evidence Status At ADR Creation

本 ADR 仅进行指定本地文件的离线审查，未运行 Node、Cargo、Tauri、installer、Windows ACL/rename、断电恢复或 packaged launch 测试。以下均为 `NOT_RUN`：

- RP-01 至 RP-07 全部 crash/reader/publisher 测试；
- Rust resolver 与 Tauri supervisor 编译、单元测试和实际 spawn；
- Windows `ReplaceFileW`/`MoveFileExW`、ACL、sharing mode、锁和断电持久化行为；
- installer build、clean install、资源布局和 packaged sidecar launch；
- Credential Manager、TRTC、watchdog/no-orphan 的现场链路。

`overview.md:79-81` 与 `.workbuddy/memory/2026-08-17.md:7` 记录过 fixture PASS 和 packaged launch/cargo 失败的历史审计结论；它们不是本 ADR 的新执行证据，不能替代上述 `NOT_RUN` 项。

## Consequences

正面后果：消费者永远从稳定根选择并验证一个 immutable snapshot；publisher crash 不再以固定 runtime 路径缺失的形式暴露；完整性校验可与 build-time manifest digest 连续绑定；GC 不会与启动 reader 产生未受保护的删除竞态。

负面后果：新增 resolver、native Windows pointer replace 适配、lease/lock 协调、布局迁移和多进程 crash 测试；安装包可能因保留 generation 增大；回收需要可审计失败处理。MVP 不引入独立更新服务、数据库或远程配置面。

## Related ADRs

- ADR-017: Tauri sidecar supervision
- ADR-025: Sidecar package layout
- ADR-026: Packaged Electron sidecar launch contract
