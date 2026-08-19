//! 贾克斯模式桌宠入口：透明窗口 + 系统托盘 + sidecar 监督（ADR-017）
// 2026-08-13 弹窗修复：release 构建使用 GUI 子系统（禁止黑色命令窗）；
// debug 保留 console 便于日志。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod crash_report;
mod tray;
mod window;

use std::sync::Mutex;
use std::time::{Duration, Instant};

use jax_pet::credential::CredentialProvider;
use jax_pet::credential_windows::WindowsCredentialStore;
use jax_pet::sidecar::{SidecarSpec, SidecarSupervisor};
use jax_pet::sidecar_credential::SidecarCredentialService;
use jax_pet::sidecar_runtime_pointer::{resolve_sidecar_runtime, GenerationLease, ResolverError};
use jax_pet::watchdog::{
    drive_restart_policy, HealthWindow, Watchdog, WatchdogAction, WatchdogConfig,
};
use tauri::{Emitter, Manager};

const SIDECAR_RUNTIME_DIR: &str = "jax-rtc-sidecar-runtime";
const SIDECAR_ARGS: [&str; 1] = ["--role=sidecar"];
const WATCHDOG_HEALTHY_AFTER: Duration = Duration::from_secs(30);
const COMPILED_MANIFEST_SHA256: &str = env!("JAX_SIDECAR_MANIFEST_SHA256");

fn main() {
    // E-1：尽早注册全局 panic hook，任何后续 panic 都先落盘崩溃现场再走默认 hook。
    // 日志目录 setup 阶段注入 app_log_dir；注册时机早于 app 构建，故此处先用兜底目录。
    crash_report::install_panic_hook();

    tauri::Builder::default()
        .setup(|app| {
            // 注入真实日志目录：app_log_dir()/crash，后续运行时 panic 落盘于此。
            if let Ok(log_dir) = app.path().app_log_dir() {
                crash_report::set_log_dir(log_dir.join("crash"));
            }

            // 受信面扩张红线（ADR-020 A2 + 总监裁决）：绝不静默装自签根 CA。
            // 已安装 → 幂等跳过（不装不弹）；未安装 → emit ca-confirm-required 通知前端
            // 弹明示确认界面，等用户同意后经 install_trusted_ca 命令才真正安装。
            // setup 在 webview 加载前运行，事件可能被错过，故前端 mount 时还会用
            // is_ca_install_required 拉取一次（见 App.tsx）。
            if !jax_pet::ca_trust::is_ca_installed() {
                let _ = app.emit("ca-confirm-required", ());
            }

            let mut supervisor = match resolve_sidecar_spec(app) {
                Ok((spec, lease)) => SidecarSupervisor::new_with_lease(spec, lease),
                Err(error) => {
                    eprintln!("sidecar runtime resolve failed: {error}");
                    SidecarSupervisor::unresolved(error.to_string())
                }
            };
            let mut service = SidecarCredentialService::new(WindowsCredentialStore::sidecar());
            if let Err(error) = service.start_initial(&mut supervisor) {
                eprintln!("sidecar initial start blocked: {error:?}");
            }
            app.manage(Mutex::new(supervisor));
            app.manage(Mutex::new(service));
            app.manage(Mutex::new(Watchdog::new(WatchdogConfig {
                max_restarts: 3,
                base_backoff: Duration::from_secs(1),
                max_backoff: Duration::from_secs(30),
            })));
            tray::setup_tray(app)?;
            spawn_watchdog(app.handle().clone());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            window::set_ignore_cursor_events,
            window::get_sidecar_status,
            install_trusted_ca,
            is_ca_install_required,
            get_owner_credential,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

/// 用户确认后安装自签根 CA（明示用户流程的落地动作，ADR-020 A2）。
/// 只有前端用户点击「同意并安装」后才会被调用；setup 绝不静默安装。
#[tauri::command]
fn install_trusted_ca(app: tauri::AppHandle) -> Result<String, String> {
    let resource_dir = app.path().resource_dir().map_err(|e| e.to_string())?;
    jax_pet::ca_trust::install_current_user_root_ca(&resource_dir)
}

/// 前端 mount 时拉取：是否还需弹 CA 确认（未安装 = true）。
/// 用于兜底 setup 阶段 emit 事件在 webview 加载前可能被错过的情况。
#[tauri::command]
fn is_ca_install_required() -> bool {
    !jax_pet::ca_trust::is_ca_installed()
}

/// 读取 owner credential（owner-only 隐私开关的 Bearer，ADR-022 D2/D6）。
/// fail-closed：CM 读失败/缺失一律 Err，绝不返回空串/降级。前端 privacy.ts 的
/// getOwnerToken() 捕获异常返回 null → 请求不带 Authorization → 后端 40101 禁用开关。
#[tauri::command]
fn get_owner_credential() -> Result<String, String> {
    WindowsCredentialStore::owner()
        .load_active()
        .map(|secret| secret.expose().to_string())
        .map_err(|error| format!("owner credential unavailable: {}", error.code.stable_code()))
}

/// 解析 sidecar runtime 为 immutable generation 快照（ADR-027 §3）。
/// fail-closed：current.json 缺失/截断/未知字段/错误 generation id/
/// pointer-generation-provenance 摘要不匹配/missing/extra payload/traversal/
/// symlink/reparse 一律返回结构化错误并拒绝启动，绝不构造 fallback sentinel 路径。
/// 返回值同时携带 spec 与 generation 租约，supervisor 持有租约至 child 退出。
fn resolve_sidecar_spec(
    app: &tauri::App,
) -> Result<(SidecarSpec, GenerationLease), ResolverError> {
    let dir = app
        .path()
        .resource_dir()
        .map_err(|error| ResolverError::ResourceDir(error.to_string()))?;
    let runtime_root = dir.join(SIDECAR_RUNTIME_DIR);
    let resolved = resolve_sidecar_runtime(&runtime_root, COMPILED_MANIFEST_SHA256)?;
    Ok(resolved.into_sidecar_spec(
        SIDECAR_ARGS.iter().map(|s| s.to_string()).collect(),
        dir.join("certs").join("ca.crt"),
        Duration::from_secs(5),
        Duration::from_secs(3),
    ))
}

/// watchdog 后台线程：code=0 不重启；异常退出/重启失败共享有限退避与熔断。
fn spawn_watchdog(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        let mut health = HealthWindow::new(WATCHDOG_HEALTHY_AFTER);
        loop {
            std::thread::sleep(Duration::from_secs(1));
            let (running, exit_code) = {
                let sup = app.state::<Mutex<SidecarSupervisor>>();
                let mut sup = match sup.lock() {
                    Ok(s) => s,
                    Err(_) => continue,
                };
                let running = sup.state() == jax_pet::sidecar::SidecarState::Running;
                (running, if running { sup.try_wait() } else { None })
            };
            if health.observe(running && exit_code.is_none(), Instant::now()) {
                if let Ok(mut wd) = app.state::<Mutex<Watchdog>>().lock() {
                    wd.on_healthy();
                }
            }
            let Some(code) = exit_code else { continue };
            let action = match app.state::<Mutex<Watchdog>>().lock() {
                Ok(mut wd) => wd.on_process_exit(code),
                Err(_) => continue,
            };
            drive_restart(&app, action);
        }
    });
}

fn drive_restart(app: &tauri::AppHandle, action: WatchdogAction) {
    let wd = app.state::<Mutex<Watchdog>>();
    let Ok(mut watchdog) = wd.lock() else { return };
    let final_action = drive_restart_policy(&mut watchdog, action, std::thread::sleep, || {
        let sup = app.state::<Mutex<SidecarSupervisor>>();
        let service = app.state::<Mutex<SidecarCredentialService<WindowsCredentialStore>>>();
        let result = match (sup.lock(), service.lock()) {
            (Ok(mut supervisor), Ok(mut credential_service)) => {
                credential_service.restart_after_unexpected_exit(&mut supervisor)
            }
            _ => return Err(()),
        };
        result.map_err(|_| {
            eprintln!("sidecar watchdog restart failed");
        })
    });
    if final_action == WatchdogAction::Fuse {
        eprintln!("sidecar watchdog fused");
    }
}
