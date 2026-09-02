//! 商业化 P0 修复（2026-09-02 用户投诉）：宠物窗 webview 导航锁定。
//!
//! 用户实测：宠物窗 webview 被导航到站外页面（闲鱼风控页「非法访问」），
//! 在 200x200 无边框窗口里渲染第三方页面 = 内容失控 + 品牌灾难。
//! 规则：宠物窗只允许本地受信 origin（Tauri 资产协议 + dev server），
//! 其余一律拒绝（外部链接不属于宠物窗，未来若需要应由系统浏览器打开）。

/// 判定 URL 是否允许在宠物窗 webview 内导航。
/// 白名单（fail-closed：解析失败 = 拒绝）：
/// - tauri://localhost / http://tauri.localhost / https://tauri.localhost（Tauri 2 资产协议）
/// - http://localhost:{port} / https://localhost:{port}（vite dev server，仅开发形态）
/// - about:blank 及空串
pub fn is_allowed_navigation(url: &str) -> bool {
    let url = match url::Url::parse(url) {
        Ok(u) => u,
        Err(_) => return false,
    };
    match url.scheme() {
        "tauri" | "about" => true,
        "http" | "https" => match url.host_str() {
            Some("tauri.localhost") => true,
            Some("localhost") | Some("127.0.0.1") => true,
            _ => false,
        },
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::is_allowed_navigation;

    #[test]
    fn allows_tauri_asset_scheme() {
        assert!(is_allowed_navigation("tauri://localhost/index.html"));
        assert!(is_allowed_navigation("http://tauri.localhost/index.html"));
        assert!(is_allowed_navigation("https://tauri.localhost/index.html"));
    }

    #[test]
    fn allows_local_dev_server() {
        assert!(is_allowed_navigation("http://localhost:5173/"));
        assert!(is_allowed_navigation("http://127.0.0.1:5173/src/main.tsx"));
    }

    #[test]
    fn rejects_remote_pages() {
        // 用户实测事故页：闲鱼风控页
        assert!(!is_allowed_navigation(
            "https://www.goofish.com/anti-content?x=1"
        ));
        assert!(!is_allowed_navigation("https://www.xianyu.com/"));
        // 仿冒域（localhost.evil.com 不等于 localhost）
        assert!(!is_allowed_navigation("http://localhost.evil.com/"));
        // 其它 scheme 一律拒绝
        assert!(!is_allowed_navigation("file:///C:/Windows/system32/drivers/etc/hosts"));
        assert!(!is_allowed_navigation("data:text/html,<h1>x</h1>"));
        assert!(!is_allowed_navigation("javascript:alert(1)"));
    }

    #[test]
    fn rejects_garbage_fail_closed() {
        assert!(!is_allowed_navigation(""));
        assert!(!is_allowed_navigation("not a url"));
        assert!(!is_allowed_navigation(":::"));
    }
}
