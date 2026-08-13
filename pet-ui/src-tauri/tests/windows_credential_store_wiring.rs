#![cfg(all(windows, feature = "credential-test-support"))]

use std::time::{SystemTime, UNIX_EPOCH};

use jax_pet::credential::{CredentialProvider, SecretString};
use jax_pet::credential_windows::{CredentialSlot, WindowsCredentialStore};

fn secret(seed: u8) -> SecretString {
    SecretString::parse_utf8(vec![seed; 32]).expect("synthetic credential must be valid")
}

#[test]
fn windows_store_routes_provider_operations_through_three_transaction_slots() {
    let suffix = format!(
        "test-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock must follow unix epoch")
            .as_nanos()
    );
    let store = WindowsCredentialStore::for_test_suffix(&suffix)
        .expect("test store must bind targets and mutex to current user");

    store.provision(secret(b'o')).expect("provision active");
    store.rotate(secret(b'n')).expect("transactional rotation");
    let active = store.load_active().expect("load committed active");
    assert!(active.expose().as_bytes().iter().all(|byte| *byte == b'n'));
    assert!(!store
        .slot_exists_for_test(CredentialSlot::Staging)
        .expect("read staging state"));
    assert!(!store
        .slot_exists_for_test(CredentialSlot::Backup)
        .expect("read backup state"));

    store.revoke().expect("revoke all slots");
    for slot in [
        CredentialSlot::Active,
        CredentialSlot::Staging,
        CredentialSlot::Backup,
    ] {
        assert!(!store
            .slot_exists_for_test(slot)
            .expect("read revoked slot state"));
    }
}
