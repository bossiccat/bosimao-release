# ADR-026: Windows packaged Electron sidecar 启动契约

## Status: Accepted (2026-08-14)

本 ADR 补充 ADR-025，只裁决 Gate A 的 packaged Electron 启动方式。它不修改 API、数据库或业务需求，因此不新增 `openapi.yaml`。

## Background

ADR-025 已锁定 Electron runtime 合流布局：`electron.exe` 原地改名为 `jax-rtc-sidecar.exe`，并与 DLL、ICU、pak、locales 和 `resources/app` 保持同目录分发。剩余问题是 Tauri supervisor 应如何执行该 branded exe，以及宿主环境和业务参数如何进入 Electron main process。

项目固定版本与现状：

- Electron `31.7.7`，入口由 `sidecar/package.json` 的 `main: main.js` 指定。
- 开发命令是 `electron . --role=sidecar`；这是 default Electron executable 接收 app path 的开发契约。
- 分发目录内存在 `resources/app/package.json`，且 `resources/default_app.asar` 已删除。
- `pet-ui/src-tauri/src/main.rs::resolve_sidecar_spec` 已把 exe 解析为 runtime 内的 branded exe，并固定参数 `--role=sidecar`。
- `sidecar/main.js` 从 `process.argv.slice(1)` 过滤业务参数；`sidecar/config.js::parseArgList` 只接受单元素 `--key=value`。
- 临时证据 `_node_opts.txt` 显示宿主同时注入 `ELECTRON_RUN_AS_NODE=1` 与包含 `--require=...genie-safe-delete.cjs --use-system-ca` 的 `NODE_OPTIONS`。
- 历史现象包含 `bad option`、`Cannot find module 'electron'` 和 `ipcMain` 不可用，均与上述污染变量相符。

Electron 官方文档给出的事实：

1. Windows 手工打包时，应用目录应放在 Electron dist 的 `resources/app`，然后直接执行 Windows exe；Windows rebranding 允许重命名 `electron.exe`。
2. `process.defaultApp` 仅在应用作为参数传给 default Electron executable 时为 `true`，例如 `electron .`；否则为 `undefined`。它用于判断 `process.argv` 需要跳过多少项。
3. `ELECTRON_RUN_AS_NODE` 会让 Electron 作为普通 Node.js 进程启动。
4. packaged app 明确禁止除极少数例外外的 `NODE_OPTIONS`；当前宿主注入项不属于允许集合。
5. `NODE_EXTRA_CA_CERTS` 是独立的生产环境变量，项目依据 ADR-020 用它向 Node 网络栈注入打包 CA。

官方依据：

- https://www.electronjs.org/docs/latest/tutorial/application-distribution
- https://www.electronjs.org/docs/latest/api/process#processdefaultapp-readonly
- https://www.electronjs.org/docs/latest/api/environment-variables#electron_run_as_node
- https://www.electronjs.org/docs/latest/api/environment-variables#node_options
- https://www.electronjs.org/docs/latest/api/environment-variables#node_extra_ca_certs

## Decision drivers

评分为 1 至 5，越高越好。权重：启动语义正确性 35%、改动与学习成本 20%、分发闭集 20%、安全边界 20%、扩展性 5%。
| 方案 | 正确性 | 成本 | 闭集 | 安全 | 扩展 | 加权分 |
|---|---:|---:|---:|---:|---:|---:|
| A. 直接执行 branded packaged exe，只传业务参数 | 5 | 5 | 5 | 5 | 4 | 4.95 |
| B. 执行 branded exe，再传 `.` 或 `resources/app` | 2 | 3 | 3 | 3 | 2 | 2.55 |
| C. 保留 default `electron.exe`，显式传 app path | 3 | 2 | 1 | 2 | 3 | 2.10 |

### 方案 A: 直接执行 packaged exe

直接执行 runtime 内 `jax-rtc-sidecar.exe`，不传 app path。Electron 根据同级 `resources/app/package.json` 加载 `main.js`，只传 `--role=sidecar` 等受控业务参数。它符合官方手工打包与 rebranding 模型，保留品牌化、runtime 闭集、完整性与 provenance；代价是必须净化继承环境并建立 packaged 真机回归。

### 方案 B: branded exe 加 app path

执行 `jax-rtc-sidecar.exe . --role=sidecar`，或把绝对 `resources/app`、`main.js` 作为参数。该方案把 `electron .` 的开发态 default-app 语义错误移植到 branded exe，制造多余 argv、cwd 或安装绝对路径依赖，不能作为 fallback。

### 方案 C: default electron.exe 加 app path

保留 `electron.exe` 与 `default_app.asar`，由 Tauri 传 app path 和业务参数。该方案破坏 ADR-024 的品牌进程名，扩大可执行入口与打包闭集，又没有解决宿主环境污染。MVP 不引入第二套 Electron 启动模式。

## Decision

唯一允许的生产启动契约是方案 A。

### D1. 路径契约

```text
resource_dir/
├─ certs/ca.crt
└─ jax-rtc-sidecar-runtime/
   ├─ jax-rtc-sidecar.exe
   ├─ ffmpeg.dll
   ├─ icudtl.dat
   ├─ resources.pak
   ├─ locales/
   └─ resources/app/
      ├─ package.json
      ├─ main.js
      └─ node_modules/trtc-electron-sdk/...
```

固定路径：

```text
exe = <resource_dir>\jax-rtc-sidecar-runtime\jax-rtc-sidecar.exe
app = <resource_dir>\jax-rtc-sidecar-runtime\resources\app
ca  = <resource_dir>\certs\ca.crt
```

`app` 是 Electron 内部发现的 embedded app，不是 supervisor 参数。生产命令不得包含 `.`, `main.js`, `resources/app` 或其绝对路径。

`resources/app/node_modules/electron` 仍是禁止项。应用通过 packaged runtime 提供的内建 `electron` 模块运行，不复制 npm Electron runtime 到应用依赖树。

### D2. argv 契约

Tauri 必须使用非 shell API 等价执行：

```text
Command::new(<absolute exe>)
  .args(["--role=sidecar"])
```

生产 main process 预期：

```text
process.defaultApp === undefined
process.argv[0] === <absolute exe>
process.argv[1] === "--role=sidecar"
```

`sidecar/main.js` 的 `process.argv.slice(1)` 因此得到唯一业务参数。每个业务参数必须保持单 argv 元素的 `--key=value` 形式，不允许改成 `--key value`。

不添加 POSIX 风格的 `--` 分隔符。Electron 的 packaged 启动文档未将其定义为 app 参数隔离契约，项目解析器也不需要它。固定 allowlist 和 `Command::args` 已提供边界，不经过 `cmd.exe`、PowerShell 或字符串拼接。

开发命令 `electron . --role=sidecar` 保持不变，但不得作为 production spawn 的依据。开发态 `process.defaultApp === true`，argv 中会多出 app path；当前 allowlist 过滤能忽略该项。

### D3. cwd 契约

`current_dir` 不是 Electron 发现 embedded app、DLL 或 ICU 的机制，启动正确性不得依赖 cwd。`sidecar/main.js` 使用 `__dirname` 定位 `index.html`，也不需要宿主 cwd。

为消除 Tauri 安装路径、快捷方式和测试运行器带来的 cwd 漂移，supervisor 应设置：

```text
current_dir = <resource_dir>\jax-rtc-sidecar-runtime
```

这是确定性防护，不是修复入口的手段。packaged 回归测试还必须从一个无关 cwd 启动 exe，以证明 app 发现仍是 exe-relative。

### D4. 子进程环境契约

supervisor 必须在 spawn 前构造如下 child environment：

```text
remove ELECTRON_RUN_AS_NODE
remove NODE_OPTIONS
set NODE_EXTRA_CA_CERTS=<absolute resource_dir>\certs\ca.crt
set VOICE_SIDECAR_CREDENTIAL=<Credential Manager active slot secret>
```

规则：

- `ELECTRON_RUN_AS_NODE` 必须删除；值为 `1` 时 Electron main API 不成立。
- `NODE_OPTIONS` 必须整体删除；不得试图解析、过滤后继承宿主内容。
- `NODE_EXTRA_CA_CERTS` 必须在删除 `NODE_OPTIONS` 后显式覆盖为已打包 CA 的绝对路径，保留 ADR-020 的 fail-closed 信任契约。
- `VOICE_SIDECAR_CREDENTIAL` 继续仅注入 child env，不写命令行、日志、文件或父进程全局环境。
- 其他宿主环境变量维持现状；本 ADR 不扩大清洗范围。

Rust spawn 链的规范形态：

```rust
let child = cmd
    .args(&self.spec.args)
    .current_dir(&self.spec.integrity.runtime_dir)
    .env_remove("ELECTRON_RUN_AS_NODE")
    .env_remove("NODE_OPTIONS")
    .env(SIDECAR_CREDENTIAL_ENV, launch.expose())
    .env("NODE_EXTRA_CA_CERTS", &self.spec.ca_cert_path)
    .stdin(Stdio::piped())
    .stdout(Stdio::null())
    .stderr(stderr)
    .spawn();
```

### D5. 不可退让的安全与运维不变量

以下机制原样保留：

- `validate -> Credential Manager load -> revalidate -> spawn` 的抗 TOCTOU 顺序。
- manifest 编译期 digest、exe SHA-256、runtime 文件闭集与逐文件 hash。
- provenance 的 `build_input_file`、`installed_file`、Electron/TRTC 版本和 target triple。
- Credential Manager 三槽事务、active slot 读取和 child-only secret。
- Windows `CREATE_NO_WINDOW` (`0x0800_0000`)。
- stdin `shutdown\n` 优雅退出、超时 kill、watchdog 有界重启。
- `sidecar-stderr.log` 落盘，启动失败不得静默。

本 ADR 不增加 shell 权限、不恢复 Electron externalBin、不放宽任意参数、不把 credential 放入 argv。

### D6. 前端图标依赖锁定

本启动修复不需要新增 UI。全项目前端功能图标继续唯一使用 `lucide-react@0.469.0` 的 SVG 图标，不引入第二套图标库，不使用 emoji 充当功能图标。视觉实现禁止紫色到粉色渐变。
## Implementation map

后续实施者仅按以下文件和函数修改：
1. `pet-ui/src-tauri/src/sidecar.rs`, `SidecarSupervisor::spawn_with_credential`
   - 在 `.args(...)` 后加入 `.current_dir(&self.spec.integrity.runtime_dir)`。
   - 加入 `.env_remove("ELECTRON_RUN_AS_NODE")` 和 `.env_remove("NODE_OPTIONS")`。
   - 保持 credential、CA、`CREATE_NO_WINDOW`、stderr、stdin 与 stdout 策略不变。
2. `pet-ui/src-tauri/src/main.rs`, `resolve_sidecar_spec`
   - 保持 `binary_path = runtime_dir.join(SIDECAR_BIN)`。
   - 保持唯一生产参数 `SIDECAR_ARGS = ["--role=sidecar"]`。
   - 保持 `ca_cert_path = resource_dir/certs/ca.crt`。
3. `pet-ui/src-tauri/tests/sidecar_supervisor.rs`
   - 增加 child 环境回归：父进程预置两项污染变量，stub 断言 child 中两者均不存在。
   - 断言 `NODE_EXTRA_CA_CERTS` 与 child-only credential 存在且值来源正确。
   - 断言固定 argv 不含 `.`, `main.js`, `resources/app` 或 `--`。
4. `scripts/test/sidecar-package.test.js`
   - 保持并加强 packaged 布局断言：branded exe、`resources/app/package.json`、入口文件和 runtime 必需文件存在。
   - 断言 `resources/default_app.asar` 与 `resources/app/node_modules/electron` 不存在。
5. 新增 Windows packaged 启动回归夹具，放在现有 sidecar/package 测试职责下，不放入入口文件
   - 构建真实 runtime 后从无关 cwd 执行 branded exe。
   - 注入污染父环境，spawn 层净化后验证进程存活、Electron main 可用、argv 精确匹配。
   - 捕获 stderr 并拒绝 `bad option`、`Cannot find module 'electron'`、DLL/ICU 和 `ipcMain` 错误。

`sidecar/main.js` 与 `sidecar/config.js` 不需要为 Gate A 改动。若未来重写参数解析，必须用 `process.defaultApp` 区分开发和 packaged 形态，并保留 `--key=value` allowlist。

## Acceptance

所有命令从仓库根目录执行。构建命令必须先清除宿主 Node/Electron 污染；这只影响当前命令会话。

```powershell
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
Remove-Item Env:NODE_OPTIONS -ErrorAction SilentlyContinue
npm --prefix .\pet-ui run build:sidecar
npm --prefix .\pet-ui run verify:sidecar
node --test .\scripts\test\sidecar-package.test.js
cargo test --manifest-path .\pet-ui\src-tauri\Cargo.toml --test sidecar_supervisor
```

布局门禁：

```powershell
$runtime = Resolve-Path .\pet-ui\src-tauri\binaries\jax-rtc-sidecar-runtime
Test-Path "$runtime\jax-rtc-sidecar.exe"
Test-Path "$runtime\resources\app\package.json"
Test-Path "$runtime\resources\default_app.asar"
Test-Path "$runtime\resources\app\node_modules\electron"
```

期望依次为 `True`, `True`, `False`, `False`。

独立 packaged smoke，从无关 cwd 启动，不传 app path 或 `--`：

```powershell
$runtime = Resolve-Path .\pet-ui\src-tauri\binaries\jax-rtc-sidecar-runtime
$env:NODE_OPTIONS = '--require=definitely-missing.cjs --use-system-ca'
$env:ELECTRON_RUN_AS_NODE = '1'
node .\_manual_start_test.js
```

现有 `_manual_start_test.js` 会为 child 删除两项污染变量，设置 CA、credential、`cwd=runtime`，并仅传 `--role=sidecar`。回归夹具落地后，还要增加无关 cwd 变体。通过标准是 6 秒内不异常退出，stderr 不含以下文本：

```text
bad option
Cannot find module 'electron'
ipcMain
ffmpeg.dll
Invalid file descriptor to ICU data
```

真实 Tauri 监督验收：

```powershell
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
Remove-Item Env:NODE_OPTIONS -ErrorAction SilentlyContinue
npm --prefix .\pet-ui run tauri build
Get-Process jax-rtc-sidecar -ErrorAction Stop | Select-Object Id, ProcessName, Path
Get-Content "$env:LOCALAPPDATA\com.jax.pet\logs\sidecar-stderr.log" -Tail 200
```

最终 Gate A 必须同时具备：

- Tauri 从 Credential Manager active slot 拉起唯一 branded sidecar 进程。
- 进程命令行只含 exe 与受控业务参数，不含 app path、credential 或 `--`。
- integrity/provenance/runtime 闭集校验通过。
- stderr 无上述 Electron bootstrap 错误。
- sidecar 完成 TRTC SDK 初始化并进入目标房间；仅有进程存活不算 Gate A 通过。
- 主程序退出后 sidecar 按 supervisor 生命周期退出，无孤儿进程。

当前仓库 runtime 目录尚未形成可执行的新鲜 packaged 产物，且 `_build_out.txt` 记录的最近构建被宿主 safe-delete shim 阻断。因此本 ADR 只完成架构裁决，Gate A 仍为 blocked，必须由实施任务执行上述真实验收后解除。

## Consequences

正面后果：

- 生产启动只剩一种 exe、app 发现和 argv 形态，停止试错式追加参数。
- 将历史三类错误统一归因到可测试的 child environment 边界。
- 保留 ADR-017/019/020/024/025 的监督、credential、TLS、品牌化和供应链约束。

负面后果：

- supervisor 必须主动抵抗宿主环境污染，不能依赖安装环境天然干净。
- packaged Electron smoke 需要 Windows 真实 runtime，普通 Node 单元测试不能替代。
- 设置确定性 cwd 后仍需无关 cwd 的独立测试，避免团队误把 cwd 当作 app 发现机制。

## Out of scope

- 不修改移动端进房状态机、后端签名服务或 TRTC 业务协议。
- 不改用 electron-builder、Electron Forge 或另一套桌面框架。
- 不新增 `resources/app.asar`；当前目录形态继续由 runtime 闭集保护。
- 不处理代码签名与安装器品牌元数据。
- 不新增 API，故无 OpenAPI sidecar 变更。
## Related ADRs

ADR-017, ADR-019, ADR-020, ADR-024, ADR-025。
