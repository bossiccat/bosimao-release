use sidecar_publish_coordination::owner::{
    classify_owner_bytes, create_owner_file, OwnerState, ProcessIdentity,
};
use std::fs;
use tempfile::tempdir;

fn valid_owner() -> &'static [u8] {
    br#"{"schema_version":1,"token":"550e8400-e29b-41d4-a716-446655440000","pid":1234,"created_at":"2026-08-18T12:00:00.000Z","process_creation_time":"2026-08-18T12:00:00.000Z","process_creation_identity":"sha256-owner"}
"#
}

#[test]
fn malformed_owner_is_never_reclaimable() {
    assert_eq!(
        classify_owner_bytes(br#"{"pid":1234}"#, |_: u32| ProcessIdentity::Unavailable {
            reason: "not queried".to_owned(),
        }),
        OwnerState::Invalid
    );
}

#[test]
fn unavailable_identity_is_never_reclaimable() {
    assert_eq!(
        classify_owner_bytes(valid_owner(), |_: u32| ProcessIdentity::Unavailable {
            reason: "access denied".to_owned(),
        }),
        OwnerState::IdentityUnavailable
    );
}

#[test]
fn matching_identity_is_live_and_mismatch_is_pid_reused() {
    assert_eq!(
        classify_owner_bytes(valid_owner(), |_: u32| ProcessIdentity::Verified {
            creation_time: "2026-08-18T12:00:00.000Z".to_owned(),
            identity: "sha256-owner".to_owned(),
        }),
        OwnerState::Live
    );
    assert_eq!(
        classify_owner_bytes(valid_owner(), |_: u32| ProcessIdentity::Verified {
            creation_time: "2026-08-18T12:00:00.000Z".to_owned(),
            identity: "sha256-new".to_owned(),
        }),
        OwnerState::PidReused
    );
}

#[test]
fn owner_creation_is_create_new_and_preserves_existing_bytes() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("publish.lock");
    let bytes = valid_owner();
    create_owner_file(&path, bytes).unwrap();
    assert_eq!(fs::read(&path).unwrap(), bytes);
    let result = create_owner_file(&path, br#"replacement"#);
    assert!(result.is_err());
    assert_eq!(fs::read(&path).unwrap(), bytes);
}
