//! 贾克斯模式桌宠入口：透明窗口 + 系统托盘 + sidecar 监督（ADR-017）
// 2026-08-13 弹窗修复：release 构建使用 GUI 子系统（禁止黑色命令窗）；
// debug 保留 console 便于日志。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod tray;
mod window;

use std::sync::Mutex;
use std::time::{Duration, Instant};

use jax_pet::credential_windows::WindowsCredentialStore;
use jax_pet::sidecar::{IntegritySpec, SidecarSpec, SidecarSupervisor};
use jax_pet::sidecar_credential::SidecarCredentialService;
use jax_pet::watchdog::{
    drive_restart_policy, HealthWindow, Watchdog, WatchdogAction, WatchdogConfig,
};
use tauri::Manager;

const SIDECAR_BIN: &str = "jax-rtc-sidecar.exe";
const SIDECAR_SHA_FILE: &str = "jax-rtc-sidecar.exe.sha256";
const SIDECAR_MANIFEST_FILE: &str = "jax-rtc-sidecar.provenance.json";
const SIDECAR_RUNTIME_DIR: &str = "jax-rtc-sidecar-runtime";
const SIDECAR_ARGS: [&str; 1] = ["--role=sidecar"];
const WATCHDOG_HEALTHY_AFTER: Duration = Duration::from_secs(30);
const COMPILED_MANIFEST_SHA256: &str = env!("JAX_SIDECAR_MANIFEST_SHA256");

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let spec = resolve_sidecar_spec(app);
            let mut supervisor = SidecarSupervisor::new(spec);
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
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

/// 解析 externalBin 产物路径与哈希文件。
/// 产物或哈希缺失时保持 fail-closed：spec 仍可构造，但 start() 会因
/// BinaryMissing / HashMismatch 拒绝启动（ADR-017 最小权限）。
fn resolve_sidecar_spec(app: &tauri::App) -> SidecarSpec {
    let dir = app
        .path()
        .resource_dir()
        .unwrap_or_else(|_| std::path::PathBuf::from("."));
    let binary_path = dir.join(SIDECAR_BIN);
    let runtime_dir = dir.join(SIDECAR_RUNTIME_DIR);
    let hash_path = runtime_dir.join(SIDECAR_SHA_FILE);
    let expected_sha256 = std::fs::read_to_string(&hash_path)
        .map(|s| s.trim().to_string())
        .unwrap_or_default();
    SidecarSpec {
        binary_path,
        expected_sha256,
        integrity: IntegritySpec {
            manifest_path: runtime_dir.join(SIDECAR_MANIFEST_FILE),
            expected_manifest_sha256: COMPILED_MANIFEST_SHA256.to_string(),
            runtime_dir,
        },
        args: SIDECAR_ARGS.iter().map(|s| s.to_string()).collect(),
        graceful_timeout: Duration::from_secs(5),
        kill_timeout: Duration::from_secs(3),
    }
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
