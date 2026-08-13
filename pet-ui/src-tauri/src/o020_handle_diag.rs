#![cfg(all(windows, feature = "credential-test-support"))]

//! O-020 句柄泄漏诊断（test-only）：逐步执行 controller 真实操作并打印句柄增量，
//! 用于定位 residual_handles 归因。不参与生产路径。

use std::process::Command;

use zeroize::Zeroizing;

use crate::credential::SecretString;
use crate::credential_windows::{CredentialSlot, WindowsCredentialStore};
use crate::o020_controller_evidence::EvidenceRun;
use crate::o020_controller_process::{run_probe, spawn_probe, terminate_and_reap};
use crate::o020_crash_barrier::ControllerBarrier;

fn handles() -> u32 {
    let mut count = 0;
    unsafe {
        windows::Win32::System::Threading::GetProcessHandleCount(
            windows::Win32::System::Threading::GetCurrentProcess(),
            &mut count,
        )
    }
    .unwrap();
    count
}

fn step(label: &str, before: u32) -> u32 {
    let after = handles();
    println!("{label}: delta {}", after as i64 - before as i64);
    after
}

pub fn run_diagnostic() {
    let mut before = handles();
    let secret = SecretString::parse_test_bytes(Zeroizing::new(
        b"0123456789abcdef0123456789abcdef0123456789abcdef".to_vec(),
    ))
    .unwrap();
    before = step("baseline", before);

    let run_id = format!("diag-{}", std::process::id());
    let evidence = EvidenceRun::create(&run_id).unwrap();
    before = step("evidence_create", before);

    let suffix = format!("diags-{}", std::process::id());
    let store = WindowsCredentialStore::for_test_suffix(&suffix).unwrap();
    before = step("store_create", before);

    let _ = store.slot_exists_for_test(CredentialSlot::Active);
    before = step("slot_exists(active)", before);

    let _ = store.revoke_for_cleanup();
    before = step("revoke_for_cleanup", before);

    let barrier = ControllerBarrier::create(&format!("diagb-{}", std::process::id())).unwrap();
    before = step("barrier_create", before);
    drop(barrier);
    before = step("barrier_drop", before);

    let mut child = Command::new("cmd.exe")
        .args(["/C", "exit", "0"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    child.wait().unwrap();
    drop(child);
    before = step("cmd_spawn_wait_drop", before);

    if let Ok(outcome) = run_probe("load", &suffix, None, None, secret.expose().as_bytes()) {
        println!("probe load exit={}", outcome.actual_exit);
    }
    before = step("probe_load", before);

    let checkpoint_json = serde_json::json!({ "diag": true });
    let _ = evidence.write_manifest(&checkpoint_json);
    before = step("write_manifest", before);

    let _ = spawn_probe("load", &suffix, None, None, secret.expose().as_bytes())
        .map(terminate_and_reap);
    before = step("spawn+terminate_and_reap", before);

    // 模拟一轮完整 case（与 run_case 相同的真实操作序列）
    let case_suffix = format!("diagc-{}", std::process::id());
    if let Ok(o) = run_probe(
        "provision",
        &case_suffix,
        None,
        None,
        secret.expose().as_bytes(),
    ) {
        println!("case: provision exit={}", o.actual_exit);
    }
    before = step("case: provision", before);
    let barrier_id = format!("diagcb-{}", std::process::id());
    let barrier = ControllerBarrier::create(&barrier_id).unwrap();
    let child = spawn_probe(
        "crash",
        &case_suffix,
        Some("stage-write"),
        Some(&barrier_id),
        secret.expose().as_bytes(),
    )
    .unwrap();
    let _ = terminate_and_reap(child);
    drop(barrier);
    before = step("case: barrier+crash+reap", before);
    if let Ok(o) = run_probe("load", &case_suffix, None, None, &[]) {
        println!("case: load exit={}", o.actual_exit);
    }
    before = step("case: load", before);
    let case_store = WindowsCredentialStore::for_test_suffix(&case_suffix).unwrap();
    let _ = case_store.revoke_for_cleanup();
    before = step("case: cleanup", before);

    println!("final handles={}", handles());
}
