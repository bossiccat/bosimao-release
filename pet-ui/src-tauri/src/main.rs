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

const SIDECAR_RUNTIME_DIR: &str = "jrt";
// 商业化云端控制面（2026-09-08）：sidecar sign 指向 CloudBase 云托管 jax-backend，
// 与手机 App 同一控制面；不再依赖本地 backend:8000（测试隧道/本地链路全部退役）。
const SIDECAR_ARGS: [&str; 2] = [
    "--role=sidecar",
    "--sign-url=https://jax-backend-283963-7-1436773060.sh.run.tcloudbase.com",
];
const WATCHDOG_HEALTHY_AFTER: Duration = Duration::from_secs(30);
const COMPILED_MANIFEST_SHA256: &str = env!("JAX_SIDECAR_MANIFEST_SHA256");

/// 单实例互斥（RP-07 六场景验收缺陷修复，2026-08-31）：
/// 命名互斥体 `Global\JaxPet.SingleInstance.v1`，第二实例立即静默退出（exit 0）。
/// 句柄刻意不关闭：进程生命周期内持有，进程退出即由内核释放。
/// 失败 fail-closed：CreateMutex 失败视为「无法确认唯一性」→ 退出（桌宠宁可不开也不双开）。
fn acquire_single_instance() -> bool {
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{CloseHandle, ERROR_ALREADY_EXISTS, GetLastError};
    use windows::Win32::System::Threading::CreateMutexW;

    let name_wide: Vec<u16> = "Global\\JaxPet.SingleInstance.v1"
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect();
    let name = PCWSTR::from_raw(name_wide.as_ptr());
    unsafe {
        let handle = match CreateMutexW(None, false, name) {
            Ok(handle) => handle,
            Err(error) => {
                eprintln!("single-instance mutex create failed: {error}");
                return false;
            }
        };
        if GetLastError() == ERROR_ALREADY_EXISTS {
            let _ = CloseHandle(handle);
            return false;
        }
        if handle.is_invalid() {
            return false;
        }
        std::mem::forget(handle);
        true
    }
}

fn main() {
    // E-1：尽早注册全局 panic hook，任何后续 panic 都先落盘崩溃现场再走默认 hook。
    // 日志目录 setup 阶段注入 app_log_dir；注册时机早于 app 构建，故此处先用兜底目录。
    crash_report::install_panic_hook();

    if !acquire_single_instance() {
        eprintln!("jax-pet already running (single-instance); exiting");
        std::process::exit(0);
    }

    tauri::Builder::default()
        .setup(|app| {
            // 商业化 P0 修复（2026-09-02 用户投诉）：宠物窗迁移到代码构建。
            // 原 config 建窗无法挂 on_navigation —— 用户实测 webview 被导航到
            // 站外页面（闲鱼风控页）在 200x200 无边框窗里渲染 = 内容失控。
            // 迁移后导航白名单见 jax_pet::navigation（fail-closed）。
            let pet_window = tauri::WebviewWindowBuilder::new(
                app,
                "pet",
                tauri::WebviewUrl::App("index.html".into()),
            )
            .title("贾克斯 · 星核")
            .inner_size(200.0, 200.0)
            .decorations(false)
            .transparent(true)
            .always_on_top(true)
            .skip_taskbar(true)
            .resizable(false)
            .shadow(false)
            .visible(true)
            .on_navigation(|url| {
                let allowed = jax_pet::navigation::is_allowed_navigation(url.as_str());
                if !allowed {
                    eprintln!("pet webview navigation blocked: {url}");
                }
                allowed
            })
            .build()?;
            let _ = pet_window; // label "pet" 供 get_webview_window 使用

            // 商业化 P1 修复（2026-09-03）：关闭窗口 ≠ 退出应用。
            // Tauri 默认所有窗口销毁即进程退出；宠物窗无 on_window_event 防护时，
            // 任何 Alt+F4 / 外部 WM_CLOSE（含桌面级干扰程序）都会让整个 app 静默
            // 消失（v4o 实机 2-10 分钟内多次复现）。宠物窗标准行为：关闭 = 隐藏
            // 到托盘，恢复与退出入口都在托盘菜单（tray.rs show/quit）。
            {
                let win_for_close = pet_window.clone();
                pet_window.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = win_for_close.hide();
                        eprintln!("pet window close requested -> hidden to tray (close-to-hide policy)");
                    }
                });
            }

            // 注入真实日志目录：app_log_dir()/crash，后续运行时 panic 落盘于此。
            if let Ok(log_dir) = app.path().app_log_dir() {
                crash_report::set_log_dir(log_dir.join("crash"));

                // RP-07 P0（2026-09-01）：sidecar 运行期日志不得落入 immutable
                // generation 目录（会破坏完整性闭集校验）。sidecar CWD 指向
                // generation 目录是 Electron 相对路径解析所必需（见 sidecar.rs
                // spawn_with_credential 注释），不可更改；故通过环境变量把
                // logger.js 的输出重定向到 app log dir（sidecar-logs/）。
                // Command 继承父进程环境，supervisor 后续 spawn 的子进程同样生效。
                let sidecar_log_dir = log_dir.join("sidecar-logs");
                let _ = std::fs::create_dir_all(&sidecar_log_dir);
                std::env::set_var("JAX_SIDECAR_LOG_DIR", &sidecar_log_dir);
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
            let initial_start_failed = if let Err(error) = service.start_initial(&mut supervisor) {
                eprintln!("sidecar initial start blocked: {error:?}");
                true
            } else {
                false
            };
            app.manage(Mutex::new(supervisor));
            app.manage(Mutex::new(service));
            app.manage(Mutex::new(Watchdog::new(WatchdogConfig {
                max_restarts: 3,
                base_backoff: Duration::from_secs(1),
                max_backoff: Duration::from_secs(30),
            })));
            tray::setup_tray(app)?;
            spawn_watchdog(app.handle().clone());
            if initial_start_failed {
                let initial_action = app
                    .state::<Mutex<Watchdog>>()
                    .lock()
                    .map(|mut wd| wd.on_initial_start_failure())
                    .unwrap_or(WatchdogAction::None);
                spawn_initial_restart(app.handle().clone(), initial_action);
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            window::set_ignore_cursor_events,
            window::get_sidecar_status,
            hide_pet,
            set_pet_size,
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

/// 隐藏宠物窗（商业化 P0 修复 2026-09-02）：窗口内「隐藏」控件的落地动作。
/// 恢复入口 = 托盘「显示/隐藏宠物」（tray.rs toggle_window）。
#[tauri::command]
fn hide_pet(app: tauri::AppHandle) -> Result<(), String> {
    let win = app
        .get_webview_window("pet")
        .ok_or("pet window not found")?;
    win.hide().map_err(|e| e.to_string())
}

/// 内容驱动窗口尺寸（商业化 P0 修复 2026-09-02）：200x200 视口裁剪了全部
/// 340-420px 宽的面板/弹窗（CA 确认卡、监控面板、设置、错误横幅全部溢出）。
/// 前端按当前 UI 状态 invoke 本命令调整窗口，面板关闭时恢复 200x200。
#[tauri::command]
fn set_pet_size(app: tauri::AppHandle, width: f64, height: f64) -> Result<(), String> {
    let win = app
        .get_webview_window("pet")
        .ok_or("pet window not found")?;
    win.set_size(tauri::LogicalSize::new(width, height))
        .map_err(|e| e.to_string())
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

#[derive(Clone, Copy)]
enum RestartMode {
    Initial,
    UnexpectedExit,
}

/// 首次启动失败后使用与异常退出相同的受控重试预算，避免在 setup 线程阻塞。
fn spawn_initial_restart(app: tauri::AppHandle, action: WatchdogAction) {
    std::thread::spawn(move || drive_restart(&app, action, RestartMode::Initial));
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
            drive_restart(&app, action, RestartMode::UnexpectedExit);
        }
    });
}

fn drive_restart(app: &tauri::AppHandle, action: WatchdogAction, mode: RestartMode) {
    let wd = app.state::<Mutex<Watchdog>>();
    let Ok(mut watchdog) = wd.lock() else { return };
    let final_action = drive_restart_policy(&mut watchdog, action, std::thread::sleep, || {
        let sup = app.state::<Mutex<SidecarSupervisor>>();
        let service = app.state::<Mutex<SidecarCredentialService<WindowsCredentialStore>>>();
        let result = match (sup.lock(), service.lock()) {
            (Ok(mut supervisor), Ok(mut credential_service)) => match mode {
                RestartMode::Initial => credential_service.start_initial(&mut supervisor),
                RestartMode::UnexpectedExit => {
                    credential_service.restart_after_unexpected_exit(&mut supervisor)
                }
            },
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
