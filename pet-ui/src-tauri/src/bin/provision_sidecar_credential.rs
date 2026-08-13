//! provision_sidecar_credential.rs — 一次性无回显 provisioner（契约 §6.1）。
//!
//! 受保护部署编排把独立 CSPRNG opaque sidecar credential 经继承的 stdin
//! 匿名管道写入本进程；本进程 validate 后写入 Credential Manager 三槽
//! active（readback 验证），并确保 staging/backup absent；只返回稳定状态码，
//! 不回显、不落盘、RAII 零化。安装器失败即不启动 sidecar（fail-closed）。
// 2026-08-13 弹窗修复：GUI 子系统禁止命令窗；stdin 管道读取不受影响
// （仅交互式无重定向输入会 fail-closed EXIT_INVALID_INPUT(1)，属预期契约）。
#![cfg_attr(windows, windows_subsystem = "windows")]

use std::io::Read;
use std::process::ExitCode;

use jax_pet::credential::{CredentialProvider, SecretString, SIDECAR_CREDENTIAL_MAX_BYTES};
use jax_pet::credential_windows::WindowsCredentialStore;
use zeroize::Zeroizing;

/// 稳定非零退出码（契约：helper 只返回 0 或稳定非零码，不回显）。
const EXIT_INVALID_INPUT: u8 = 1;
const EXIT_PROVISION_FAILED: u8 = 2;

/// 从继承的 stdin 管道读取至多 `max` 字节；EOF 结束；超长或读错误返回 Err。
/// 缓冲全程 Zeroizing，drop 时清空，避免失败路径残留明文。
fn read_limited<R: Read>(reader: &mut R, max: usize) -> Result<Zeroizing<Vec<u8>>, ()> {
    let mut buf = Zeroizing::new(Vec::with_capacity(64));
    let mut byte = [0u8; 1];
    loop {
        match reader.read(&mut byte) {
            Ok(0) => break,
            Ok(_) => {
                buf.push(byte[0]);
                if buf.len() > max {
                    return Err(());
                }
            }
            Err(_) => return Err(()),
        }
    }
    Ok(buf)
}

fn provision_from<R: Read>(reader: &mut R) -> Result<(), u8> {
    let mut raw =
        read_limited(reader, SIDECAR_CREDENTIAL_MAX_BYTES).map_err(|_| EXIT_INVALID_INPUT)?;
    if raw.is_empty() {
        return Err(EXIT_INVALID_INPUT);
    }
    // 从 Zeroizing 容器取出（mem::take 替换为空 Vec），parse_utf8 立即接管并
    // 在内部转入 Zeroizing<String>；原容器已空，无额外明文副本。
    let inner = std::mem::take(&mut *raw);
    let secret = SecretString::parse_utf8(inner).map_err(|_| EXIT_INVALID_INPUT)?;
    let store = WindowsCredentialStore::sidecar();
    // fresh install 编排：先 revoke（幂等清理三槽，CredDeleteW ERROR_NOT_FOUND
    // 视为成功），确保 staging/backup absent；再 provision（active 写入 + readback）。
    store.revoke().map_err(|_| EXIT_PROVISION_FAILED)?;
    store.provision(secret).map_err(|_| EXIT_PROVISION_FAILED)
}

fn main() -> ExitCode {
    let mut stdin = std::io::stdin().lock();
    match provision_from(&mut stdin) {
        Ok(()) => ExitCode::SUCCESS,
        Err(code) => ExitCode::from(code),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FailingReader;

    impl Read for FailingReader {
        fn read(&mut self, _buf: &mut [u8]) -> std::io::Result<usize> {
            Err(std::io::Error::other("read failure"))
        }
    }

    struct ChunkReader {
        data: Vec<u8>,
        pos: usize,
    }

    impl ChunkReader {
        fn new(data: &[u8]) -> Self {
            Self {
                data: data.to_vec(),
                pos: 0,
            }
        }
    }

    impl Read for ChunkReader {
        fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
            if self.pos >= self.data.len() {
                return Ok(0);
            }
            let n = buf.len().min(self.data.len() - self.pos).min(1);
            buf[..n].copy_from_slice(&self.data[self.pos..self.pos + n]);
            self.pos += n;
            Ok(n)
        }
    }

    #[test]
    fn read_limited_returns_empty_on_eof() {
        let mut reader = ChunkReader::new(b"");
        let out = read_limited(&mut reader, 512).expect("empty read ok");
        assert!(out.is_empty());
    }

    #[test]
    fn read_limited_returns_full_input() {
        let payload: Vec<u8> = (0..64u8).collect();
        let mut reader = ChunkReader::new(&payload);
        let out = read_limited(&mut reader, 512).expect("read ok");
        assert_eq!(*out, payload);
    }

    #[test]
    fn read_limited_rejects_overflow() {
        let payload = vec![b'a'; 513];
        let mut reader = ChunkReader::new(&payload);
        assert!(read_limited(&mut reader, 512).is_err());
    }

    #[test]
    fn read_limited_rejects_reader_failure() {
        let mut reader = FailingReader;
        assert!(read_limited(&mut reader, 512).is_err());
    }

    #[test]
    fn provision_from_rejects_empty_input() {
        let mut reader = ChunkReader::new(b"");
        assert_eq!(provision_from(&mut reader), Err(EXIT_INVALID_INPUT));
    }

    #[test]
    fn provision_from_rejects_invalid_secret() {
        // 31 bytes：低于 SIDECAR_CREDENTIAL_MIN_BYTES=32，parse_utf8 必须拒绝
        let mut reader = ChunkReader::new(&vec![b'a'; 31]);
        assert_eq!(provision_from(&mut reader), Err(EXIT_INVALID_INPUT));
    }

    #[test]
    fn provision_from_rejects_nul_byte() {
        let mut payload = vec![b'a'; 32];
        payload[10] = 0;
        let mut reader = ChunkReader::new(&payload);
        assert_eq!(provision_from(&mut reader), Err(EXIT_INVALID_INPUT));
    }
}
