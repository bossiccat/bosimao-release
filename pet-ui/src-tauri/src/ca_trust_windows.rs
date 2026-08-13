#![cfg(windows)]

//! ca_trust_windows.rs — Windows「当前用户」受信根库安装实现（ADR-020 A2）。
//!
//! 幂等把 ca.crt 装进当前用户「受信任的根证书颁发机构」库（CURRENT_USER，无需管理员），
//! 按 SHA-1 thumbprint 判重，并把 thumbprint 写入 HKCU\Software\JaxPet\ca_thumbprint
//! 供卸载清理。卸载按该 thumbprint 用 CertDeleteCertificateFromStore 干净移除。

use std::path::Path;

use windows::Win32::Security::Cryptography::{
    CertAddCertificateContextToStore, CertCloseStore, CertCreateCertificateContext,
    CertDeleteCertificateFromStore, CertFindCertificateInStore, CertFreeCertificateContext,
    CertOpenStore, CryptHashCertificate, CALG_SHA1, CERT_CONTEXT, CERT_FIND_SHA1_HASH,
    CERT_OPEN_STORE_FLAGS, CERT_STORE_ADD_REPLACE_EXISTING, CERT_STORE_PROV_SYSTEM_W,
    CERT_SYSTEM_STORE_CURRENT_USER, HCERTSTORE, X509_ASN_ENCODING,
};

const ROOT_STORE_NAME: &str = "Root";
const REG_KEY: &str = r"Software\JaxPet";
const REG_VALUE: &str = "ca_thumbprint";
const SHA1_LEN: usize = 20;

/// CertFindCertificateInStore(CERT_FIND_SHA1_HASH) 所需的 CRYPT_HASH_BLOB 布局。
/// windows-rs 0.61 未导出 CRYPTOAPI_BLOB/CRYPT_HASH_BLOB，按稳定 C ABI 手工声明。
#[repr(C)]
struct CryptHashBlob {
    cb_data: u32,
    pb_data: *const u8,
}

/// 幂等安装当前用户根 CA，返回 SHA-1 thumbprint 大写十六进制串。
pub fn install_current_user_root_ca(resource_dir: &Path) -> Result<String, String> {
    let ca_path = resource_dir.join("certs").join("ca.crt");
    let der = std::fs::read(&ca_path)
        .map_err(|e| format!("读取 CA 证书失败 {}: {e}", ca_path.display()))?;

    let thumbprint = sha1_thumbprint(&der)?;
    let thumbprint_hex = to_hex(&thumbprint);

    // 受信面扩张：安装留痕日志，便于首启/隐私说明审计。
    eprintln!("[ca_trust] installing self-signed root CA thumbprint {thumbprint_hex} into current-user root store");

    let store = open_root_store()?;

    // 幂等：同 thumbprint 已存在则跳过安装，仅刷新注册表记录。
    if store_has_thumbprint(store, &thumbprint) {
        write_thumbprint_registry(&thumbprint_hex)?;
        unsafe {
            let _ = CertCloseStore(Some(store), 0);
        }
        return Ok(thumbprint_hex);
    }

    let cert = unsafe { CertCreateCertificateContext(X509_ASN_ENCODING, &der) };
    if cert.is_null() {
        unsafe {
            let _ = CertCloseStore(Some(store), 0);
        }
        return Err("CertCreateCertificateContext 失败".to_string());
    }

    let add_result = unsafe {
        CertAddCertificateContextToStore(
            Some(store),
            cert,
            CERT_STORE_ADD_REPLACE_EXISTING,
            None,
        )
    };
    unsafe {
        let _ = CertFreeCertificateContext(Some(cert));
    }
    if let Err(e) = add_result {
        unsafe {
            let _ = CertCloseStore(Some(store), 0);
        }
        return Err(format!("安装根证书到当前用户根库失败: {e}"));
    }

    write_thumbprint_registry(&thumbprint_hex)?;
    unsafe {
        let _ = CertCloseStore(Some(store), 0);
    }
    Ok(thumbprint_hex)
}

/// 是否已安装当前用户根 CA（真判重：注册表 thumbprint 记录 + 根库命中）。
pub fn is_ca_installed() -> bool {
    let Some(thumbprint_hex) = read_thumbprint_registry().ok().flatten() else {
        return false;
    };
    let Ok(thumbprint) = from_hex(&thumbprint_hex) else {
        return false;
    };
    let Ok(store) = open_root_store() else {
        return false;
    };
    let present = store_has_thumbprint(store, &thumbprint);
    unsafe {
        let _ = CertCloseStore(Some(store), 0);
    }
    present
}

/// 卸载清理（联动阶段 D4）：按注册表记录的 thumbprint 从当前用户根库删除。
pub fn remove_current_user_root_ca() -> Result<(), String> {
    let Some(thumbprint_hex) = read_thumbprint_registry()? else {
        return Ok(()); // 无记录：无可清理。
    };
    let thumbprint = from_hex(&thumbprint_hex)?;
    let store = open_root_store()?;
    if let Some(found) = find_thumbprint(store, &thumbprint) {
        unsafe {
            CertDeleteCertificateFromStore(found)
                .map_err(|e| format!("删除根证书失败: {e}"))?;
        }
    }
    unsafe {
        let _ = CertCloseStore(Some(store), 0);
    }
    delete_thumbprint_registry()?;
    Ok(())
}

fn open_root_store() -> Result<HCERTSTORE, String> {
    let name = wide(ROOT_STORE_NAME);
    unsafe {
        CertOpenStore(
            CERT_STORE_PROV_SYSTEM_W,
            X509_ASN_ENCODING,
            None,
            CERT_OPEN_STORE_FLAGS(CERT_SYSTEM_STORE_CURRENT_USER),
            Some(name.as_ptr() as *const std::ffi::c_void),
        )
        .map_err(|e| format!("打开当前用户根证书库失败: {e}"))
    }
}

fn find_thumbprint(store: HCERTSTORE, target: &[u8; SHA1_LEN]) -> Option<*mut CERT_CONTEXT> {
    let blob = CryptHashBlob {
        cb_data: target.len() as u32,
        pb_data: target.as_ptr(),
    };
    let found = unsafe {
        CertFindCertificateInStore(
            store,
            X509_ASN_ENCODING,
            0,
            CERT_FIND_SHA1_HASH,
            Some(&blob as *const CryptHashBlob as *const std::ffi::c_void),
            None,
        )
    };
    if found.is_null() {
        None
    } else {
        Some(found)
    }
}

fn store_has_thumbprint(store: HCERTSTORE, target: &[u8; SHA1_LEN]) -> bool {
    match find_thumbprint(store, target) {
        Some(found) => {
            unsafe {
                let _ = CertFreeCertificateContext(Some(found));
            }
            true
        }
        None => false,
    }
}

fn sha1_thumbprint(der: &[u8]) -> Result<[u8; SHA1_LEN], String> {
    let mut hash = [0u8; SHA1_LEN];
    let mut hash_len = hash.len() as u32;
    unsafe {
        CryptHashCertificate(
            None,
            CALG_SHA1,
            0,
            der,
            Some(hash.as_mut_ptr()),
            &mut hash_len,
        )
        .map_err(|e| format!("计算 CA thumbprint 失败: {e}"))?;
    }
    Ok(hash)
}

fn write_thumbprint_registry(thumbprint_hex: &str) -> Result<(), String> {
    let (key, _disposition) = winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER)
        .create_subkey(REG_KEY)
        .map_err(|e| format!("创建注册表键 {REG_KEY} 失败: {e}"))?;
    key.set_value(REG_VALUE, &thumbprint_hex)
        .map_err(|e| format!("写入 {REG_KEY}\\{REG_VALUE} 失败: {e}"))
}

fn read_thumbprint_registry() -> Result<Option<String>, String> {
    let key = winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER).open_subkey(REG_KEY);
    let Ok(key) = key else {
        return Ok(None); // 键不存在：从未安装过，无可清理。
    };
    match key.get_value::<String, _>(REG_VALUE) {
        Ok(value) => Ok(Some(value)),
        Err(_) => Ok(None), // 键值缺失视为无记录。
    }
}

fn delete_thumbprint_registry() -> Result<(), String> {
    let key = winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER)
        .open_subkey_with_flags(
            REG_KEY,
            winreg::enums::KEY_READ | winreg::enums::KEY_WRITE,
        )
        .map_err(|e| format!("打开注册表键 {REG_KEY} 失败: {e}"))?;
    key.delete_value(REG_VALUE)
        .map_err(|e| format!("删除 {REG_KEY}\\{REG_VALUE} 失败: {e}"))
}

fn to_hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02X}"));
    }
    out
}

fn from_hex(hex: &str) -> Result<[u8; SHA1_LEN], String> {
    if hex.len() != SHA1_LEN * 2 {
        return Err(format!("thumbprint 长度非法: {hex}"));
    }
    let mut out = [0u8; SHA1_LEN];
    for (i, chunk) in hex.as_bytes().chunks_exact(2).enumerate() {
        let hi = (chunk[0] as char).to_digit(16).ok_or("thumbprint 含非法十六进制字符")?;
        let lo = (chunk[1] as char).to_digit(16).ok_or("thumbprint 含非法十六进制字符")?;
        out[i] = ((hi << 4) | lo) as u8;
    }
    Ok(out)
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}
