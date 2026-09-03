# ADR-025: sidecar 打包布局 —— externalBin 与 runtime 必须合流

## Status: Accepted（2026-08-14，首席架构师高见远裁决）

> 编号说明：`docs/decisions/` 已到 ADR-024，本决策顺延为 **ADR-025**。
> 触发：P0「语音不通/没声音」，总监真机排查确认 sidecar（jax-rtc-sidecar.exe）根本没起来。

## Background

语音链路：手机 App 进 TRTC 房间 ↔ sidecar（Electron，trtc-electron-sdk）进同一房间做无头对端。
sidecar 起不来 = 房间没有对端 = 手机听不到声音。

总监真机排查锁定的事实：

1. `tasklist` 查无 jax-rtc-sidecar 进程。
2. 手动启动安装目录根部的 `jax-rtc-sidecar.exe`，报
   `error while loading shared libraries: ffmpeg.dll: cannot open shared object file`。
3. 安装目录：根目录 `jax-rtc-sidecar.exe`（180MB，Electron 主程序）；子目录
   `jax-rtc-sidecar-runtime/` 里是 `ffmpeg.dll`、`icudtl.dat`、`resources.pak`、
   `v8_context_snapshot.bin`、`locales/`、`resources/app/`（sidecar 的 main.js/rtc.js）。
4. `scripts/lib/sidecar-package-build.js` L37-41 把 `electron.exe` 复制到根目录
   executable 后，`fs.rmSync(electronExe)` 从 runtime 目录删掉 electron.exe。
5. `tauri.conf.json` `externalBin: ["binaries/jax-rtc-sidecar", ...]`（exe 装到根）+ 
   `resources: {"binaries/jax-rtc-sidecar-runtime/": "jax-rtc-sidecar-runtime/"}`
   （runtime 装到子目录）。
6. `main.rs resolve_sidecar_spec`：`binary_path = resource_dir/jax-rtc-sidecar.exe`（根）。
7. `sidecar.rs spawn_with_credential`：`Command::new(binary_path)`，无 current_dir，
   `stderr(Stdio::null())`（崩溃日志被吞）。
8. 手动把 cwd 设到 runtime 目录，仍报 `Invalid file descriptor to ICU data`——证明
   icudtl.dat 等是相对 exe 路径硬编码读取，不走 cwd。

### 设计意图溯源（不是要推翻它，是要搞清它想保护什么）

读 `docs/windows-sidecar-packaging.md` 与 ADR-017/019，externalBin（根）与 runtime（子目录）
的分离承载了三个明确意图：

- **品牌化入口**：客户任务管理器看到 `jax-rtc-sidecar.exe` 而非裸 `electron.exe`
  （与 ADR-024「进程家族品牌化 jax-*」同源）。
- **完整性校验**：manifest 的 `external_bin.sha256`（exe 单独 hash）+ `runtime_files`
  （runtime 目录闭集逐一 hash）；两者是不同粒度的校验面。
- **provenance 证明**：manifest 记录带 target triple 的 `build_input_file` 与安装逻辑名
  `installed_file`，并把 manifest digest 作为编译期常量嵌入 Tauri binary
  （`JAX_SIDECAR_MANIFEST_SHA256`），启动时逐文件校验。

这三个意图全部**成立且必须保留**。但「exe 放根目录、运行时放子目录」这一条**实现载体**是错的——
它与 Electron 的加载机制硬冲突，不是可以打补丁绕过的小问题。

## Decision

### D1. 结论：externalBin 分离对 Electron 不可行，必须合流

Electron 的 `electron.exe` 与其 dist 目录是一体的，不能拆开：

- **DLL（ffmpeg.dll / libEGL.dll / vk_swiftshader.dll / d3dcompiler_47.dll 等）**：
  Windows `LoadLibrary` 首先搜索「exe 所在目录」。exe 在根、DLL 在子目录 → 必然
  `cannot open shared object file`。**无环境变量/flag 可改 DLL 搜索目录。**
- **数据文件（icudtl.dat / resources.pak / v8_context_snapshot.bin / locales/）**：
  Chromium 启动引导（`base::PathService::Get(DIR_ASSETS)`，经 `GetModuleFileName` 定位
  exe 目录）按 **exe 目录**相对路径加载，**不看 current_dir**。总监实测「cwd 设到 runtime
  仍报 ICU 错误」即印证。
- **`ELECTRON_OVERRIDE_DIST_PATH`**：只被 npm `electron` 包的 `cli.js` 包装器读取
  （`node_modules/electron/cli.js` 决定去哪个 dist 找 electron.exe）；**不被 electron.exe
  本体读取**。本侧直接用 `Command::new(exe)` 拉起 electron.exe，不经过 npm 包装器，故无效。
- **`--resources-path` / `app.setPath`**：至多能重定位 `resources/`（app.asar），
  **管不到 DLL 和 icudtl.dat/pak**。

官方唯一支持的「品牌化」方式是**原地重命名**：把 `electron.exe` 重命名为
`jax-rtc-sidecar.exe`，**留在 dist 目录内**与 DLL/数据文件同目录（electron-builder 对
Windows 便携/免安装包就是这么做）。

### D2. 正确布局：品牌 exe 留在 runtime 目录内

```
安装目录（resource_dir）/
├─ jax-pet.exe                     （Tauri 主程序）
├─ jax-rtc-sidecar-runtime/        （受管 runtime 闭集）
│   ├─ jax-rtc-sidecar.exe         （← electron.exe 原地重命名，与下方同目录）
│   ├─ ffmpeg.dll / icudtl.dat / resources.pak / v8_context_snapshot.bin
│   ├─ locales/ / resources/app/   （sidecar main.js/rtc.js + trtc-electron-sdk native）
│   └─ jax-rtc-sidecar.exe.sha256 / *.provenance.json / *.provenance.sha256
└─ provision_sidecar_credential*.exe（externalBin 保留，与本次无关）
```

三个意图的落地方式：

| 意图 | 原载体（错误） | 正确载体（本次） |
|------|----------------|------------------|
| 品牌化入口 | exe 复制到根 | **electron.exe 原地重命名为 jax-rtc-sidecar.exe** |
| 完整性校验 | exe 在根单独 hash | external_bin.sha256 仍单独 hash exe；exe 同时进入 runtime_files 闭集 |
| provenance | build_input_file 带 triple | build_input_file 改为 `jax-rtc-sidecar.exe`，target_triple 仍由 manifest 顶层字段承载 |

关键取舍：**exe 从「非受管安装同级文件」变为「受管 runtime 文件」**。这不削弱完整性——恰恰相反，
exe 现在既被 `external_bin.sha256` 单独校验、又被 `runtime_files` 闭集校验，双重覆盖；
`bundle_resources` 的 1:1 目录映射（`binaries/jax-rtc-sidecar-runtime/` →
`jax-rtc-sidecar-runtime/`）把 exe 连同 dist 一起以受管形式搬进安装包。

### D3. 修复点清单（实施者照做，精确到文件/行）

P0 主修复（让 sidecar 能启动）：

1. `scripts/lib/sidecar-package-build.js` L38-41：
   把 `fs.copyFileSync(electronExe, config.executable)` + `fs.rmSync(electronExe)` 两行，
   替换为 `fs.renameSync(electronExe, config.executable)`（config.executable 此时应指向
   `runtimeDir/jax-rtc-sidecar.exe`）。同时 L34 的 `fs.rmSync(config.executable)` 变为冗余
   （L33 已整目录删除 runtimeDir），可删可留、建议删以免歧义。

2. `scripts/build-sidecar-external-bin.js` L39：
   `executable: path.join(binDir, 'jax-rtc-sidecar-${TARGET_TRIPLE}.exe')`
   → `executable: path.join(runtimeDir, 'jax-rtc-sidecar.exe')`。

3. `pet-ui/src-tauri/src/main.rs` L112-114（resolve_sidecar_spec）：
   把 `let runtime_dir = dir.join(SIDECAR_RUNTIME_DIR);` 提到 binary_path 之前，
   `binary_path = runtime_dir.join(SIDECAR_BIN)`（原来是 `dir.join(SIDECAR_BIN)`）。
   `SIDECAR_BIN`/`SIDECAR_RUNTIME_DIR` 常量值不变；hash_path 已指向 runtime_dir，不动。

4. `pet-ui/src-tauri/src/sidecar_integrity.rs` L181-185（validate_metadata）：
   `build_input_file` 断言从 `format!("jax-rtc-sidecar-{}.exe", target_triple)`
   改为字面量 `"jax-rtc-sidecar.exe"`。

5. `pet-ui/src-tauri/tauri.conf.json` L33-38（bundle.externalBin）：
   从 externalBin 数组移除 `"binaries/jax-rtc-sidecar"`（exe 已随 resources 打包进 runtime
   子目录）；保留 `provision_sidecar_credential` / `provision_sidecar_credential_launcher`。
   capability（`capabilities/sidecar.json` 仅 `core:default`）与 spawn 均走
   `std::process::Command`，不依赖 shell 插件，移除 externalBin 无权限面影响。

6. `pet-ui/src-tauri/build.rs` L41（emit_rerun_rules）：
   `"binaries/jax-rtc-sidecar-x86_64-pc-windows-msvc.exe"`
   → `"binaries/jax-rtc-sidecar-runtime/jax-rtc-sidecar.exe"`。

测试夹具同步（否则 verify/test 红）：

7. `pet-ui/src-tauri/tests/support.rs` L51 与 L94：
   L51 `binary_path = root.join("jax-rtc-sidecar.exe")` → `runtime_dir.join("jax-rtc-sidecar.exe")`；
   L94 `"build_input_file": "jax-rtc-sidecar-x86_64-pc-windows-msvc.exe"` → `"jax-rtc-sidecar.exe"`。
   （binary_path 移入 runtime_dir 后，`runtime_entries` 会自然把它纳入 runtime_files，
   与 sidecar_integrity.rs 的 `list_runtime_files` 逻辑一致。）

8. `scripts/test/sidecar-package.test.js` L54-56、L185、L200：
   L54-56 fixture 的 `exe = path.join(binDir, 'jax-rtc-sidecar-${TARGET_TRIPLE}.exe')`
   → `path.join(runtime, 'jax-rtc-sidecar.exe')`；
   L185 断言 `build_input_file` 改为 `'jax-rtc-sidecar.exe'`；
   L200 的「未受管兄弟」用例语义微调（exe 现属 runtime 闭集，不再是 binDir 根下的兄弟）。

9. `scripts/create-sidecar-runtime-fixture.js` L16、L43-46：
   L16 `executable` 改为 `path.join(runtimeDir, 'jax-rtc-sidecar.exe')`；L44 的
   `fs.rmSync(executable)` 变为冗余（L43 已删 runtimeDir），L46 写入位置随 executable 落在
   runtimeDir 内，顺序仍自洽。

10. `pet-ui/src-tauri/src/sidecar.rs` L165-167（spawn_with_credential）：
    `stderr(Stdio::null())` 改为把 stderr 落盘到 app_log_dir 下的 sidecar 日志文件
    （如 `sidecar-stderr.log`，create/append 均可）。
    **总监红线（必做，非 advisory）**：这正是本次 ffmpeg.dll 崩溃只能靠手动启动才暴露的
    能见度根因——stderr 静默吞掉崩溃日志 = 未来 sidecar 再崩依旧不可见。落盘后任何
    启动崩溃都能被日志定位，杜绝「静默起不来」复发。

验收（手动，实施者最后跑）：

- 重跑 `npm run build:sidecar` + `npm run verify:sidecar`，再 `cargo build --release`
  （build.rs 会二次 verify）。
- 断言 `binaries/jax-rtc-sidecar-runtime/` 下出现 `jax-rtc-sidecar.exe`（180MB），
  且根目录不再有 `jax-rtc-sidecar-x86_64-pc-windows-msvc.exe`。
- 手动启动 `binaries/jax-rtc-sidecar-runtime/jax-rtc-sidecar.exe --role=sidecar`，
  不再报 ffmpeg.dll / ICU 错误。

## Consequences

正面：

- sidecar 真正能启动，TRTC 房间有对端，手机语音恢复。
- 品牌化（jax-rtc-sidecar.exe）、完整性（external_bin.sha256 + runtime_files 闭集）、
  provenance（build_input_file/installed_file/target_triple + manifest digest 嵌入）三者全部保留。
- exe 进入受管 runtime 闭集后，反而多一层 `runtime_files` hash 覆盖。

负面：

- exe 的 `build_input_file` 语义从「带 target triple 的根目录产物」变为「branded runtime 产物」，
  需同步更新 JS 测试断言与 Rust fixture（D3 第 4/7/8/9 条），否则 verify/test 红。
- 安装根目录不再有 `jax-rtc-sidecar.exe` 独立入口；品牌入口藏进 `jax-rtc-sidecar-runtime/`
  子目录（这是 Electron 硬约束的代价，不是设计偏好）。

## Migration and rollback

- 本次是打包布局修正，无数据/credential 迁移；安装包重新构建即可。
- 回滚 = 还原 D3 所列 9 处改动（revert），回到「exe 在根」的旧布局——但旧布局本来就不能启动，
  回滚仅用于代码层面对照，不构成可发布态。

## Explicitly not doing

- 不改成「把 dist 整体铺到安装根目录」（会污染安装根、丢 runtime 目录隔离、易与 Tauri 主程序
  文件命名冲突）。
- 不用 `--resources-path` / `ELECTRON_OVERRIDE_DIST_PATH` / current_dir 去「迁就」分离布局——
  三者对 DLL 与 icudtl.dat/pak 均无效，属于自欺式绕过。
- 不碰 mobile-app / backend / ADR-017 的「Tauri 只监督 sidecar」独立进程模式。
- 不引入代码签名（阶段 G，等营业执照，ADR-024）。

## Related ADRs

ADR-017（Tauri externalBin 只监督 sidecar）、ADR-019（sidecar credential CM 存储）、
ADR-020（TLS 四端 + 自签 CA，本布局中 certs/ca.crt 与 runtime 同源打包）、
ADR-024（进程品牌化 jax-* 家族，本决策的「品牌化入口」目标与之同源）。
