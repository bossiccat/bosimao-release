#[path = "windows_credential_transaction_contract/support.rs"]
mod support;

use jax_pet::credential::{
    CredentialError, CredentialErrorCode, CredentialProvider, SIDECAR_CREDENTIAL_TARGET,
};
use jax_pet::credential_windows::{
    CredentialSlot, LockState, SIDECAR_CREDENTIAL_BACKUP_TARGET, SIDECAR_CREDENTIAL_MUTEX_PREFIX,
    SIDECAR_CREDENTIAL_STAGING_TARGET,
};
use support::{secret, store, Event, FakeLock, Kind, MemoryBackend};

#[test]
fn sidecar_transaction_identity_is_three_fixed_targets_and_one_mutex_prefix() {
    assert_eq!(
        SIDECAR_CREDENTIAL_TARGET,
        "JaxPet/com.jax.pet/voice-sidecar/v1"
    );
    assert_eq!(
        SIDECAR_CREDENTIAL_STAGING_TARGET,
        "JaxPet/com.jax.pet/voice-sidecar/v1/txn/staging"
    );
    assert_eq!(
        SIDECAR_CREDENTIAL_BACKUP_TARGET,
        "JaxPet/com.jax.pet/voice-sidecar/v1/txn/backup"
    );
    assert_eq!(
        SIDECAR_CREDENTIAL_MUTEX_PREFIX,
        "Global\\JaxPet.VoiceSidecarCredential.v1"
    );
}

#[test]
fn rotate_stages_and_verifies_then_backs_up_promotes_verifies_and_cleans() {
    let backend = MemoryBackend::seeded(Some(b'o'), None, None);
    store(backend.clone(), FakeLock::new(LockState::Acquired))
        .rotate(secret(b'n'))
        .expect("rotation must commit");
    assert!(backend.has(CredentialSlot::Active, b'n'));
    assert!(!backend
        .0
        .lock()
        .unwrap()
        .slots
        .contains_key(&CredentialSlot::Staging));
    assert!(!backend
        .0
        .lock()
        .unwrap()
        .slots
        .contains_key(&CredentialSlot::Backup));
    let events = backend.events();
    assert!(MemoryBackend::before(
        &events,
        Event(Kind::Write, CredentialSlot::Staging),
        Event(Kind::Write, CredentialSlot::Backup)
    ));
    assert!(MemoryBackend::before(
        &events,
        Event(Kind::Write, CredentialSlot::Backup),
        Event(Kind::Write, CredentialSlot::Active)
    ));
    assert!(MemoryBackend::before(
        &events,
        Event(Kind::Delete, CredentialSlot::Backup),
        Event(Kind::Delete, CredentialSlot::Staging)
    ));
}

#[test]
fn rotation_failures_never_replace_the_last_verified_active_value() {
    for (kind, slot) in [
        (Kind::Write, CredentialSlot::Staging),
        (Kind::Write, CredentialSlot::Backup),
        (Kind::Write, CredentialSlot::Active),
    ] {
        let backend = MemoryBackend::seeded(Some(b'o'), None, None);
        backend.fail_once(kind, slot, CredentialErrorCode::CredentialWriteFailed);
        let result =
            store(backend.clone(), FakeLock::new(LockState::Acquired)).rotate(secret(b'n'));
        assert!(result.is_err());
        assert!(backend.has(CredentialSlot::Active, b'o'));
    }
}

#[test]
fn backup_readback_mismatch_is_cleaned_without_poisoning_the_verified_active() {
    let backend = MemoryBackend::seeded(Some(b'o'), None, None);
    backend.corrupt_next_read_after_write(CredentialSlot::Backup, b'x');

    let error = store(backend.clone(), FakeLock::new(LockState::Acquired))
        .rotate(secret(b'n'))
        .expect_err("a mismatched backup readback must abort rotation");

    assert_eq!(error.code, CredentialErrorCode::CredentialWriteFailed);
    assert!(backend.has(CredentialSlot::Active, b'o'));
    assert!(!backend
        .0
        .lock()
        .unwrap()
        .slots
        .contains_key(&CredentialSlot::Staging));
    assert!(!backend
        .0
        .lock()
        .unwrap()
        .slots
        .contains_key(&CredentialSlot::Backup));

    let loaded = store(backend.clone(), FakeLock::new(LockState::Acquired))
        .load_active()
        .expect("a later load must keep the last verified active credential");
    assert!(loaded.expose().as_bytes().iter().all(|byte| *byte == b'o'));
}

#[test]
fn backup_mismatch_cleanup_failure_returns_recovery_failed() {
    let backend = MemoryBackend::seeded(Some(b'o'), None, None);
    backend.corrupt_next_read_after_write(CredentialSlot::Backup, b'x');
    backend.fail_once(
        Kind::Delete,
        CredentialSlot::Backup,
        CredentialErrorCode::CredentialDeleteFailed,
    );

    let error = store(backend.clone(), FakeLock::new(LockState::Acquired))
        .rotate(secret(b'n'))
        .expect_err("cleanup failure must be fail-closed");

    assert_eq!(error.code, CredentialErrorCode::CredentialRecoveryFailed);
    assert!(backend.has(CredentialSlot::Active, b'o'));
}

#[test]
fn load_recovers_an_incomplete_promote_from_backup_before_returning_active() {
    let backend = MemoryBackend::seeded(Some(b'x'), Some(b'n'), Some(b'o'));
    let loaded = store(backend.clone(), FakeLock::new(LockState::Acquired))
        .load_active()
        .expect("backup recovery must produce a verified active value");
    assert!(loaded.expose().as_bytes().iter().all(|byte| *byte == b'o'));
    assert!(!backend
        .0
        .lock()
        .unwrap()
        .slots
        .contains_key(&CredentialSlot::Staging));
    assert!(!backend
        .0
        .lock()
        .unwrap()
        .slots
        .contains_key(&CredentialSlot::Backup));
}

#[test]
fn failed_recovery_keeps_transaction_evidence_and_fails_closed() {
    let backend = MemoryBackend::seeded(Some(b'x'), Some(b'n'), Some(b'o'));
    backend.fail_once(
        Kind::Write,
        CredentialSlot::Active,
        CredentialErrorCode::CredentialWriteFailed,
    );
    let error = store(backend.clone(), FakeLock::new(LockState::Acquired))
        .load_active()
        .expect_err("unrecoverable relation must not return an active credential");
    assert_eq!(error.code, CredentialErrorCode::CredentialRecoveryFailed);
    assert!(backend.has(CredentialSlot::Staging, b'n'));
    assert!(backend.has(CredentialSlot::Backup, b'o'));
}

#[test]
fn revoke_is_idempotent_and_deletes_active_staging_and_backup() {
    let backend = MemoryBackend::seeded(Some(b'o'), Some(b'n'), Some(b'o'));
    let credential_store = store(backend.clone(), FakeLock::new(LockState::Acquired));
    credential_store.revoke().expect("first revoke");
    credential_store.revoke().expect("idempotent revoke");
    for slot in [
        CredentialSlot::Active,
        CredentialSlot::Staging,
        CredentialSlot::Backup,
    ] {
        assert!(!backend.0.lock().unwrap().slots.contains_key(&slot));
    }
}

#[test]
fn busy_and_abandoned_mutex_paths_are_fail_closed_and_recover_first() {
    let busy_backend = MemoryBackend::seeded(Some(b'o'), None, None);
    let busy_lock = FakeLock::new(LockState::Busy);
    let error = store(busy_backend.clone(), busy_lock.clone())
        .load_active()
        .expect_err("busy lock must prevent an unlocked read");
    assert_eq!(error.code, CredentialErrorCode::CredentialBusy);
    assert!(busy_backend.events().is_empty());
    assert_eq!(busy_lock.acquisitions(), 1);

    let abandoned_backend = MemoryBackend::seeded(Some(b'x'), Some(b'n'), Some(b'o'));
    let loaded = store(
        abandoned_backend.clone(),
        FakeLock::new(LockState::Abandoned),
    )
    .load_active()
    .expect("abandoned owner must trigger recovery before read");
    assert!(loaded.expose().as_bytes().iter().all(|byte| *byte == b'o'));
    assert!(!abandoned_backend
        .0
        .lock()
        .unwrap()
        .slots
        .contains_key(&CredentialSlot::Staging));
    assert!(!abandoned_backend
        .0
        .lock()
        .unwrap()
        .slots
        .contains_key(&CredentialSlot::Backup));
}

#[test]
fn secret_and_errors_are_redacted_from_debug_and_stable_diagnostics() {
    let value = secret(b'q');
    let secret_debug = format!("{value:?}");
    assert_eq!(secret_debug, "SecretString([REDACTED])");
    assert!(!secret_debug.contains(value.expose()));

    let error = CredentialError::with_os_code(CredentialErrorCode::CredentialWriteFailed, 5);
    let error_debug = format!("{error:?}");
    assert!(!error_debug.contains(value.expose()));
    assert_eq!(error.code.stable_code(), "SIDECAR_CREDENTIAL_WRITE_FAILED");
    assert!(!std::env::args().any(|arg| arg.contains(value.expose())));
}
