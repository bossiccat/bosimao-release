#![cfg(all(windows, feature = "credential-test-support"))]

use crate::credential::{CredentialError, CredentialProvider, SecretString};
use crate::credential_windows::{CredentialSlot, LockState, WindowsCredentialStore};
use crate::o020_crash_barrier::CrashBarrier;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProbeCheckpoint {
    StageWrite,
    BackupWrite,
    ActiveWrite,
    ActiveVerify,
    DeleteBackup,
    DeleteStaging,
}

impl ProbeCheckpoint {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "stage-write" => Some(Self::StageWrite),
            "backup-write" => Some(Self::BackupWrite),
            "active-write" => Some(Self::ActiveWrite),
            "active-verify" => Some(Self::ActiveVerify),
            "delete-backup" => Some(Self::DeleteBackup),
            "delete-staging" => Some(Self::DeleteStaging),
            _ => None,
        }
    }
}

pub fn provision(suffix: &str, secret: SecretString) -> Result<(), CredentialError> {
    WindowsCredentialStore::for_test_suffix(suffix)?.provision(secret)
}

pub fn read(suffix: &str) -> Result<bool, CredentialError> {
    WindowsCredentialStore::for_test_suffix(suffix)?
        .load_active()
        .map(|_| true)
}

pub fn rotate(suffix: &str, replacement: SecretString) -> Result<(), CredentialError> {
    WindowsCredentialStore::for_test_suffix(suffix)?.rotate(replacement)
}

pub fn revoke(suffix: &str) -> Result<(), CredentialError> {
    WindowsCredentialStore::for_test_suffix(suffix)?.revoke()
}

pub fn cleanup(suffix: &str) -> Result<(), CredentialError> {
    let store = WindowsCredentialStore::for_test_suffix(suffix)?;
    for slot in [
        CredentialSlot::Active,
        CredentialSlot::Staging,
        CredentialSlot::Backup,
    ] {
        store.delete_slot_for_test(slot)?;
    }
    Ok(())
}

pub fn recover(suffix: &str) -> Result<bool, CredentialError> {
    WindowsCredentialStore::for_test_suffix(suffix)?
        .load_active()
        .map(|_| true)
}

pub fn inject_checkpoint(
    suffix: &str,
    replacement: &SecretString,
    checkpoint: ProbeCheckpoint,
    barrier_id: &str,
) -> Result<(), CredentialError> {
    let barrier = CrashBarrier::create(barrier_id)?;
    WindowsCredentialStore::for_test_suffix(suffix)?.rotate_with_hook(
        replacement,
        move |slot, verified_or_deleted| {
            let reached = match checkpoint {
                ProbeCheckpoint::StageWrite => {
                    slot == CredentialSlot::Staging && !verified_or_deleted
                }
                ProbeCheckpoint::BackupWrite => {
                    slot == CredentialSlot::Backup && !verified_or_deleted
                }
                ProbeCheckpoint::ActiveWrite => {
                    slot == CredentialSlot::Active && !verified_or_deleted
                }
                ProbeCheckpoint::ActiveVerify => {
                    slot == CredentialSlot::Active && verified_or_deleted
                }
                ProbeCheckpoint::DeleteBackup => {
                    slot == CredentialSlot::Backup && verified_or_deleted
                }
                ProbeCheckpoint::DeleteStaging => {
                    slot == CredentialSlot::Staging && verified_or_deleted
                }
            };
            if reached {
                barrier.notify_and_wait()?;
            }
            Ok(())
        },
    )
}

pub fn hold(suffix: &str, hold_ms: u64) -> Result<LockState, CredentialError> {
    WindowsCredentialStore::for_test_suffix(suffix)?.hold_lock_for_test(hold_ms)
}
