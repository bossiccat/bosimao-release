# ADR-017: Tauri externalBin 只监督唯一 Node/Electron RTC sidecar

## Status: Accepted (2026-08-07)

## Background

Windows 端需要 TRTC Electron SDK 的运行环境，同时需要 Tauri 的托盘、自启、单实例和安装包能力。若 Rust 与 Node/Electron 各实现一套 RTC adapter，会形成协议漂移、双 owner 和不可审计的媒体路径。

## Decision

Windows RTC adapter 的唯一实现形态是 Tauri `externalBin` 托管的 Node/Electron sidecar：

- `pet-ui/src-tauri/src/sidecar.rs` 只负责产物存在性与哈希、固定参数启动、单实例、watchdog、优雅退出和超时终止；不链接 TRTC、不处理 PCM。
- `sidecar/main.js` 只管理 Electron 生命周期。
- `sidecar/rtc.js` 是唯一 TRTC SDK adapter。
- `sidecar/audio.js` 是唯一格式 adapter。
- `sidecar/bridge.js` 只连接 localhost rtc_bridge。
- capability 只允许指定 externalBin 与固定参数；禁止泛化 shell。

Node/Electron 候选为 Electron 31.7.7 与 TRTC Electron SDK 13.4.802-beta.3，但未放行。必须通过干净安装、`npm ls`、原生二进制存在、运行时版本、哈希、实际注入签名、进程退出和 Android 真机门禁。失败时回到架构变更流程，禁止实现者自行切换 Rust/native adapter。

## Consequences

正面后果：RTC 与 PCM 责任唯一；Tauri 权限保持最小；watchdog、单实例和退出清理可独立测试。

负面后果：需要打包 Node/Electron 产物并带目标 triple；发布体积增大；候选 SDK 未验证前商业 Release 保持失败。

## Alternatives

- Rust 原生 RTC adapter：拒绝作为并行或自动替换方案，除非重新走架构决策。
- Electron 取代 Tauri 宿主：拒绝，扩大现有迁移范围。
- Tauri 泛化 shell 启动任意命令：拒绝，违反最小权限。

## Related ADRs

ADR-013、ADR-015。
