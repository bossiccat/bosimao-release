//! 系统托盘：显示/隐藏宠物、sidecar 启动/停止、开机自启开关、退出

use std::sync::Mutex;

use jax_pet::credential_windows::WindowsCredentialStore;
use jax_pet::sidecar::{registry_autostart::RegistryAutoStart, SidecarState, SidecarSupervisor};
use jax_pet::sidecar_credential::SidecarCredentialService;
use jax_pet::watchdog::{reset_after_manual_start, Watchdog};
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::TrayIconBuilder,
    AppHandle, Manager,
};

pub fn setup_tray(app: &tauri::App) -> tauri::Result<()> {
    let show_i = MenuItem::with_id(app, "show", "显示/隐藏宠物", true, None::<&str>)?;
    let sidecar_i = MenuItem::with_id(app, "sidecar_toggle", "启动语音引擎", true, None::<&str>)?;
    let autostart_i = MenuItem::with_id(app, "autostart_toggle", "开机自启", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let quit_i = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[&show_i, &sep, &sidecar_i, &autostart_i, &sep, &quit_i],
    )?;

    // 图标可选：default_window_icon 可能为 None（未配置窗口图标时），不 panic
    let tray_icon = app.default_window_icon().cloned();
    let mut tray_builder = TrayIconBuilder::new()
        .icon(tray_icon.ok_or_else(|| tauri::Error::AssetNotFound("tray icon".into()))?)
        .menu(&menu)
        .show_menu_on_left_click(false);
    tray_builder = tray_builder.on_menu_event(|app, event| match event.id.as_ref() {
        "quit" => app.exit(0),
        "show" => toggle_window(app),
        "sidecar_toggle" => toggle_sidecar(app),
        "autostart_toggle" => toggle_autostart(app),
        _ => {}
    });
    let _tray = tray_builder
        .on_tray_icon_event(|tray, event| {
            if let tauri::tray::TrayIconEvent::Click {
                button: tauri::tray::MouseButton::Left,
                ..
            } = event
            {
                toggle_window(tray.app_handle());
            }
        })
        .build(app)?;

    Ok(())
}

fn toggle_window(app: &AppHandle) {
    if let Some(win) = app.get_webview_window("pet") {
        if win.is_visible().unwrap_or(false) {
            let _ = win.hide();
        } else {
            let _ = win.show();
            let _ = win.set_focus();
        }
    }
}

/// sidecar 启停：Running -> 优雅停止；Stopped -> 校验哈希后启动。
fn toggle_sidecar(app: &AppHandle) {
    let supervisor_state = app.state::<Mutex<SidecarSupervisor>>();
    let service_state = app.state::<Mutex<SidecarCredentialService<WindowsCredentialStore>>>();
    let (Ok(mut supervisor), Ok(mut service)) = (supervisor_state.lock(), service_state.lock())
    else {
        return;
    };
    match supervisor.state() {
        SidecarState::Running => {
            let _ = service.stop(&mut supervisor);
        }
        SidecarState::Stopped => {
            let started = service.start_initial(&mut supervisor).is_ok();
            if let Ok(mut watchdog) = app.state::<Mutex<Watchdog>>().lock() {
                reset_after_manual_start(&mut watchdog, started);
            }
        }
    }
}

/// 开机自启开关（Windows 注册表 Run 键）。
fn toggle_autostart(app: &AppHandle) {
    let mut store = RegistryAutoStart;
    let _ = jax_pet::sidecar::toggle_autostart(&mut store);
    let _ = app;
}
