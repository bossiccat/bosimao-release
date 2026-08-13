#[cfg(not(windows))]
use crate::credential::CredentialErrorCode;
use crate::credential::{CredentialError, CredentialProvider, CredentialStatus, SecretString};
pub use crate::credential_transaction::{
    CredentialBackend, CredentialSlot, CredentialTransactionLock, LockState,
    TransactionalCredentialStore,
};

pub const SIDECAR_CREDENTIAL_STAGING_TARGET: &str =
    "JaxPet/com.jax.pet/voice-sidecar/v1/txn/staging";
pub const SIDECAR_CREDENTIAL_BACKUP_TARGET: &str = "JaxPet/com.jax.pet/voice-sidecar/v1/txn/backup";
pub const SIDECAR_CREDENTIAL_MUTEX_PREFIX: &str = "Global\\JaxPet.VoiceSidecarCredential.v1";

#[cfg(windows)]
use crate::credential::SIDECAR_CREDENTIAL_TARGET;
#[cfg(windows)]
use crate::credential_windows_backend::{CredentialTargets, Win32CredentialBackend};
#[cfg(windows)]
use crate::credential_windows_lock::Win32TransactionLock;

#[cfg(windows)]
type ProductionStore = TransactionalCredentialStore<Win32CredentialBackend, Win32TransactionLock>;

pub struct WindowsCredentialStore {
    #[cfg(windows)]
    inner: Result<ProductionStore, CredentialError>,
}

impl WindowsCredentialStore {
    #[cfg(windows)]
    pub fn sidecar() -> Self {
        Self {
            inner: Self::build_inner(None),
        }
    }

    #[cfg(not(windows))]
    pub const fn sidecar() -> Self {
        Self {}
    }

    #[cfg(windows)]
    fn build_inner(suffix: Option<&str>) -> Result<ProductionStore, CredentialError> {
        let targets = match suffix {
            Some(suffix) => CredentialTargets::new(
                format!("{SIDECAR_CREDENTIAL_TARGET}/{suffix}"),
                format!("{SIDECAR_CREDENTIAL_STAGING_TARGET}/{suffix}"),
                format!("{SIDECAR_CREDENTIAL_BACKUP_TARGET}/{suffix}"),
            ),
            None => CredentialTargets::new(
                SIDECAR_CREDENTIAL_TARGET.to_owned(),
                SIDECAR_CREDENTIAL_STAGING_TARGET.to_owned(),
                SIDECAR_CREDENTIAL_BACKUP_TARGET.to_owned(),
            ),
        };
        let backend = Win32CredentialBackend::new(targets);
        let lock = Win32TransactionLock::current_user(SIDECAR_CREDENTIAL_MUTEX_PREFIX, suffix)?;
        Ok(TransactionalCredentialStore::new(backend, lock))
    }

    #[cfg(all(windows, feature = "credential-test-support"))]
    pub fn for_test_suffix(suffix: &str) -> Result<Self, CredentialError> {
        Ok(Self {
            inner: Ok(Self::build_inner(Some(suffix))?),
        })
    }

    #[cfg(all(windows, feature = "credential-test-support"))]
    pub(crate) fn rotate_with_hook(
        &self,
        replacement: &SecretString,
        checkpoint: impl FnMut(CredentialSlot, bool) -> Result<(), CredentialError>,
    ) -> Result<(), CredentialError> {
        self.inner
            .as_ref()
            .map_err(|error| *error)?
            .rotate_with_hook(replacement, checkpoint)
    }

    #[cfg(all(windows, feature = "credential-test-support"))]
    pub(crate) fn hold_lock_for_test(&self, hold_ms: u64) -> Result<LockState, CredentialError> {
        self.inner
            .as_ref()
            .map_err(|error| *error)?
            .hold_lock_for_test(hold_ms)
    }

    #[cfg(all(windows, feature = "credential-test-support"))]
    pub fn revoke_for_cleanup(&self) -> Result<(), CredentialError> {
        let mut first_cleanup_error = None;
        for slot in [
            CredentialSlot::Active,
            CredentialSlot::Staging,
            CredentialSlot::Backup,
        ] {
            if let Err(error) = self.delete_slot_for_test(slot) {
                first_cleanup_error.get_or_insert(error);
            }
        }
        match first_cleanup_error {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }

    #[cfg(all(windows, feature = "credential-test-support"))]
    pub fn load_slot_for_test(
        &self,
        slot: CredentialSlot,
    ) -> Result<Option<SecretString>, CredentialError> {
        self.inner
            .as_ref()
            .map_err(|error| *error)?
            .backend_for_test()
            .read(slot)
    }

    #[cfg(all(windows, feature = "credential-test-support"))]
    pub fn write_slot_for_test(
        &self,
        slot: CredentialSlot,
        secret: &SecretString,
    ) -> Result<(), CredentialError> {
        self.inner
            .as_ref()
            .map_err(|error| *error)?
            .backend_for_test()
            .write(slot, secret)
    }

    #[cfg(all(windows, feature = "credential-test-support"))]
    pub fn delete_slot_for_test(&self, slot: CredentialSlot) -> Result<(), CredentialError> {
        self.inner
            .as_ref()
            .map_err(|error| *error)?
            .backend_for_test()
            .delete(slot)
    }

    #[cfg(all(windows, feature = "credential-test-support"))]
    pub fn slot_exists_for_test(&self, slot: CredentialSlot) -> Result<bool, CredentialError> {
        self.inner
            .as_ref()
            .map_err(|error| *error)?
            .backend_for_test()
            .read(slot)
            .map(|value| value.is_some())
    }
}

#[cfg(windows)]
impl CredentialProvider for WindowsCredentialStore {
    fn status(&self) -> CredentialStatus {
        match &self.inner {
            Ok(inner) => inner.status(),
            Err(error) => CredentialStatus::Error(error.code),
        }
    }

    fn load_active(&self) -> Result<SecretString, CredentialError> {
        self.inner.as_ref().map_err(|error| *error)?.load_active()
    }

    fn provision(&self, secret: SecretString) -> Result<(), CredentialError> {
        self.inner
            .as_ref()
            .map_err(|error| *error)?
            .provision(secret)
    }

    fn rotate(&self, replacement: SecretString) -> Result<(), CredentialError> {
        self.inner
            .as_ref()
            .map_err(|error| *error)?
            .rotate(replacement)
    }

    fn revoke(&self) -> Result<(), CredentialError> {
        self.inner.as_ref().map_err(|error| *error)?.revoke()
    }
}

#[cfg(not(windows))]
impl CredentialProvider for WindowsCredentialStore {
    fn status(&self) -> CredentialStatus {
        CredentialStatus::Error(CredentialErrorCode::UnsupportedPlatform)
    }

    fn load_active(&self) -> Result<SecretString, CredentialError> {
        Err(unsupported())
    }

    fn provision(&self, _: SecretString) -> Result<(), CredentialError> {
        Err(unsupported())
    }

    fn rotate(&self, _: SecretString) -> Result<(), CredentialError> {
        Err(unsupported())
    }

    fn revoke(&self) -> Result<(), CredentialError> {
        Err(unsupported())
    }
}

#[cfg(not(windows))]
fn unsupported() -> CredentialError {
    CredentialError::new(CredentialErrorCode::UnsupportedPlatform)
}
