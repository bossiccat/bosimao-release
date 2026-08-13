//! jax-pet Tauri 库入口：sidecar 监督与 watchdog（ADR-017）。
//!
//! 边界：本 crate 只负责 externalBin 托管 Node/Electron sidecar 的
//! 存在性/哈希、固定参数启动、单实例、watchdog 与退出清理；
//! 不链接 TRTC SDK、不处理 PCM，不构成第二套 RTC adapter。

pub mod credential;
mod credential_transaction;
pub mod credential_windows;
#[cfg(windows)]
mod credential_windows_backend;
#[cfg(windows)]
mod credential_windows_lock;
#[cfg(all(windows, feature = "credential-test-support"))]
pub mod o020_controller;
#[cfg(all(windows, feature = "credential-test-support"))]
pub mod o020_controller_evidence;
#[cfg(all(windows, feature = "credential-test-support"))]
mod o020_controller_process;
#[cfg(all(windows, feature = "credential-test-support"))]
mod o020_crash_barrier;
#[cfg(all(windows, feature = "credential-test-support"))]
pub mod o020_handle_diag;
#[cfg(all(windows, feature = "credential-test-support"))]
pub mod o020_probe;
pub mod provision_orchestrator;
#[cfg(windows)]
mod provision_orchestrator_windows;
pub mod sidecar;
pub mod sidecar_credential;
mod sidecar_integrity;
mod sidecar_runtime_trust;
pub mod watchdog;
