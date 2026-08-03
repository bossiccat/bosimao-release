//! 窗口控制：点击穿透切换（宠物在贴边时不拦截鼠标，交互时恢复）

use tauri::Manager;

/// 切换点击穿透：true = 鼠标穿透（监控态贴边），false = 正常交互（点击宠物/面板）
#[tauri::command]
pub fn set_ignore_cursor_events(app: tauri::AppHandle, ignore: bool) -> Result<(), String> {
    let win = app
        .get_webview_window("pet")
        .ok_or("pet window not found")?;
    win.set_ignore_cursor_events(ignore).map_err(|e| e.to_string())
}
