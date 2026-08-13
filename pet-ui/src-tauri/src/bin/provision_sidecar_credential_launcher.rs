#![cfg(windows)]
// 2026-08-13 弹窗修复：GUI 子系统禁止命令窗。
#![windows_subsystem = "windows"]

use std::process::ExitCode;
use std::time::Duration;

use jax_pet::credential::SecretString;
use jax_pet::provision_orchestrator::{ProvisionOrchestrator, WindowsProvisionTransport};
use windows::Win32::Security::Cryptography::{BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG};
use zeroize::Zeroizing;

const SECRET_BYTES: usize = 32;
const PROVISION_TIMEOUT: Duration = Duration::from_secs(30);
const EXIT_LAUNCHER_FAILED: u8 = 10;

fn hex_encode(bytes: &[u8]) -> Zeroizing<Vec<u8>> {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut encoded = Zeroizing::new(Vec::with_capacity(bytes.len() * 2));
    for byte in bytes {
        encoded.push(HEX[(byte >> 4) as usize]);
        encoded.push(HEX[(byte & 0x0f) as usize]);
    }
    encoded
}

fn generate_secret() -> Result<SecretString, ()> {
    let mut random = Zeroizing::new([0u8; SECRET_BYTES]);
    let status =
        unsafe { BCryptGenRandom(None, random.as_mut_slice(), BCRYPT_USE_SYSTEM_PREFERRED_RNG) };
    if status.0 < 0 {
        return Err(());
    }
    let mut encoded = hex_encode(random.as_slice());
    let bytes = std::mem::take(&mut *encoded);
    SecretString::parse_utf8(bytes).map_err(|_| ())
}

fn run() -> Result<(), ()> {
    let secret = generate_secret()?;
    ProvisionOrchestrator::new(WindowsProvisionTransport, PROVISION_TIMEOUT)
        .provision(secret)
        .map_err(|_| ())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(()) => ExitCode::from(EXIT_LAUNCHER_FAILED),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_secret_is_valid_and_independent() {
        let first = generate_secret().expect("first secret");
        let second = generate_secret().expect("second secret");
        assert_eq!(first.expose().len(), SECRET_BYTES * 2);
        assert_eq!(second.expose().len(), SECRET_BYTES * 2);
        assert_ne!(first.expose(), second.expose());
    }
}
