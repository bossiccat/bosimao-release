use std::fs;
use std::path::PathBuf;

fn crate_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

#[test]
fn controller_is_feature_gated_and_locates_fixed_sibling_probe() {
    let cargo = fs::read_to_string(crate_root().join("Cargo.toml")).unwrap();
    assert!(cargo.contains("name = \"o020_credential_controller\""));
    assert!(cargo.contains("required-features = [\"credential-test-support\"]"));
    let process = fs::read_to_string(crate_root().join("src/o020_controller_process.rs")).unwrap();
    assert!(process.contains("current_exe"));
    assert!(process.contains("o020_credential_probe.exe"));
    assert!(!process.contains("probe_path:"));
}

#[test]
fn controller_uses_anonymous_stdin_named_barrier_and_bounded_hard_kill() {
    let source = fs::read_to_string(crate_root().join("src/o020_controller.rs")).unwrap();
    let process = fs::read_to_string(crate_root().join("src/o020_controller_process.rs")).unwrap();
    for required in ["WaitForMultipleObjects", "BARRIER_WAIT_MS"] {
        assert!(source.contains(required), "missing {required}");
    }
    for required in [
        "Stdio::piped",
        "WaitForSingleObject",
        "TerminateProcess",
        "child.wait()",
        "REAP_WAIT_MS",
    ] {
        assert!(process.contains(required), "missing {required}");
    }
    assert!(process.contains("--barrier"));
    assert!(process.contains("--checkpoint"));
    assert!(!process.contains("Command::env"));
    assert!(include_str!("../src/bin/o020_credential_controller.rs").contains("stdin"));
    assert!(!source.contains("std::process::exit"));
}

#[test]
fn controller_runs_all_checkpoints_recovery_cleanup_and_manifest() {
    let source = fs::read_to_string(crate_root().join("src/o020_controller.rs")).unwrap();
    for checkpoint in [
        "stage-write",
        "backup-write",
        "active-write",
        "active-verify",
        "delete-backup",
        "delete-staging",
    ] {
        assert!(source.contains(checkpoint), "missing {checkpoint}");
    }
    for operation in ["provision", "load", "crash"] {
        assert!(source.contains(&format!("\"{operation}\"")));
    }
    for field in [
        "numeric_exit",
        "slot_presence",
        "artifacts",
        "cleanup",
        "residual_processes",
        "residual_handles",
    ] {
        assert!(source.contains(field), "missing manifest field {field}");
    }
    assert!(source.contains("slot_exists_for_test"));
    assert!(source.contains("Sha256"));
    assert!(source.contains("barrier_id_hash"));
    assert!(source.contains("reached_at_ms"));
    assert!(source.contains("kill_at_ms"));
    assert!(source.contains("reap_at_ms"));
    assert!(source.contains("WaitForMultipleObjects"));
    assert!(include_str!("../src/o020_controller_process.rs").contains("drain_stream"));
    assert!(source.contains("constant_time_equal"));
    assert!(include_str!("../src/o020_controller_evidence.rs").contains("artifact_relative_path"));
    assert!(!source.contains("residual_processes += 0"));
}

#[test]
fn controller_reports_real_terminate_outcome_and_child_exit() {
    let process = fs::read_to_string(crate_root().join("src/o020_controller_process.rs")).unwrap();
    assert!(process.contains("ProcessOutcome"));
    assert!(process.contains("killed"));
    assert!(process.contains("actual_exit"));
    assert!(!process.contains("let _ = unsafe { TerminateProcess"));
    assert!(process.contains("WaitForSingleObject"));
    assert!(include_str!("../src/o020_controller.rs").contains("WAIT_FAILED"));
}

#[test]
fn controller_drains_both_pipes_concurrently_until_eof() {
    let process = fs::read_to_string(crate_root().join("src/o020_controller_process.rs")).unwrap();
    assert!(process.contains("std::thread::spawn"));
    assert!(process.contains("JoinHandle"));
    assert!(process.contains("truncated"));
    assert!(process.contains("drain_stream"));
    assert!(!process.contains("take(MAX_CHILD_OUTPUT)"));
}

#[test]
fn controller_artifacts_are_written_and_hashed_from_disk() {
    let artifacts =
        fs::read_to_string(crate_root().join("src/o020_controller_evidence.rs")).unwrap();
    assert!(artifacts.contains("create_dir_all"));
    assert!(artifacts.contains("write_all"));
    assert!(artifacts.contains("std::fs::read"));
    assert!(artifacts.contains("artifact_relative_path"));
    assert!(artifacts.contains("Sha256"));
    assert!(artifacts.contains("MoveFileExW"));
    let controller = fs::read_to_string(crate_root().join("src/o020_controller.rs")).unwrap();
    assert!(controller.contains("write_checkpoint"));
    assert!(controller.contains("write_manifest"));
}

#[test]
fn controller_propagates_cleanup_failure_and_observes_cleanup_flags() {
    let controller = fs::read_to_string(crate_root().join("src/o020_controller.rs")).unwrap();
    assert!(controller.contains("cleanup_error"));
    assert!(controller.contains("revoked:"));
    assert!(controller.contains("all_slots_absent:"));
    assert!(!controller.contains("revoked: true"));
    assert!(!controller.contains("all_slots_absent: true"));
}

#[test]
fn controller_persists_each_checkpoint_artifact_and_hashes_readback_bytes() {
    let source = fs::read_to_string(crate_root().join("src/o020_controller.rs")).unwrap();
    let evidence = fs::read_to_string(crate_root().join("src/o020_controller_evidence.rs"))
        .unwrap_or_default();
    let combined = format!("{source}\n{evidence}");
    for required in [
        "create_dir_all",
        "write_all",
        "sync_all",
        "MoveFileExW",
        "std::fs::read",
        "artifact_hash",
    ] {
        assert!(
            combined.contains(required),
            "missing real artifact step {required}"
        );
    }
    assert!(combined.contains("artifact_relative_path"));
    assert!(!source.contains("artifact_path: format!(\"checkpoints/"));
    assert!(!source.contains("Sha256::digest(&manifest_json)"));
}

#[test]
fn controller_records_actual_process_outcomes_and_drains_both_streams() {
    let source = fs::read_to_string(crate_root().join("src/o020_controller.rs")).unwrap();
    let process = fs::read_to_string(crate_root().join("src/o020_controller_process.rs")).unwrap();
    for required in [
        "TerminateProcess(handle, 30)",
        "GetExitCodeProcess",
        "actual_exit",
        "killed",
        "reaped",
        "stdout_digest",
        "stderr_digest",
        "total_bytes",
        "truncated",
        "std::thread::spawn",
    ] {
        assert!(
            source.contains(required) || process.contains(required),
            "missing actual process evidence {required}"
        );
    }
    assert!(!process.contains("let _ = unsafe { TerminateProcess"));
    assert!(!source.contains("numeric_exit: 30"));
    assert!(!source.contains("killed: true"));
    assert!(!source.contains("reaped: true"));
}

#[test]
fn controller_always_reports_cleanup_and_removes_secret_environment() {
    let source = fs::read_to_string(crate_root().join("src/o020_controller.rs")).unwrap();
    let process = fs::read_to_string(crate_root().join("src/o020_controller_process.rs")).unwrap();
    let windows = fs::read_to_string(crate_root().join("src/credential_windows.rs")).unwrap();
    assert!(source.contains("primary_error"));
    assert!(source.contains("cleanup_error"));
    assert!(source.contains("cleanup_attempted"));
    assert!(source.contains("all_slots_absent"));
    assert!(process.contains("env_remove(SIDECAR_CREDENTIAL_ENV)"));
    assert!(windows.contains("first_cleanup_error"));
    assert!(!source.contains("let _ = store.revoke_for_cleanup()"));
}

#[test]
fn manifest_omits_secret_hashes_and_raw_sensitive_identifiers() {
    let source = fs::read_to_string(crate_root().join("src/o020_controller.rs")).unwrap();
    let process = fs::read_to_string(crate_root().join("src/o020_controller_process.rs")).unwrap();
    for forbidden in [
        "expected_active_hash",
        "observed_active_hash",
        "secret_hash",
        "artifact_path: format!",
    ] {
        assert!(
            !source.contains(forbidden),
            "leaky manifest field {forbidden}"
        );
    }
    assert!(source.contains("constant_time_equal"));
    assert!(process.contains("forbidden_token_detected"));
    assert!(process.contains("env_remove(SIDECAR_CREDENTIAL_ENV)"));
}

#[test]
fn controller_distinguishes_reached_child_timeout_and_wait_failure() {
    let source = fs::read_to_string(crate_root().join("src/o020_controller.rs")).unwrap();
    for disposition in [
        "WAIT_OBJECT_0",
        "WAIT_EVENT(WAIT_OBJECT_0.0 + 1)",
        "WAIT_TIMEOUT",
        "WAIT_FAILED",
    ] {
        assert!(
            source.contains(disposition),
            "missing wait disposition {disposition}"
        );
    }
    let child_first = source
        .find("state == WAIT_EVENT(WAIT_OBJECT_0.0 + 1)")
        .unwrap();
    let next_branch = source[child_first..]
        .find("if state == WAIT_TIMEOUT")
        .unwrap()
        + child_first;
    assert!(!source[child_first..next_branch].contains("terminate_and_reap"));
}

#[test]
fn controller_source_files_stay_within_size_gate() {
    for relative in [
        "src/o020_controller.rs",
        "src/bin/o020_credential_controller.rs",
        "src/o020_controller_process.rs",
    ] {
        let source = fs::read_to_string(crate_root().join(relative)).unwrap();
        assert!(source.lines().count() <= 300, "oversized {relative}");
    }
}
