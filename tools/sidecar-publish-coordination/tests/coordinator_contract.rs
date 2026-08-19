use sidecar_publish_coordination::coordinator::{Coordinator, ReleaseOutcome};
use sidecar_publish_coordination::owner::ProcessIdentity;
use std::fs;
use tempfile::tempdir;

fn owner_json(token: &str, pid: u32) -> String {
    format!(
        r#"{{"schema_version":1,"token":"{token}","pid":{pid},"created_at":"2026-08-19T00:00:00.000Z","process_creation_time":"2026-08-19T00:00:00.000Z","process_creation_identity":"sha256-test"}}"#
    )
}

#[test]
fn acquire_then_release_round_trip_leaves_no_lock_file() {
    let directory = tempdir().unwrap();
    let root = directory.path().join("runtime");
    fs::create_dir_all(&root).unwrap();
    let mut coordinator = Coordinator::new(&root).unwrap();

    let lease = coordinator
        .acquire(owner_json("550e8400-e29b-41d4-a716-446655440000", std::process::id()))
        .expect("acquire should succeed on an unlocked root");
    assert_eq!(fs::read_to_string(root.join("publish.lock")).unwrap(),
        owner_json("550e8400-e29b-41d4-a716-446655440000", std::process::id()) + "\n");

    let outcome = coordinator.release(&lease, "550e8400-e29b-41d4-a716-446655440000").unwrap();
    assert_eq!(outcome, ReleaseOutcome::Ok);
    assert!(!root.join("publish.lock").exists());
}

#[test]
fn second_acquire_while_held_returns_busy() {
    let directory = tempdir().unwrap();
    let root = directory.path().join("runtime");
    fs::create_dir_all(&root).unwrap();
    let mut coordinator = Coordinator::new(&root).unwrap();
    let _lease = coordinator
        .acquire(owner_json("550e8400-e29b-41d4-a716-446655440000", std::process::id()))
        .unwrap();

    // Same coordinator instance: second acquire must fail busy while mutex is held.
    let second = coordinator
        .acquire(owner_json("660e8400-e29b-41d4-a716-446655440001", std::process::id()));
    assert!(second.is_err(), "second acquire must fail while first is held");
}

#[test]
fn release_with_wrong_token_reports_owner_mismatch_and_keeps_owner() {
    let directory = tempdir().unwrap();
    let root = directory.path().join("runtime");
    fs::create_dir_all(&root).unwrap();
    let mut coordinator = Coordinator::new(&root).unwrap();
    let lease = coordinator
        .acquire(owner_json("550e8400-e29b-41d4-a716-446655440000", std::process::id()))
        .unwrap();

    let outcome = coordinator
        .release(&lease, "770e8400-e29b-41d4-a716-446655440002")
        .unwrap();
    assert_eq!(outcome, ReleaseOutcome::OwnerMismatch);
    assert!(root.join("publish.lock").exists());
}

#[test]
fn publish_without_valid_lease_is_rejected_and_pointer_untouched() {
    let directory = tempdir().unwrap();
    let root = directory.path().join("runtime");
    fs::create_dir_all(&root).unwrap();
    fs::write(root.join("current.json"), r#"{"generation":"g-old"}"#).unwrap();
    let mut coordinator = Coordinator::new(&root).unwrap();

    let result = coordinator.publish("bogus-lease", root.join("next.tmp"), root.join("current.json"));
    assert!(result.is_err());
    assert_eq!(
        fs::read_to_string(root.join("current.json")).unwrap(),
        r#"{"generation":"g-old"}"#
    );
}

#[test]
fn coordinator_classifies_crash_residue_before_recovery() {
    let directory = tempdir().unwrap();
    let root = directory.path().join("runtime");
    fs::create_dir_all(&root).unwrap();
    // Dead owner: pid that no longer exists.
    fs::write(root.join("publish.lock"), owner_json("550e8400-e29b-41d4-a716-446655440000", 400001)).unwrap();

    let mut coordinator = Coordinator::new_with_identity(
        &root,
        |_pid: u32| ProcessIdentity::Absent,
    )
    .unwrap();
    // Acquire over a dead owner inside the mutex must succeed and replace the owner bytes.
    let lease = coordinator
        .acquire(owner_json("880e8400-e29b-41d4-a716-446655440003", std::process::id()))
        .expect("dead owner must be reclaimable under mutex");
    assert_eq!(
        fs::read_to_string(root.join("publish.lock")).unwrap(),
        owner_json("880e8400-e29b-41d4-a716-446655440003", std::process::id()) + "\n"
    );
    let _ = coordinator.release(&lease, "880e8400-e29b-41d4-a716-446655440003").unwrap();
}
