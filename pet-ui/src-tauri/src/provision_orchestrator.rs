use std::time::Duration;

use crate::credential::SecretString;

pub const HELPER_SUCCESS: u32 = 0;
pub const HELPER_NAME: &str = "provision_sidecar_credential.exe";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProvisionError {
    UnsupportedPlatform,
    SpawnFailed,
    PipeWriteFailed,
    WaitTimedOut,
    WaitFailed,
    HelperFailed(u32),
}

pub trait ProvisionTransport {
    fn run(&self, secret: &SecretString, timeout: Duration) -> Result<u32, ProvisionError>;
}

pub struct ProvisionOrchestrator<T> {
    transport: T,
    timeout: Duration,
}

impl<T: ProvisionTransport> ProvisionOrchestrator<T> {
    pub const fn new(transport: T, timeout: Duration) -> Self {
        Self { transport, timeout }
    }

    pub fn provision(&self, secret: SecretString) -> Result<(), ProvisionError> {
        let code = self.transport.run(&secret, self.timeout)?;
        if code == HELPER_SUCCESS {
            Ok(())
        } else {
            Err(ProvisionError::HelperFailed(code))
        }
    }

    pub const fn transport(&self) -> &T {
        &self.transport
    }
}

#[cfg(windows)]
pub use crate::provision_orchestrator_windows::WindowsProvisionTransport;

#[cfg(not(windows))]
pub struct WindowsProvisionTransport;

#[cfg(not(windows))]
impl ProvisionTransport for WindowsProvisionTransport {
    fn run(&self, _: &SecretString, _: Duration) -> Result<u32, ProvisionError> {
        Err(ProvisionError::UnsupportedPlatform)
    }
}
