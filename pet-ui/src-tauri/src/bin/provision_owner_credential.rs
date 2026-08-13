//! provision_owner_credential.rs — owner credential 首启一次性 provisioner（ADR-022 D4）。
//!
//! owner 是「桌宠 UI 自己的身份」，由本机首启自生成（CSPRNG 32B → hex 64 chars，
//! 满足 SecretString::parse_utf8 的 32–512 bytes / 无 CR/LF/NUL 校验），写入
//! Windows Credential Manager active 槽（`WindowsCredentialStore::owner()`），再幂等
//! 写入 `.env` 的 `VOICE_OWNER_CREDENTIAL=` 一行（后端启动时经 Load-Env 注入读取）。
//!
//! 契约（ADR-022 D4 + 总监实施红线）：
//! - `status() == Ready` → 幂等 exit 0（CM 已就绪），并确保 `.env` 与 CM 逐字节一致；
//!   否则 `revoke()`（幂等清槽，CredDeleteW ERROR_NOT_FOUND 视为成功）→ `provision()`。
//! - 写 `.env` 用绝对路径定位：从可执行文件所在目录向上找 `.env.example` 定位项目根，
//!   不依赖进程工作目录（服务化/脚本编排下 cwd 不可信）。
//! - 幂等更新只增改 `VOICE_OWNER_CREDENTIAL=` 一行，保留其余行原样（含行尾换行风格）。
//! - secret 零回显、不写日志、RAII Zeroize 覆盖生成 buffer；任一步失败稳定非零退出码。
//!
//! 注意：本进程无 stdout/stderr 输出，仅以退出码表达结果；启动脚本 Start-Process 无需
//! 也不应带 -WindowStyle（`windows_subsystem = "windows"` 已禁止命令窗）。
#![cfg_attr(windows, windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use getrandom::getrandom;
use jax_pet::credential::{
    CredentialProvider, CredentialStatus, SecretString, OWNER_CREDENTIAL_ENV,
};
use jax_pet::credential_windows::WindowsCredentialStore;
use zeroize::Zeroizing;

/// 稳定非零退出码（契约：只返回 0 或稳定非零码，不回显、不落盘明文）。
const EXIT_GENERATE_FAILED: u8 = 1;
const EXIT_PROVISION_FAILED: u8 = 2;
const EXIT_ENV_WRITE_FAILED: u8 = 3;
const EXIT_RESOLVE_FAILED: u8 = 4;

/// CSPRNG 32 bytes → hex 64 chars，全程 Zeroizing，drop 时清空。
/// 失败（RNG 拒绝 / 编码不可达）返回稳定码，不残留明文。
fn generate_secret() -> Result<SecretString, u8> {
    let mut raw = Zeroizing::new([0u8; 32]);
    getrandom(raw.as_mut_slice()).map_err(|_| EXIT_GENERATE_FAILED)?;
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut hex_bytes = Zeroizing::new(Vec::with_capacity(64));
    for byte in raw.iter() {
        hex_bytes.push(HEX[(byte >> 4) as usize]);
        hex_bytes.push(HEX[(byte & 0x0f) as usize]);
    }
    let inner = std::mem::take(&mut *hex_bytes);
    SecretString::parse_utf8(inner).map_err(|_| EXIT_GENERATE_FAILED)
}

/// 从可执行文件所在目录向上定位项目根：以 `.env.example` 为标记（项目根恒有该文件）。
/// 绝对路径，不依赖进程工作目录（总监红线）。
fn project_root() -> Result<PathBuf, u8> {
    let exe = std::env::current_exe().map_err(|_| EXIT_RESOLVE_FAILED)?;
    let mut dir = exe
        .parent()
        .map(Path::to_path_buf)
        .ok_or(EXIT_RESOLVE_FAILED)?;
    for _ in 0..8 {
        if dir.join(".env.example").is_file() {
            return Ok(dir);
        }
        if !dir.pop() {
            break;
        }
    }
    Err(EXIT_RESOLVE_FAILED)
}

/// 判断某行是否为 `key=...`（容忍行首空白；`VOICE_OWNER_CREDENTIAL_EXTRA=` 不匹配）。
fn is_key_line(line: &str, key: &str) -> bool {
    let trimmed = line.trim_start();
    trimmed == key || trimmed.starts_with(&format!("{key}="))
}

/// 取某行行首空白前缀（用于保留原缩进），无缩进返回空串。
fn leading_whitespace(line: &str) -> &str {
    &line[..line.len() - line.trim_start().len()]
}

/// 幂等 upsert `.env` 的 `key=value` 一行：已存在则替换该行（保留行首缩进与行尾终止符），
/// 不存在则追加；其余行（含注释、空行、各自的行尾 CRLF/LF）逐字节原样保留（P1）。
/// 读失败立即返回 Err（fail-closed，P2），绝不用空内容覆盖原文件。
fn upsert_env_line(path: &Path, key: &str, value: &str) -> Result<(), u8> {
    let raw = std::fs::read_to_string(path).map_err(|_| EXIT_ENV_WRITE_FAILED)?;
    // 追加全新行时选用的换行风格（仅作用于新增行；既有行沿用各自原始终止符）
    let append_newline = if raw.contains("\r\n") { "\r\n" } else { "\n" };

    let mut out = String::with_capacity(raw.len() + key.len() + value.len() + 2);
    let mut found = false;
    let mut rest = raw.as_str();
    while !rest.is_empty() {
        let (body, terminator, remaining) = match rest.find('\n') {
            Some(nl) => {
                let line = &rest[..nl]; // 不含 '\n'
                let terminator = if line.ends_with('\r') { "\r\n" } else { "\n" };
                let body = line.strip_suffix('\r').unwrap_or(line);
                (body, terminator, &rest[nl + 1..])
            }
            None => (rest, "", ""),
        };
        if is_key_line(body, key) {
            out.push_str(leading_whitespace(body));
            out.push_str(key);
            out.push('=');
            out.push_str(value);
            found = true;
        } else {
            out.push_str(body);
        }
        out.push_str(terminator);
        rest = remaining;
    }
    if !found {
        if !out.is_empty() && !out.ends_with('\n') {
            out.push_str(append_newline);
        }
        out.push_str(key);
        out.push('=');
        out.push_str(value);
        out.push_str(append_newline);
    }
    std::fs::write(path, out).map_err(|_| EXIT_ENV_WRITE_FAILED)
}

/// 确保 CM active 就绪：Ready 则读回既有 secret；否则生成 → revoke → provision → 读回。
fn ensure_cm_ready(store: &WindowsCredentialStore) -> Result<SecretString, u8> {
    match store.status() {
        CredentialStatus::Ready => store.load_active().map_err(|_| EXIT_PROVISION_FAILED),
        _ => {
            let fresh = generate_secret()?;
            store.revoke().map_err(|_| EXIT_PROVISION_FAILED)?;
            store.provision(fresh).map_err(|_| EXIT_PROVISION_FAILED)?;
            store.load_active().map_err(|_| EXIT_PROVISION_FAILED)
        }
    }
}

/// 确保 `.env` 的 `VOICE_OWNER_CREDENTIAL` 与 CM 逐字节一致（幂等 upsert，同值即 no-op）。
fn ensure_env_matches(secret: &SecretString) -> Result<(), u8> {
    let root = project_root()?;
    let env_path = root.join(".env");
    upsert_env_line(&env_path, OWNER_CREDENTIAL_ENV, secret.expose())
}

fn run() -> Result<(), u8> {
    let store = WindowsCredentialStore::owner();
    let secret = ensure_cm_ready(&store)?;
    ensure_env_matches(&secret)
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(code) => ExitCode::from(code),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn is_key_line_matches_exact_and_value_but_not_suffix() {
        assert!(is_key_line("VOICE_OWNER_CREDENTIAL=", "VOICE_OWNER_CREDENTIAL"));
        assert!(is_key_line("VOICE_OWNER_CREDENTIAL=abc", "VOICE_OWNER_CREDENTIAL"));
        assert!(is_key_line("  VOICE_OWNER_CREDENTIAL=abc", "VOICE_OWNER_CREDENTIAL"));
        assert!(!is_key_line("VOICE_OWNER_CREDENTIAL_EXTRA=abc", "VOICE_OWNER_CREDENTIAL"));
        assert!(!is_key_line("RELAY_TOKEN=abc", "VOICE_OWNER_CREDENTIAL"));
    }

    #[test]
    fn leading_whitespace_extracts_indent() {
        assert_eq!(leading_whitespace("  KEY=1"), "  ");
        assert_eq!(leading_whitespace("KEY=1"), "");
    }

    #[test]
    fn upsert_adds_line_when_missing_and_preserves_others() {
        let dir = std::env::temp_dir().join("jaxpet_owner_prov_test_add");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("t.env");
        std::fs::write(&path, "A=1\nB=2\n").unwrap();

        upsert_env_line(&path, "VOICE_OWNER_CREDENTIAL", "secret").unwrap();

        let out = std::fs::read_to_string(&path).unwrap();
        assert_eq!(out, "A=1\nB=2\nVOICE_OWNER_CREDENTIAL=secret\n");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn upsert_replaces_existing_line_and_preserves_others() {
        let dir = std::env::temp_dir().join("jaxpet_owner_prov_test_replace");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("t.env");
        std::fs::write(&path, "A=1\nVOICE_OWNER_CREDENTIAL=old\nB=2\n").unwrap();

        upsert_env_line(&path, "VOICE_OWNER_CREDENTIAL", "new").unwrap();

        let out = std::fs::read_to_string(&path).unwrap();
        assert_eq!(out, "A=1\nVOICE_OWNER_CREDENTIAL=new\nB=2\n");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn upsert_preserves_crlf_style() {
        let dir = std::env::temp_dir().join("jaxpet_owner_prov_test_crlf");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("t.env");
        std::fs::write(&path, "A=1\r\nB=2\r\n").unwrap();

        upsert_env_line(&path, "VOICE_OWNER_CREDENTIAL", "secret").unwrap();

        let out = std::fs::read_to_string(&path).unwrap();
        assert_eq!(out, "A=1\r\nB=2\r\nVOICE_OWNER_CREDENTIAL=secret\r\n");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn upsert_is_idempotent_for_same_value() {
        let dir = std::env::temp_dir().join("jaxpet_owner_prov_test_idem");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("t.env");
        std::fs::write(&path, "VOICE_OWNER_CREDENTIAL=secret\n").unwrap();

        upsert_env_line(&path, "VOICE_OWNER_CREDENTIAL", "secret").unwrap();

        let out = std::fs::read_to_string(&path).unwrap();
        assert_eq!(out, "VOICE_OWNER_CREDENTIAL=secret\n");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn upsert_preserves_mixed_line_terminators_per_line() {
        let dir = std::env::temp_dir().join("jaxpet_owner_prov_test_mixed");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("t.env");
        let input = "A=1\r\nB=2\nVOICE_OWNER_CREDENTIAL=old\r\nC=3\n";
        std::fs::write(&path, input).unwrap();

        upsert_env_line(&path, "VOICE_OWNER_CREDENTIAL", "new").unwrap();

        let out = std::fs::read_to_string(&path).unwrap();
        // 仅 owner 行 value 变更；每行终止符（\r\n 或 \n）逐字节保持原样
        assert_eq!(out, "A=1\r\nB=2\nVOICE_OWNER_CREDENTIAL=new\r\nC=3\n");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn upsert_read_failure_is_fail_closed_and_leaves_target_untouched() {
        let dir = std::env::temp_dir().join("jaxpet_owner_prov_test_readfail");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        // 用目录路径触发 read_to_string 失败（P2：读失败不得用空内容覆盖原文件）
        let dirpath = dir.join("as_dir");
        std::fs::create_dir_all(&dirpath).unwrap();

        let result = upsert_env_line(&dirpath, "VOICE_OWNER_CREDENTIAL", "secret");

        assert_eq!(result, Err(EXIT_ENV_WRITE_FAILED));
        assert!(dirpath.is_dir()); // 未被写成文件，内容未被动过
        let _ = std::fs::remove_dir_all(&dir);
    }
}
