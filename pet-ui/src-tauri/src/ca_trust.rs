//! ca_trust.rs — 自签 CA 信任分发门面（ADR-020 A2）。
//!
//! 把打包进 `resource_dir/certs/ca.crt` 的自签根 CA 幂等装进**当前用户**受信根库，
//! 并按 SHA-1 thumbprint 写注册表 `HKCU\Software\JaxPet\ca_thumbprint` 供卸载清理
//! （联动阶段 D4）。
//!
//! # 受信面扩张提示（红线）
//! 把自签根 CA 装入用户根库是一次「受信面扩张」：同机同用户的所有程序都会信任该 CA。
//! 因此：
//! - 安装/首启必须**明示用户**（隐私说明 + 首次安装提示），不得静默安装；
//! - 卸载时按记录的 thumbprint 干净移除（`remove_current_user_root_ca`）；
//! - 营业执照后换正式 CA = 替换打包的 ca.crt + 重装，并按旧 thumbprint 删除；
//! - `ca.key` 私钥绝不分发，本模块只读取 `ca.crt` 公钥。
//!
//! 本阶段（A）仅落地 Windows 根库安装；非 Windows 平台不装系统根库（各走各自信任链）。

use std::path::Path;

/// setup 阶段幂等安装当前用户根 CA 信任（ADR-020 A2 改动点 3）。
/// 返回已安装（或已存在）证书的 SHA-1 thumbprint 十六进制串（大写）。
#[cfg(windows)]
pub fn install_current_user_root_ca(resource_dir: &Path) -> Result<String, String> {
    crate::ca_trust_windows::install_current_user_root_ca(resource_dir)
}

/// 是否已安装当前用户根 CA（明示用户流程的幂等判重依据）。
/// 真判重：注册表有 thumbprint 记录 AND 当前用户根库命中该证书。
#[cfg(windows)]
pub fn is_ca_installed() -> bool {
    crate::ca_trust_windows::is_ca_installed()
}

/// 卸载清理：按注册表记录的 thumbprint 从当前用户根库删除（联动阶段 D4）。
#[cfg(windows)]
pub fn remove_current_user_root_ca() -> Result<(), String> {
    crate::ca_trust_windows::remove_current_user_root_ca()
}

#[cfg(not(windows))]
pub fn install_current_user_root_ca(_resource_dir: &Path) -> Result<String, String> {
    // 非 Windows 平台不装系统根库；WebView2/Linux/其他端各自处理信任链。
    Ok(String::new())
}

#[cfg(not(windows))]
pub fn is_ca_installed() -> bool {
    false
}

#[cfg(not(windows))]
pub fn remove_current_user_root_ca() -> Result<(), String> {
    Ok(())
}
