# 统一发布/迁移锁：实现修订（2026-08-30）

> 修订对象：`docs/plans/2026-08-29-sidecar-publish-migration-lock-implementation.md`
> 结论：原计划基于 Rust named-mutex helper 的方案**被实测否决**，同步路径改为目录级跨进程租约。

## 1. 否决 Rust named-mutex helper 的实测依据

`tools/sidecar-publish-coordination/src/main.rs` 是**行式 stdin 循环**，租约（`coordinator`
变量）存活在 helper 进程内。同步脚本用一次性子进程调用时，helper 处理完请求即退出，
**互斥量随之释放**，得到的只是“某一瞬间无人持锁”的假象，而不是跨越
probe → rename → publish → verify 的持锁区间。要在同步脚本里真正持有它，必须维护长驻
子进程并做异步行读，与现有同步发布/迁移链路不兼容。

因此同步路径改用具名**目录级原子租约**，并保留与 Rust `Owner` 一致的身份字段：

```text
schema_version / token / pid / created_at / process_creation_time / process_creation_identity
```

后续若要切回 named mutex，只需替换 acquire 实现，不动调用方。

附带协议约束：Rust `protocol.rs` 使用 `#[serde(deny_unknown_fields)]`，owner 记录不得附加
`operation` / `runtime_name` 等字段，否则被判 `invalid_request`。Node 租约的 owner 文件是
自有 schema（Rust 六字段的超集），已在代码注释中标明。

## 2. 落地实现（隔离 worktree，未合入主工作区）

### `scripts/lib/sidecar-runtime-coordination.js`

- `runtimeCoordinationIdentity(runtimeDir, options)`
  先 `realpath` 解析父目录再小写化，锁名为：

  ```text
  <resolved parent>/<runtime name>.coordination-lock
  ```

  它是 runtime root 的**兄弟节点**，因此 legacy 迁移 rename 整个 root 时不会带走锁。

- `acquireRuntimeLease(runtimeDir, options)`
  原子 `mkdir` 建锁目录 → `wx` 独占写 owner 记录 → 返回 release 函数。
  锁已存在时按 owner 记录判定，且一律 fail-closed：

  | 情形 | 诊断码 |
  |------|--------|
  | owner 文件缺失或字段非法 | `SIDECAR_RUNTIME_COORDINATION_OWNER_AMBIGUOUS` |
  | 进程探针不可用/超时/脏输出 | `SIDECAR_RUNTIME_COORDINATION_PROBE_UNAVAILABLE` |
  | owner 进程已不存在 | `SIDECAR_RUNTIME_COORDINATION_STALE_OWNER`（不自动回收） |
  | PID 存活但创建时间与 owner 不符 | `SIDECAR_RUNTIME_COORDINATION_PID_REUSED` |
  | PID 存活且创建时间一致 | `SIDECAR_RUNTIME_COORDINATION_BUSY` |
  | 释放时 token 不匹配 | `SIDECAR_RUNTIME_COORDINATION_LEASE_LOST` |
  | 删除 owner 失败 | `SIDECAR_RUNTIME_COORDINATION_RELEASE_FAILED` |

- 进程探针：默认走固定参数数组的 Windows 进程查询（按 PID 取 `Win32_Process` 与创建时间，
  PID 先做正整数校验后才拼接，5s 超时）。任何错误、超时、脏 JSON 都降级为 `unknown` 并
  fail-closed，绝不把“查不到”当作“没有持有者”。

### `scripts/build-sidecar-external-bin.js`

- 普通 build/verify 与 legacy 迁移**共用同一把锁**，仅 `operation` 不同（`publish` / `migration`）。
- **修复 TOCTOU**：生产可信门（PE 头/体积 + selected generation 校验）原先在释放租约**之后**
  执行，已移入 `try` 内、release 之前。否则并发 publisher 可在 release 之后替换
  `current.json`，使“被校验的 generation”与“实际选中的 generation”不一致。
- 新增可注入接缝：`packageConfig` / `buildPackage` / `verifyPackage` / `trustGate` /
  `acquireRuntimeLease` / `coordination`，使持锁顺序由测试证明，而不是靠人工约定。

## 3. 实测证据（本机，非模拟）

- **双进程争用**（固定绝对路径 + 时间戳）：
  - 进程 A：`A_HELD_AT=2026-08-30T03:16:03.295Z`，持锁 20s；
  - 进程 B：`B_AT=2026-08-30T03:16:13.553Z` 争用同一锁；
  - 结果：`SIDECAR_RUNTIME_COORDINATION_BUSY`（B 的尝试确在 A 的持锁窗口内）。
- **失败注入**：`unlink` 抛错 → `RELEASE_FAILED`；owner token 被改 → `LEASE_LOST`；
  脏 owner → `OWNER_AMBIGUOUS`；探针 unknown → `PROBE_UNAVAILABLE`。
- **回归**：`58 tests / 57 passed / 0 failed / 1 skipped`。
  跳过项仍是 Windows junction/reparse 真机测试，不得计为通过。

## 4. 未决项（OPEN）

| 项 | 现状 | 阻断什么 |
|----|------|----------|
| ~~路径拼写归一~~ | **2026-08-30 RESOLVED**：改用 `fs.realpathSync.native` 展开 8.3 短名；真机复核 `C:\Users\ADMINI~1\...` 与 `C:\Users\Administrator\...` 现收敛为同一锁名（`COLLAPSED=true`） | 已解除 |
| ~~Windows reparse/junction 实机拒绝测试~~ | **2026-08-30 RESOLVED**：新增 `scripts/test/sidecar-runtime-migration-reparse.test.js`，在本机创建真实 junction（顶层 + 嵌套）验证迁移被 `SIDECAR_RUNTIME_MIGRATION_REPARSE_POINT` 阻断且不移动任何证据；测试自带 junction 清理 | 已解除 |
| stale owner 自动回收 | 当前一律 fail-closed，需人工删除锁目录 | 运维负担；在确认无活跃 consumer 前不做自动回收 |
| 长驻 helper 切换 | 暂不使用 Rust named mutex | 后续若需更强互斥，需引入长驻进程与异步协议 |
| 普通 publisher 端到端争用 | 已用注入接缝证明顺序 + 真实双进程租约争用，未用真实 build 双跑 | 正式迁移前需补一次真实双进程 build 争用 |
| ~~Cargo / native pointer helper 正式构建环境~~ | **2026-08-30 RESOLVED**：新增 `resolveCargoPath()`，按 `cargoPath` → `JAX_CARGO_BIN` → `%USERPROFILE%\.cargo\bin\cargo.exe` → 可验证的 PATH 查找顺序解析，全部不可用时抛 `SIDECAR_CARGO_TOOLCHAIN_UNAVAILABLE`；本机实测该 shell 的 PATH 中没有 cargo，靠 cargo home 命中 | 已解除；CI 仍建议显式设置 `JAX_CARGO_BIN` |
| ~~普通 publisher 端到端争用~~ | **2026-08-30 RESOLVED**：新增 `sidecar-publish-migration-contention.test.js`——真实进程 A 持锁，另一进程执行**真实迁移 CLI**（`build-sidecar-external-bin.js --migrate-legacy-runtime`），以 exit 1 + `SIDECAR_RUNTIME_COORDINATION_BUSY` 失败，且未创建 backup、未生成 `current.json` | 已解除 |
| installer / clean-install / packaged 六场景 | 无目标 Windows 会话动态证据 | 商业发布 NO-GO |

## 5. 补充实测（2026-08-30 后续）

- Windows junction 判定：Node 对真实 junction 报 `isSymbolicLink=true`、`S_IFMT=0xa000`，
  现有 `isReparsePoint()` 可识别；此前只是**没有真机证据**，现已补上。
- 8.3 短名：普通 `fs.realpathSync` **不展开**短名（原样返回 `ADMINI~1`），
  `fs.realpathSync.native` **会展开**为长名。已据此修正 identity 实现。
- 全量回归（隔离 worktree）：`61 tests / 60 passed / 0 failed / 1 skipped`；
  唯一跳过项是 POSIX-only symlink 用例（Windows 侧由真实 junction 套件覆盖）。
- Cargo 工具链（同一轮）：本机 shell PATH 中**无** `cargo`（`which cargo` 失败），
  但 `%USERPROFILE%\.cargo\bin\cargo.exe` 存在（指向 rustup.exe）。`resolveCargoPath()`
  解析结果 = `C:\Users\Administrator\.cargo\bin\cargo.exe`。
  强制重编译验证：`touch` 源码后构建，`BUILD_STATUS=0` 且产物 mtime 由
  `2026-08-29T14:05:19Z` 变为 `2026-08-30T03:51:57Z`——证明解析出的工具链确实能重新产出
  helper，而不是命中缓存的假成功。
- 含工具链套件的全量回归：`68 tests / 67 passed / 0 failed / 1 skipped`。

## 6. 锁结构修订：目录租约 → 单文件租约（2026-08-30 晚）

端到端争用测试暴露了目录租约的释放缺陷：`fs.rmdirSync` 在受限运行环境会被删除 shim
路由到 trash 并**挂起**（owner 文件已删除但目录残留，holder 卡死）。锁的释放不应依赖
目录删除，因此改为**单文件租约**：

- 锁路径 = `<resolved parent>/<runtime name>.coordination-lock`，**该文件本身即 owner 记录**；
- 获取：`openSync(lockFile, 'wx')` 独占创建（原子性等价于原子 mkdir），EEXIST 时读取并
  判定 owner（AMBIGUOUS / PROBE_UNAVAILABLE / STALE_OWNER / PID_REUSED / BUSY）；
- 释放：校验 token 后 `unlinkSync`，失败抛 `RELEASE_FAILED`——不再有 mkdir/rmdir 步骤；
- 新增诊断码 `SIDECAR_RUNTIME_PARENT_MISSING`：runtime 父目录不存在时给出稳定诊断，
  而不是裸 ENOENT（隔离 worktree 无 binaries 目录时实测暴露）。

### 端到端争用证据（真实 CLI，非注入）

```text
进程 A：真实 acquireRuntimeLease 持锁（以 owner 文件出现为证）
进程 B：node scripts/build-sidecar-external-bin.js --migrate-legacy-runtime --backup-dir <tmp>
结果：  exit 1，stderr = SIDECAR_RUNTIME_COORDINATION_BUSY
        backup 未创建，runtime 未生成 current.json
释放：  写入 release 标志后，A 释放并以锁文件消失为证
```

含争用套件的全量回归：`70 tests / 69 passed / 0 failed / 1 skipped`。
主工作区未改动；隔离 worktree 无残留锁、无空壳 runtime 目录。
