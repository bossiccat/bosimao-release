# Windows Electron/TRTC sidecar 构建与校验

Windows 安装包不得复用来源不明的 `jax-rtc-sidecar-*.exe`。本仓库把 sidecar 视为构建产物，每次本地 installer build 都从 `sidecar/package-lock.json` 新鲜生成。

## 构建

在 `pet-ui` 目录执行：

```bash
npm run build:sidecar
npm run verify:sidecar
```

`npm run build:bundle` 会按顺序执行 sidecar build、sidecar verify、TypeScript 和 Vite build；Tauri 的 `beforeBuildCommand` 已锁定到该命令。`cargo build --release` 还会在 `build.rs` 再执行 verify。若构建环境无法从 PATH 找到 Node，可为 Rust 构建设置 `NODE` 为 Node 可执行文件的绝对路径。

构建器执行两次隔离安装：

1. 在 `sidecar/` 执行 `npm ci`，取得锁定的 Electron `31.7.7` 完整 Windows dist。
2. 在生成目录 `resources/app` 执行 `npm ci --omit=dev`，只安装生产依赖 `trtc-electron-sdk@13.4.802-beta.3`；验证器拒绝嵌入 `resources/app/node_modules/electron`。

生成目录位于 `pet-ui/src-tauri/binaries/`，包括目标三元组 executable 和 `jax-rtc-sidecar-runtime/`。runtime 通过 Tauri `bundle.resources` 映射到 externalBin 同目录，保留 Electron DLL、pak、locales、snapshot、`resources/app` 和 TRTC native 文件。manifest 是严格 schema 的闭集：runtime_files 枚举专用受管 runtime 目录的完整内容，Tauri 主程序和externalBin等非受管安装同级文件不进入闭集；native_files必须精确等于固定五路径，并与runtime_files对应path/hash完全相同。manifest分别记录带target triple的`build_input_file`和安装逻辑名`installed_file`。绝对路径、遍历、重复、遗漏和新增均拒绝。release `build.rs` 仅执行 verify，并将 manifest digest 作为编译期环境常量嵌入 Tauri binary；启动和 watchdog restart在同一supervisor实例内执行validate、provider load、紧邻spawn前revalidate、私有spawn。当前installed layout仍待Cargo与真实installer验证。

## Fail-closed 门

当前文档描述的是目录设计和行为门；本环境尚未证明可信 registry/TLS 下的完整可重复构建，也未产生可信 release artifact。构建或发布在以下任一条件出现时失败：

- externalBin、hash 或 provenance 缺失；
- expected hash 不是 64 位小写十六进制，或与 executable 不一致；
- sidecar lock hash、Electron 版本或 TRTC SDK 版本漂移；
- TRTC `.node`、LiteAV、FFmpeg、SoundTouch DLL 或 media server 缺失；
- Electron runtime 必需文件缺失；
- provenance 生成后任一记录文件被修改；
- `resources/app` 含 Electron devDependency。

生成物包含 package-lock、externalBin、native 文件和 runtime 文件的 SHA-256。manifest 只记录版本、相对路径和 hash，不读取或输出 credential。exe、runtime、hash 与 provenance 是本地构建产物，不提交仓库。

## 发布边界

此构建门不替代代码签名、NSIS/MSI 干净机安装、回滚演练，也不实现 fresh-install credential provision。O-018/O-020 在这些 Windows 证据完成前保持 OPEN，商业发布仍不得放行。
