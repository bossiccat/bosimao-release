use std::path::Path;
#[cfg(windows)]
use std::process::Command;

#[test]
fn o020_probe_is_feature_gated_and_uses_stdin_only() {
    let cargo = include_str!("../Cargo.toml");
    assert!(cargo.contains("name = \"o020_credential_probe\""));
    assert!(cargo.contains("required-features = [\"credential-test-support\"]"));
    let source = include_str!("../src/bin/o020_credential_probe.rs");
    assert!(source.contains("std::io::stdin"));
    assert!(!source.contains("std::env::set_var"));
    assert!(!source.contains("Command::env"));
    assert!(!source.contains("std::fs::read"));
    assert!(!source.contains("println!(\"{}\", secret"));
}

#[test]
fn o020_probe_argv_is_non_sensitive_and_contract_is_stable() {
    let source = include_str!("../src/bin/o020_credential_probe.rs");
    for operation in [
        "provision",
        "load",
        "rotate",
        "revoke",
        "hold-lock",
        "crash",
    ] {
        assert!(
            source.contains(&format!("\"{operation}\"")),
            "missing operation {operation}"
        );
    }
    for checkpoint in [
        "stage-write",
        "backup-write",
        "active-write",
        "active-verify",
        "delete-backup",
        "delete-staging",
    ] {
        let controller = include_str!("../src/o020_probe.rs");
        assert!(
            controller.contains(&format!("\"{checkpoint}\"")),
            "missing checkpoint {checkpoint}"
        );
    }
    for flag in ["--op", "--suffix", "--checkpoint", "--barrier", "--hold-ms"] {
        assert!(source.contains(flag), "missing flag {flag}");
    }
    for (name, code) in [
        ("SUCCESS", 0),
        ("INVALID_ARGS", 10),
        ("SECRET_INPUT_INVALID", 12),
        ("CREDENTIAL_MISSING", 20),
        ("CREDENTIAL_BUSY_OR_TIMEOUT", 25),
        ("CHECKPOINT_REACHED", 30),
        ("INTERNAL_FAIL", 40),
    ] {
        assert!(source.contains(name), "missing status {name}");
        assert!(
            source.contains(&format!("{name}: u8 = {code}")),
            "wrong exit code for {name}"
        );
    }
    assert!(!source.contains("eprintln!(\"{:?}\","));
}

#[cfg(windows)]
#[test]
fn o020_probe_rejects_positional_cli_with_invalid_args_exit() {
    let output = Command::new(env!("CARGO_BIN_EXE_o020_credential_probe"))
        .args(["load", "contract-test"])
        .output()
        .expect("run probe");
    assert_eq!(output.status.code(), Some(10));
    let stdout: serde_json::Value = serde_json::from_slice(&output.stdout).expect("JSON stdout");
    assert_eq!(stdout["status"], "INVALID_ARGS");
    assert_eq!(stdout["exit"], 10);
    assert_eq!(stdout["op"], "invalid");
}

#[test]
fn o020_probe_uses_one_named_mutex_and_real_rotation_seam() {
    let controller = include_str!("../src/o020_probe.rs");
    assert_eq!(controller.matches("PROBE_MUTEX_PREFIX").count(), 0);
    assert!(controller.contains("rotate_with_hook"));
    assert!(!controller.contains("write_slot_for_test(CredentialSlot::Staging"));
}

#[test]
fn crash_checkpoint_requires_controller_barrier_and_never_self_exits() {
    let binary = include_str!("../src/bin/o020_credential_probe.rs");
    let controller = include_str!("../src/o020_probe.rs");
    let barrier = include_str!("../src/o020_crash_barrier.rs");
    assert!(binary.contains("barrier: String"));
    assert!(binary.contains("&args.barrier"));
    assert!(!controller.contains("std::process::exit"));
    for primitive in ["CreateEventW", "SetEvent", "WaitForSingleObject"] {
        assert!(
            barrier.contains(primitive),
            "missing barrier primitive {primitive}"
        );
    }
}

#[test]
fn active_write_and_verify_are_distinct_checkpoint_boundaries() {
    let source = include_str!("../src/credential_transaction.rs");
    let write = source
        .find(".write(CredentialSlot::Active, replacement)")
        .unwrap();
    let written = source[write..]
        .find("checkpoint(CredentialSlot::Active, false)")
        .unwrap()
        + write;
    let verify = source[written..]
        .find("self.verify(CredentialSlot::Active, replacement)")
        .unwrap()
        + written;
    let verified = source[verify..]
        .find("checkpoint(CredentialSlot::Active, true)")
        .unwrap()
        + verify;
    assert!(write < written && written < verify && verify < verified);
}

#[cfg(windows)]
#[test]
fn o020_probe_uses_credential_manager_and_named_mutex() {
    let controller = include_str!("../src/o020_probe.rs");
    assert!(controller.contains("WindowsCredentialStore"));
    let lock = include_str!("../src/credential_windows_lock.rs");
    for primitive in ["CreateMutexW", "WaitForSingleObject"] {
        assert!(
            lock.contains(primitive),
            "missing Win32 primitive {primitive}"
        );
    }
    let backend = include_str!("../src/credential_windows_backend.rs");
    for primitive in ["CredReadW", "CredWriteW", "CredDeleteW"] {
        assert!(
            backend.contains(primitive),
            "missing Win32 primitive {primitive}"
        );
    }
}

#[test]
fn o020_probe_source_is_small_and_is_not_default_binary() {
    let source = include_str!("../src/bin/o020_credential_probe.rs");
    assert!(source.lines().count() <= 300);
    assert!(include_str!("../src/o020_probe.rs").lines().count() <= 300);
    assert!(
        include_str!("../src/credential_windows_lock.rs")
            .lines()
            .count()
            <= 300
    );
    let cargo = include_str!("../Cargo.toml");
    assert!(cargo.contains("default = [\"custom-protocol\"]"));
    assert!(cargo.contains("required-features = [\"credential-test-support\"]"));
    let _ = Path::new("o020_credential_probe.rs");
}
