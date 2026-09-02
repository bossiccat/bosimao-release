#![cfg(windows)]

//! ca_trust_windows.rs — Windows 受信根库安装实现（ADR-020 A2；2026-09-03 库选择策略修订）。
//!
//! 安装策略（2026-09-03 根因修复）：
//! 1. 优先写 LOCAL_MACHINE\Root——无确认弹窗、全机生效；仅当机器库不可写
//!    （如非提权普通用户）才回落 CURRENT_USER\Root（交互环境弹窗可确认）。
//!    背景：CU\Root 写入被 Windows 强制确认弹窗，非交互会话报
//!    「此操作中不允许使用 UI」必然失败（v4n 实测）。
//! 2. ca.crt 支持 PEM 与 DER 双格式：PEM 自动剥壳转 DER（历史版本直接喂
//!    DER-only 的 CertCreateCertificateContext，对 PEM 文件必然失败——
//!    本机 certs/ca.crt 实为 PEM，此前从未真正安装成功）。
//! 3. 幂等按 SHA-1 thumbprint 判重；注册表写双值：
//!    `HKCU\Software\JaxPet\ca_thumbprint` + `ca_store`
//!    （"LocalMachine" | "CurrentUser"）供判重与卸载定位。
//!    卸载按记录定位对应库；无 ca_store 标记的旧记录两库都尝试（兼容）。

use std::path::Path;

use windows::Win32::Security::Cryptography::{
    CertAddCertificateContextToStore, CertCloseStore, CertCreateCertificateContext,
    CertDeleteCertificateFromStore, CertFindCertificateInStore, CertFreeCertificateContext,
    CertOpenStore, CryptHashCertificate, CALG_SHA1, CERT_CONTEXT, CERT_FIND_SHA1_HASH,
    CERT_OPEN_STORE_FLAGS, CERT_STORE_ADD_REPLACE_EXISTING, CERT_STORE_PROV_SYSTEM_W,
    CERT_SYSTEM_STORE_CURRENT_USER, CERT_SYSTEM_STORE_LOCAL_MACHINE,
    HCERTSTORE, X509_ASN_ENCODING,
};

const ROOT_STORE_NAME: &str = "Root";
const REG_KEY: &str = r"Software\JaxPet";
const REG_VALUE: &str = "ca_thumbprint";
const REG_STORE_VALUE: &str = "ca_store";
const STORE_MARKER_LM: &str = "LocalMachine";
const STORE_MARKER_CU: &str = "CurrentUser";
const SHA1_LEN: usize = 20;

/// CertFindCertificateInStore(CERT_FIND_SHA1_HASH) 所需的 CRYPT_HASH_BLOB 布局。
/// windows-rs 0.61 未导出 CRYPTOAPI_BLOB/CRYPT_HASH_BLOB，按稳定 C ABI 手工声明。
#[repr(C)]
struct CryptHashBlob {
    cb_data: u32,
    pb_data: *const u8,
}

/// 幂等安装根 CA：优先 LOCAL_MACHINE\Root，权限失败回落 CURRENT_USER\Root。
/// 返回 SHA-1 thumbprint 大写十六进制串。
pub fn install_current_user_root_ca(resource_dir: &Path) -> Result<String, String> {
    let ca_path = resource_dir.join("certs").join("ca.crt");
    let der = load_ca_der(&ca_path)?;

    let thumbprint = sha1_thumbprint(&der)?;
    let thumbprint_hex = to_hex(&thumbprint);

    // 受信面扩张：安装留痕日志，便于首启/隐私说明审计。
    eprintln!(
        "[ca_trust] installing self-signed root CA thumbprint {thumbprint_hex} (LocalMachine preferred, CurrentUser fallback)"
    );

    match try_install_into(CERT_SYSTEM_STORE_LOCAL_MACHINE, &der, &thumbprint_hex, STORE_MARKER_LM) {
        Ok(()) => return Ok(thumbprint_hex),
        Err(e_lm) => {
            eprintln!("[ca_trust] LocalMachine\\Root 不可用（{e_lm}），回落 CurrentUser\\Root");
        }
    }
    try_install_into(CERT_SYSTEM_STORE_CURRENT_USER, &der, &thumbprint_hex, STORE_MARKER_CU)?;
    Ok(thumbprint_hex)
}

/// 向指定系统根库幂等安装，成功后把 thumbprint + 库标记写入注册表。
fn try_install_into(
    system_flag: u32,
    der: &[u8],
    thumbprint_hex: &str,
    store_marker: &str,
) -> Result<(), String> {
    let store = open_system_root_store(system_flag)?;

    let done = |store: HCERTSTORE| -> Result<(), String> {
        write_thumbprint_registry(thumbprint_hex)?;
        write_store_registry(store_marker)?;
        unsafe {
            let _ = CertCloseStore(Some(store), 0);
        }
        Ok(())
    };

    // 幂等：同 thumbprint 已存在则跳过安装，仅刷新注册表记录。
    if store_has_thumbprint(store, &from_hex(thumbprint_hex)?) {
        return done(store);
    }

    let cert = unsafe { CertCreateCertificateContext(X509_ASN_ENCODING, der) };
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
        return Err(format!("安装根证书到系统根库失败: {e}"));
    }

    done(store)
}

/// 是否已安装根 CA（真判重：注册表 thumbprint 记录 + 对应根库命中；
/// 无 ca_store 标记的旧记录两库任一命中即可）。
pub fn is_ca_installed() -> bool {
    let Some(thumbprint_hex) = read_thumbprint_registry().ok().flatten() else {
        return false;
    };
    let Ok(thumbprint) = from_hex(&thumbprint_hex) else {
        return false;
    };
    let marker = read_store_registry().ok().flatten();
    let flags: &[u32] = match marker.as_deref() {
        Some(STORE_MARKER_LM) => &[CERT_SYSTEM_STORE_LOCAL_MACHINE],
        Some(STORE_MARKER_CU) => &[CERT_SYSTEM_STORE_CURRENT_USER],
        _ => &[CERT_SYSTEM_STORE_LOCAL_MACHINE, CERT_SYSTEM_STORE_CURRENT_USER],
    };
    for flag in flags {
        if let Ok(store) = open_system_root_store(*flag) {
            let present = store_has_thumbprint(store, &thumbprint);
            unsafe {
                let _ = CertCloseStore(Some(store), 0);
            }
            if present {
                return true;
            }
        }
    }
    false
}

/// 卸载清理（联动阶段 D4）：按注册表记录定位库删除 thumbprint 证书；
/// 无标记旧记录两库都尝试。
pub fn remove_current_user_root_ca() -> Result<(), String> {
    let Some(thumbprint_hex) = read_thumbprint_registry()? else {
        return Ok(()); // 无记录：无可清理。
    };
    let thumbprint = from_hex(&thumbprint_hex)?;
    let marker = read_store_registry().ok().flatten();
    let flags: &[u32] = match marker.as_deref() {
        Some(STORE_MARKER_LM) => &[CERT_SYSTEM_STORE_LOCAL_MACHINE],
        Some(STORE_MARKER_CU) => &[CERT_SYSTEM_STORE_CURRENT_USER],
        _ => &[CERT_SYSTEM_STORE_LOCAL_MACHINE, CERT_SYSTEM_STORE_CURRENT_USER],
    };
    for flag in flags {
        let Ok(store) = open_system_root_store(*flag) else {
            continue;
        };
        if let Some(found) = find_thumbprint(store, &thumbprint) {
            unsafe {
                CertDeleteCertificateFromStore(found)
                    .map_err(|e| format!("删除根证书失败: {e}"))?;
            }
        }
        unsafe {
            let _ = CertCloseStore(Some(store), 0);
        }
    }
    delete_thumbprint_registry()?;
    delete_store_registry()?;
    Ok(())
}

fn open_system_root_store(system_flag: u32) -> Result<HCERTSTORE, String> {
    let name = wide(ROOT_STORE_NAME);
    unsafe {
        CertOpenStore(
            CERT_STORE_PROV_SYSTEM_W,
            X509_ASN_ENCODING,
            None,
            CERT_OPEN_STORE_FLAGS(system_flag),
            Some(name.as_ptr() as *const std::ffi::c_void),
        )
        .map_err(|e| format!("打开系统根证书库失败: {e}"))
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

fn write_store_registry(store_marker: &str) -> Result<(), String> {
    let (key, _disposition) = winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER)
        .create_subkey(REG_KEY)
        .map_err(|e| format!("创建注册表键 {REG_KEY} 失败: {e}"))?;
    key.set_value(REG_STORE_VALUE, &store_marker)
        .map_err(|e| format!("写入 {REG_KEY}\\{REG_STORE_VALUE} 失败: {e}"))
}

fn read_store_registry() -> Result<Option<String>, String> {
    let key = winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER).open_subkey(REG_KEY);
    let Ok(key) = key else {
        return Ok(None);
    };
    match key.get_value::<String, _>(REG_STORE_VALUE) {
        Ok(value) => Ok(Some(value)),
        Err(_) => Ok(None), // 旧版本无 ca_store 记录。
    }
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

fn delete_store_registry() -> Result<(), String> {
    let key = winreg::RegKey::predef(winreg::enums::HKEY_CURRENT_USER)
        .open_subkey_with_flags(
            REG_KEY,
            winreg::enums::KEY_READ | winreg::enums::KEY_WRITE,
        )
        .map_err(|e| format!("打开注册表键 {REG_KEY} 失败: {e}"))?;
    key.delete_value(REG_STORE_VALUE)
        .map_err(|e| format!("删除 {REG_KEY}\\{REG_STORE_VALUE} 失败: {e}"))
}

/// 读取 ca.crt：PEM 自动剥壳转 DER；纯 DER 原样透传（向后兼容）。
fn load_ca_der(ca_path: &Path) -> Result<Vec<u8>, String> {
    let raw = std::fs::read(ca_path)
        .map_err(|e| format!("读取 CA 证书失败 {}: {e}", ca_path.display()))?;
    if let Some(der) = pem_to_der(&raw) {
        return Ok(der);
    }
    Ok(raw)
}

/// PEM（-----BEGIN CERTIFICATE----- ... -----END CERTIFICATE-----）→ DER。
/// 非 PEM 输入返回 None。
fn pem_to_der(raw: &[u8]) -> Option<Vec<u8>> {
    const BEGIN: &str = "-----BEGIN CERTIFICATE-----";
    const END: &str = "-----END CERTIFICATE-----";
    let text = std::str::from_utf8(raw).ok()?;
    let begin = text.find(BEGIN)? + BEGIN.len();
    let end = text[begin..].find(END)? + begin;
    let b64: Vec<u8> = text[begin..end]
        .bytes()
        .filter(|b| !b.is_ascii_whitespace())
        .collect();
    base64_decode(&b64).ok()
}

/// 紧凑标准 base64 解码（跳过空白，支持 '=' padding）。避免为此新增依赖。
fn base64_decode(input: &[u8]) -> Result<Vec<u8>, String> {
    fn val(b: u8) -> Result<u32, String> {
        match b {
            b'A'..=b'Z' => Ok((b - b'A') as u32),
            b'a'..=b'z' => Ok((b - b'a' + 26) as u32),
            b'0'..=b'9' => Ok((b - b'0' + 52) as u32),
            b'+' => Ok(62),
            b'/' => Ok(63),
            _ => Err(format!("base64 非法字符 {b:#x}")),
        }
    }
    let clean: Vec<u8> = input
        .iter()
        .copied()
        .filter(|b| !b.is_ascii_whitespace())
        .collect();
    let pad = clean.iter().rev().take_while(|&&b| b == b'=').count();
    let body = &clean[..clean.len() - pad];
    let mut out = Vec::with_capacity(body.len() * 3 / 4);
    for chunk in body.chunks(4) {
        let mut acc: u32 = 0;
        for (i, &b) in chunk.iter().enumerate() {
            acc |= val(b)? << (18 - 6 * i);
        }
        out.push((acc >> 16) as u8);
        if chunk.len() > 2 {
            out.push((acc >> 8) as u8);
        }
        if chunk.len() > 3 {
            out.push(acc as u8);
        }
    }
    Ok(out)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_decode_roundtrip_known_vectors() {
        assert_eq!(base64_decode(b"").unwrap(), Vec::<u8>::new());
        assert_eq!(base64_decode(b"Zg==").unwrap(), b"f".to_vec());
        assert_eq!(base64_decode(b"Zm8=").unwrap(), b"fo".to_vec());
        assert_eq!(base64_decode(b"Zm9v").unwrap(), b"foo".to_vec());
        assert_eq!(base64_decode(b"Zm9vYg==").unwrap(), b"foob".to_vec());
        assert_eq!(base64_decode(b"Zm9vYmE=").unwrap(), b"fooba".to_vec());
        assert_eq!(base64_decode(b"Zm9vYmFy").unwrap(), b"foobar".to_vec());
    }

    #[test]
    fn base64_decode_tolerates_whitespace() {
        assert_eq!(
            base64_decode(b"Zm9v\r\nYmFy\n").unwrap(),
            b"foobar".to_vec()
        );
    }

    #[test]
    fn pem_to_der_extracts_payload_and_ignores_der() {
        // 3 字节 DER 模拟：0xDE 0xAD 0xBE → base64 "3q2+"。
        let pem = b"garbage header\n-----BEGIN CERTIFICATE-----\n3q2+\n-----END CERTIFICATE-----\ntrailer";
        assert_eq!(pem_to_der(pem).unwrap(), vec![0xDE, 0xAD, 0xBE]);
        assert!(pem_to_der(b"\xDE\xAD\xBE").is_none(), "纯 DER 应返回 None 走透传");
        assert!(pem_to_der(b"-----BEGIN CERTIFICATE-----\n@@@!\n-----END CERTIFICATE-----").is_none());
    }
}
