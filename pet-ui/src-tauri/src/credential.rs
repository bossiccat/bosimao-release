use std::fmt;
use zeroize::Zeroizing;

pub const SIDECAR_CREDENTIAL_TARGET: &str = "JaxPet/com.jax.pet/voice-sidecar/v1";
pub const SIDECAR_CREDENTIAL_ENV: &str = "VOICE_SIDECAR_CREDENTIAL";
pub const SIDECAR_CREDENTIAL_MIN_BYTES: usize = 32;
pub const SIDECAR_CREDENTIAL_MAX_BYTES: usize = 512;

// owner 是「桌宠 → 后端」的管理员身份，与 sidecar（桌宠 → 本地 sidecar）语义分离，
// 使用独立 CM target 前缀（ADR-022 D3）。owner 凭证复用 SIDECAR 的 32–512 bytes 校验。
pub const OWNER_CREDENTIAL_TARGET: &str = "JaxPet/com.jax.pet/voice-owner/v1";
pub const OWNER_CREDENTIAL_ENV: &str = "VOICE_OWNER_CREDENTIAL";

pub struct SecretString(Zeroizing<String>);

impl SecretString {
    pub fn parse_utf8(bytes: Vec<u8>) -> Result<Self, CredentialError> {
        Self::parse_zeroizing(Zeroizing::new(bytes))
    }

    #[cfg(all(windows, feature = "credential-test-support"))]
    pub fn parse_test_bytes(bytes: Zeroizing<Vec<u8>>) -> Result<Self, CredentialError> {
        Self::parse_zeroizing(bytes)
    }

    #[cfg(all(windows, feature = "credential-test-support"))]
    pub fn copy_for_test(&self) -> Zeroizing<Vec<u8>> {
        Zeroizing::new(self.expose().as_bytes().to_vec())
    }

    pub(crate) fn parse_zeroizing(mut bytes: Zeroizing<Vec<u8>>) -> Result<Self, CredentialError> {
        if !(SIDECAR_CREDENTIAL_MIN_BYTES..=SIDECAR_CREDENTIAL_MAX_BYTES).contains(&bytes.len()) {
            return Err(CredentialError::new(CredentialErrorCode::CredentialCorrupt));
        }
        let owned = std::mem::take(&mut *bytes);
        let value = match String::from_utf8(owned) {
            Ok(value) => Zeroizing::new(value),
            Err(error) => {
                let _invalid = Zeroizing::new(error.into_bytes());
                return Err(CredentialError::new(CredentialErrorCode::CredentialCorrupt));
            }
        };
        if value.bytes().any(|byte| matches!(byte, 0 | b'\r' | b'\n')) {
            return Err(CredentialError::new(CredentialErrorCode::CredentialCorrupt));
        }
        Ok(Self(value))
    }

    pub fn expose(&self) -> &str {
        self.0.as_str()
    }
}

impl fmt::Debug for SecretString {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("SecretString([REDACTED])")
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialErrorCode {
    CredentialMissing,
    CredentialCorrupt,
    CredentialReadDenied,
    CredentialBusy,
    CredentialRecoveryFailed,
    CredentialWriteFailed,
    CredentialDeleteFailed,
    CredentialRotationFailed,
    CredentialRevoked,
    UnsupportedPlatform,
}

impl CredentialErrorCode {
    pub fn stable_code(self) -> &'static str {
        match self {
            Self::CredentialMissing => "SIDECAR_CREDENTIAL_MISSING",
            Self::CredentialCorrupt => "SIDECAR_CREDENTIAL_CORRUPT",
            Self::CredentialReadDenied => "SIDECAR_CREDENTIAL_READ_DENIED",
            Self::CredentialBusy => "SIDECAR_CREDENTIAL_BUSY",
            Self::CredentialRecoveryFailed => "SIDECAR_CREDENTIAL_RECOVERY_FAILED",
            Self::CredentialWriteFailed => "SIDECAR_CREDENTIAL_WRITE_FAILED",
            Self::CredentialDeleteFailed => "SIDECAR_CREDENTIAL_DELETE_FAILED",
            Self::CredentialRotationFailed => "SIDECAR_CREDENTIAL_ROTATION_FAILED",
            Self::CredentialRevoked => "SIDECAR_CREDENTIAL_REVOKED",
            Self::UnsupportedPlatform => "SIDECAR_CREDENTIAL_UNSUPPORTED_PLATFORM",
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub struct CredentialError {
    pub code: CredentialErrorCode,
    pub os_code: Option<u32>,
}

impl CredentialError {
    pub const fn new(code: CredentialErrorCode) -> Self {
        Self {
            code,
            os_code: None,
        }
    }
    pub const fn with_os_code(code: CredentialErrorCode, os_code: u32) -> Self {
        Self {
            code,
            os_code: Some(os_code),
        }
    }
}

impl fmt::Debug for CredentialError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CredentialError")
            .field("code", &self.code)
            .field("os_code", &self.os_code)
            .finish()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialStatus {
    ProvisionRequired,
    Ready,
    Rotating,
    Revoked,
    Error(CredentialErrorCode),
}

pub trait CredentialProvider: Send + Sync {
    fn status(&self) -> CredentialStatus;
    fn load_active(&self) -> Result<SecretString, CredentialError>;
    fn provision(&self, secret: SecretString) -> Result<(), CredentialError>;
    fn rotate(&self, replacement: SecretString) -> Result<(), CredentialError>;
    fn revoke(&self) -> Result<(), CredentialError>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn secret_validation_and_debug_are_fail_closed() {
        for bytes in [
            vec![],
            vec![b'a'; 31],
            vec![b'a'; 513],
            vec![0xff; 32],
            vec![0; 32],
            vec![b'\r'; 32],
            vec![b'\n'; 32],
        ] {
            assert_eq!(
                SecretString::parse_utf8(bytes).unwrap_err().code,
                CredentialErrorCode::CredentialCorrupt
            );
        }
        let secret = SecretString::parse_utf8(vec![b'a'; 32]).unwrap();
        assert_eq!(format!("{secret:?}"), "SecretString([REDACTED])");
    }
}
