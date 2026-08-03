//! 系统托盘：显示/隐藏宠物、打开监控面板、退出

use tauri::{AppHandle, Manager, tray::TrayIconBuilder, menu::{Menu, MenuItem, PredefinedMenuItem}};

pub fn setup_tray(app: &tauri::App) -> tauri::Result<()> {
    let show_i = MenuItem::with_id(app, "show", "显示/隐藏宠物", true, None::<&str>)?;
    let quit_i = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(app, &[&show_i, &sep, &quit_i])?;

    // 图标可选：default_window_icon 可能为 None（未配置窗口图标时），不 panic
    let tray_icon = app.default_window_icon().cloned();
    let mut tray_builder = TrayIconBuilder::new()
        .icon(tray_icon.as_ref())
        .menu(&menu)
        .show_menu_on_left_click(false);
    tray_builder = tray_builder.on_menu_event(|app, event| match event.id.as_ref() {
        "quit" => app.exit(0),
        "show" => toggle_window(app),
        _ => {}
    });
    let _tray = tray_builder
        .on_tray_icon_event(|tray, event| {
            if let tauri::tray::TrayIconEvent::Click { button: tauri::tray::MouseButton::Left, .. } = event {
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
