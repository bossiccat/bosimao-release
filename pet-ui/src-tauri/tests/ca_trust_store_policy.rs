//! ca_trust_windows 集成测试 — CA 安装库选择策略（2026-09-03 根因修复）。
//!
//! 背景：CaConfirm「安装失败」根因 = 安装走 CURRENT_USER\Root，Windows 对该库
//! 写入强制确认弹窗，非交互环境报「此操作中不允许使用 UI」必然失败。
//! 修复契约：
//! 1. install 优先写 LOCAL_MACHINE\Root（无弹窗、全机生效）；
//!    仅当机器库写入失败（如非提权普通用户）才回落 CURRENT_USER\Root。
//! 2. 注册表留双值：`ca_thumbprint`（SHA-1 大写十六进制）+ `ca_store`
//!    （"LocalMachine" | "CurrentUser"）供判重与卸载定位。
//! 3. is_ca_installed 按库标记判重；无标记旧记录（历史版本兼容）两库都查。
//! 4. 幂等：同 thumbprint 已在目标库 → 跳过添加、仅刷新注册表记录。
//!
//! 测试环境注意：本测试操作真实 HKCU\Software\JaxPet 键与机器根证书库。
//! 结束时清理注册表值（证书保留在机器库无害：thumbprint 判重幂等）。

#![cfg(windows)]

use jax_pet::ca_trust::{install_current_user_root_ca, is_ca_installed};

const REG_KEY: &str = r"Software\JaxPet";

fn read_reg(name: &str) -> Option<String> {
    let key = winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER)
        .open_subkey(REG_KEY)
        .ok()?;
    key.get_value::<String, _>(name).ok()
}

fn delete_reg(name: &str) {
    if let Ok(key) = winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER)
        .open_subkey_with_flags(REG_KEY, winreg::enums::KEY_READ | winreg::enums::KEY_WRITE)
    {
        let _ = key.delete_value(name);
    }
}

/// 定位打包资源目录：cargo test 的工作目录为 src-tauri/，ca.crt 相对仓库为
/// `pet-ui/src-tauri/certs/ca.crt`（构建资源）或安装目录。优先用仓库内文件。
fn resource_dir() -> std::path::PathBuf {
    // CARGO_MANIFEST_DIR = pet-ui/src-tauri；resource_dir 契约是其自身根。
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn cleanup_registry() {
    delete_reg("ca_thumbprint");
    delete_reg("ca_store");
}

#[test]
fn install_prefers_local_machine_store_and_is_idempotent() {
    cleanup_registry();

    // 1. 安装成功且返回大写 SHA-1 thumbprint。
    let thumb = install_current_user_root_ca(&resource_dir())
        .expect("install 应成功（LM\\Root 无弹窗路径）");
    assert_eq!(thumb.len(), 40, "thumbprint 应为 40 位十六进制");
    assert!(thumb.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_lowercase()));

    // 2. 注册表留双值：thumbprint + 库标记 LocalMachine。
    assert_eq!(
        read_reg("ca_thumbprint").as_deref(),
        Some(thumb.as_str()),
        "ca_thumbprint 应写入注册表"
    );
    let store_marker = read_reg("ca_store")
        .expect("ca_store 库标记应写入注册表（新增契约）");
    assert!(
        store_marker == "LocalMachine" || store_marker == "CurrentUser",
        "ca_store 标记应为 LocalMachine/CurrentUser，实际: {store_marker}"
    );
    assert_eq!(
        store_marker, "LocalMachine",
        "本机为提权环境，应优先写 LocalMachine 库"
    );

    // 3. 判重：安装后 is_ca_installed 必须为 true（注册表 + 库命中双条件）。
    assert!(
        is_ca_installed(),
        "is_ca_installed 应为 true（含无标记/机器库命中场景）"
    );

    // 4. 幂等：重复安装返回同 thumbprint，不报错不弹窗。
    let thumb2 = install_current_user_root_ca(&resource_dir())
        .expect("重复 install 应幂等成功");
    assert_eq!(thumb, thumb2, "幂等安装应返回同 thumbprint");
    assert!(is_ca_installed(), "重复安装后判重仍为 true");

    // 清理注册表（证书留在机器库，下次 install 幂等命中）。
    cleanup_registry();
}
