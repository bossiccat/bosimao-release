//! 窗口控制：点击穿透切换与 sidecar 状态查询

use std::sync::Mutex;

use jax_pet::sidecar::{SidecarState, SidecarSupervisor};
use tauri::Manager;

/// 切换点击穿透：true = 鼠标穿透（监控态贴边），false = 正常交互（点击宠物/面板）
#[tauri::command]
pub fn set_ignore_cursor_events(app: tauri::AppHandle, ignore: bool) -> Result<(), String> {
    let win = app
        .get_webview_window("pet")
        .ok_or("pet window not found")?;
    win.set_ignore_cursor_events(ignore)
        .map_err(|e| e.to_string())
}

/// 查询 sidecar 状态（前端/诊断用，只读）
#[tauri::command]
pub fn get_sidecar_status(app: tauri::AppHandle) -> Result<String, String> {
    let sup = app.state::<Mutex<SidecarSupervisor>>();
    let sup = sup.lock().map_err(|e| e.to_string())?;
    Ok(match sup.state() {
        SidecarState::Running => "running".to_string(),
        SidecarState::Stopped => "stopped".to_string(),
    })
}
