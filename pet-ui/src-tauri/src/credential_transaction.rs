use crate::credential::{
    CredentialError, CredentialErrorCode, CredentialProvider, CredentialStatus, SecretString,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum CredentialSlot {
    Active,
    Staging,
    Backup,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LockState {
    Acquired,
    Abandoned,
    Busy,
}

pub trait CredentialBackend: Send + Sync {
    fn read(&self, slot: CredentialSlot) -> Result<Option<SecretString>, CredentialError>;
    fn write(&self, slot: CredentialSlot, value: &SecretString) -> Result<(), CredentialError>;
    fn delete(&self, slot: CredentialSlot) -> Result<(), CredentialError>;
}

pub trait CredentialTransactionLock: Send + Sync {
    type Guard;

    fn acquire(&self) -> Result<(Self::Guard, LockState), CredentialError>;
}

pub struct TransactionalCredentialStore<B, L> {
    backend: B,
    lock: L,
}

impl<B, L> TransactionalCredentialStore<B, L>
where
    B: CredentialBackend,
    L: CredentialTransactionLock,
{
    pub fn new(backend: B, lock: L) -> Self {
        Self { backend, lock }
    }

    #[cfg(all(windows, feature = "credential-test-support"))]
    pub(crate) fn backend_for_test(&self) -> &B {
        &self.backend
    }

    fn with_lock<T>(
        &self,
        operation: impl FnOnce(&Self) -> Result<T, CredentialError>,
    ) -> Result<T, CredentialError> {
        let (_guard, state) = self.lock.acquire()?;
        match state {
            LockState::Busy => {
                return Err(CredentialError::new(CredentialErrorCode::CredentialBusy));
            }
            LockState::Acquired | LockState::Abandoned => self.recover_locked()?,
        }
        operation(self)
    }

    fn read_for_recovery(
        &self,
        slot: CredentialSlot,
    ) -> Result<Option<SecretString>, CredentialError> {
        self.backend.read(slot).map_err(|_| recovery_failed())
    }

    fn verify(&self, slot: CredentialSlot, expected: &SecretString) -> Result<(), CredentialError> {
        let actual = self
            .backend
            .read(slot)
            .map_err(as_write_failed)?
            .ok_or_else(|| CredentialError::new(CredentialErrorCode::CredentialWriteFailed))?;
        if constant_time_equal(actual.expose().as_bytes(), expected.expose().as_bytes()) {
            Ok(())
        } else {
            Err(CredentialError::new(
                CredentialErrorCode::CredentialWriteFailed,
            ))
        }
    }

    fn restore_for_recovery(&self, backup: &SecretString) -> Result<(), CredentialError> {
        self.backend
            .write(CredentialSlot::Active, backup)
            .map_err(|_| recovery_failed())?;
        self.verify(CredentialSlot::Active, backup)
            .map_err(|_| recovery_failed())
    }

    fn cleanup_for_recovery(&self, slots: &[CredentialSlot]) -> Result<(), CredentialError> {
        for slot in slots {
            self.backend.delete(*slot).map_err(|_| recovery_failed())?;
        }
        Ok(())
    }

    fn recover_locked(&self) -> Result<(), CredentialError> {
        let staging = self.read_for_recovery(CredentialSlot::Staging)?;
        let backup = self.read_for_recovery(CredentialSlot::Backup)?;
        if staging.is_none() && backup.is_none() {
            return Ok(());
        }
        let active = self.read_for_recovery(CredentialSlot::Active)?;

        match (active.as_ref(), staging.as_ref(), backup.as_ref()) {
            (Some(active), Some(staging), None) => {
                let _promote_committed = secret_eq(active, staging);
                self.cleanup_for_recovery(&[CredentialSlot::Staging])
            }
            (None, Some(_), None) => Err(recovery_failed()),
            (Some(active), Some(staging), Some(backup)) => {
                if secret_eq(active, staging) {
                    self.cleanup_for_recovery(&[CredentialSlot::Backup, CredentialSlot::Staging])
                } else if secret_eq(active, backup) {
                    self.cleanup_for_recovery(&[CredentialSlot::Staging, CredentialSlot::Backup])
                } else {
                    self.restore_for_recovery(backup)?;
                    self.cleanup_for_recovery(&[CredentialSlot::Staging, CredentialSlot::Backup])
                }
            }
            (None, Some(_), Some(backup)) => {
                self.restore_for_recovery(backup)?;
                self.cleanup_for_recovery(&[CredentialSlot::Staging, CredentialSlot::Backup])
            }
            (Some(active), None, Some(backup)) if secret_eq(active, backup) => {
                self.cleanup_for_recovery(&[CredentialSlot::Backup])
            }
            (_, None, Some(backup)) => {
                self.restore_for_recovery(backup)?;
                self.cleanup_for_recovery(&[CredentialSlot::Backup])
            }
            (_, None, None) => Ok(()),
        }
    }

    fn load_locked(&self) -> Result<SecretString, CredentialError> {
        self.backend
            .read(CredentialSlot::Active)?
            .ok_or_else(|| CredentialError::new(CredentialErrorCode::CredentialMissing))
    }

    fn provision_locked(&self, secret: &SecretString) -> Result<(), CredentialError> {
        self.backend.write(CredentialSlot::Active, secret)?;
        self.verify(CredentialSlot::Active, secret)
    }

    fn rotate_locked(&self, replacement: &SecretString) -> Result<(), CredentialError> {
        self.rotate_locked_with_hook(replacement, |_, _| Ok(()))
    }

    fn rotate_locked_with_hook(
        &self,
        replacement: &SecretString,
        mut checkpoint: impl FnMut(CredentialSlot, bool) -> Result<(), CredentialError>,
    ) -> Result<(), CredentialError> {
        let active = self.load_locked()?;
        self.backend.write(CredentialSlot::Staging, replacement)?;
        checkpoint(CredentialSlot::Staging, false)?;
        self.verify(CredentialSlot::Staging, replacement)?;

        if let Err(error) = self.backend.write(CredentialSlot::Backup, &active) {
            if self.backend.delete(CredentialSlot::Staging).is_err() {
                return Err(recovery_failed());
            }
            return Err(error);
        }
        checkpoint(CredentialSlot::Backup, false)?;
        if let Err(error) = self.verify(CredentialSlot::Backup, &active) {
            if self
                .cleanup_for_recovery(&[CredentialSlot::Backup, CredentialSlot::Staging])
                .is_err()
            {
                return Err(recovery_failed());
            }
            return Err(error);
        }

        if self
            .backend
            .write(CredentialSlot::Active, replacement)
            .is_err()
        {
            if self
                .backend
                .write(CredentialSlot::Active, &active)
                .and_then(|_| self.verify(CredentialSlot::Active, &active))
                .is_err()
            {
                return Err(recovery_failed());
            }
            self.cleanup_for_recovery(&[CredentialSlot::Staging, CredentialSlot::Backup])?;
            return Err(CredentialError::new(
                CredentialErrorCode::CredentialRotationFailed,
            ));
        }
        checkpoint(CredentialSlot::Active, false)?;
        if self.verify(CredentialSlot::Active, replacement).is_err() {
            if self
                .backend
                .write(CredentialSlot::Active, &active)
                .and_then(|_| self.verify(CredentialSlot::Active, &active))
                .is_err()
            {
                return Err(recovery_failed());
            }
            self.cleanup_for_recovery(&[CredentialSlot::Staging, CredentialSlot::Backup])?;
            return Err(CredentialError::new(
                CredentialErrorCode::CredentialRotationFailed,
            ));
        }
        checkpoint(CredentialSlot::Active, true)?;
        self.backend.delete(CredentialSlot::Backup)?;
        checkpoint(CredentialSlot::Backup, true)?;
        self.backend.delete(CredentialSlot::Staging)?;
        checkpoint(CredentialSlot::Staging, true)
    }

    #[cfg(all(windows, feature = "credential-test-support"))]
    pub(crate) fn rotate_with_hook(
        &self,
        replacement: &SecretString,
        checkpoint: impl FnMut(CredentialSlot, bool) -> Result<(), CredentialError>,
    ) -> Result<(), CredentialError> {
        self.with_lock(|store| store.rotate_locked_with_hook(replacement, checkpoint))
    }

    #[cfg(all(windows, feature = "credential-test-support"))]
    pub(crate) fn hold_lock_for_test(&self, hold_ms: u64) -> Result<LockState, CredentialError> {
        let (_guard, state) = self.lock.acquire()?;
        std::thread::sleep(std::time::Duration::from_millis(hold_ms));
        Ok(state)
    }

    fn revoke_locked(&self) -> Result<(), CredentialError> {
        for slot in [
            CredentialSlot::Active,
            CredentialSlot::Staging,
            CredentialSlot::Backup,
        ] {
            self.backend.delete(slot)?;
        }
        Ok(())
    }
}

impl<B, L> CredentialProvider for TransactionalCredentialStore<B, L>
where
    B: CredentialBackend,
    L: CredentialTransactionLock,
{
    fn status(&self) -> CredentialStatus {
        match self.with_lock(|store| store.load_locked()) {
            Ok(_) => CredentialStatus::Ready,
            Err(error) if error.code == CredentialErrorCode::CredentialMissing => {
                CredentialStatus::ProvisionRequired
            }
            Err(error) => CredentialStatus::Error(error.code),
        }
    }

    fn load_active(&self) -> Result<SecretString, CredentialError> {
        self.with_lock(|store| store.load_locked())
    }

    fn provision(&self, secret: SecretString) -> Result<(), CredentialError> {
        self.with_lock(|store| store.provision_locked(&secret))
    }

    fn rotate(&self, replacement: SecretString) -> Result<(), CredentialError> {
        self.with_lock(|store| store.rotate_locked(&replacement))
    }

    fn revoke(&self) -> Result<(), CredentialError> {
        self.with_lock(|store| store.revoke_locked())
    }
}

fn recovery_failed() -> CredentialError {
    CredentialError::new(CredentialErrorCode::CredentialRecoveryFailed)
}

fn as_write_failed(error: CredentialError) -> CredentialError {
    CredentialError {
        code: CredentialErrorCode::CredentialWriteFailed,
        os_code: error.os_code,
    }
}

fn secret_eq(left: &SecretString, right: &SecretString) -> bool {
    constant_time_equal(left.expose().as_bytes(), right.expose().as_bytes())
}

fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    let mut difference = left.len() ^ right.len();
    let length = left.len().min(right.len());
    for index in 0..length {
        difference |= usize::from(left[index] ^ right[index]);
    }
    difference == 0
}
