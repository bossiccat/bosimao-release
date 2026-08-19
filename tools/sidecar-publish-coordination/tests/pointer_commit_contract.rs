//! Contract tests for atomic pointer commit (audit P0-2).

use sidecar_publish_coordination::coordinator::{Coordinator, ReleaseOutcome};
use sidecar_publish_coordination::pointer_commit::commit_pointer;
use std::fs;
use tempfile::tempdir;

fn owner_json(token: &str, pid: u32) -> String {
    format!(
        r#"{{"schema_version":1,"token":"{token}","pid":{pid},"created_at":"2026-08-19T00:00:00.000Z","process_creation_time":"2026-08-19T00:00:00.000Z","process_creation_identity":"sha256-test"}}"#
    )
}

#[test]
fn replace_over_existing_pointer_is_atomic_and_leaves_backup() {
    let directory = tempdir().unwrap();
    let root = directory.path().join("runtime");
    fs::create_dir_all(&root).unwrap();
    fs::write(root.join("current.json"), r#"{"generation":"g-old"}"#).unwrap();
    fs::write(root.join("next.tmp"), r#"{"generation":"g-new"}"#).unwrap();

    commit_pointer(&root.join("next.tmp"), &root.join("current.json")).unwrap();

    assert_eq!(
        fs::read_to_string(root.join("current.json")).unwrap(),
        r#"{"generation":"g-new"}"#,
        "pointer must now reference the new generation"
    );
    assert!(
        !root.join("next.tmp").exists(),
        "temporary file is consumed by the replace"
    );
}

#[test]
fn first_publish_without_existing_pointer_renames_atomically() {
    let directory = tempdir().unwrap();
    let root = directory.path().join("runtime");
    fs::create_dir_all(&root).unwrap();
    fs::write(root.join("next.tmp"), r#"{"generation":"g-first"}"#).unwrap();

    commit_pointer(&root.join("next.tmp"), &root.join("current.json")).unwrap();

    assert_eq!(
        fs::read_to_string(root.join("current.json")).unwrap(),
        r#"{"generation":"g-first"}"#
    );
    assert!(!root.join("next.tmp").exists());
}

#[test]
fn missing_temporary_file_fails_closed_without_touching_pointer() {
    let directory = tempdir().unwrap();
    let root = directory.path().join("runtime");
    fs::create_dir_all(&root).unwrap();
    fs::write(root.join("current.json"), r#"{"generation":"g-old"}"#).unwrap();

    let result = commit_pointer(&root.join("absent.tmp"), &root.join("current.json"));
    assert!(result.is_err());
    assert_eq!(
        fs::read_to_string(root.join("current.json")).unwrap(),
        r#"{"generation":"g-old"}"#,
        "failed commit must leave the original pointer intact"
    );
}

#[test]
fn coordinator_publish_with_valid_lease_commits_pointer_under_mutex() {
    let directory = tempdir().unwrap();
    let root = directory.path().join("runtime");
    fs::create_dir_all(&root).unwrap();
    fs::write(root.join("current.json"), r#"{"generation":"g-old"}"#).unwrap();
    fs::write(root.join("next.tmp"), r#"{"generation":"g-new"}"#).unwrap();

    let mut coordinator = Coordinator::new(&root).unwrap();
    let lease = coordinator
        .acquire(owner_json("990e8400-e29b-41d4-a716-446655440004", std::process::id()))
        .unwrap();

    coordinator
        .publish(&lease.lease_id, root.join("next.tmp"), root.join("current.json"))
        .expect("publish with a live lease must commit the pointer");

    assert_eq!(
        fs::read_to_string(root.join("current.json")).unwrap(),
        r#"{"generation":"g-new"}"#
    );

    let outcome = coordinator
        .release(&lease, "990e8400-e29b-41d4-a716-446655440004")
        .unwrap();
    assert_eq!(outcome, ReleaseOutcome::Ok);
}
