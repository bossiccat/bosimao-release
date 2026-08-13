#![cfg(all(windows, feature = "credential-test-support"))]

use std::time::Instant;

use serde::Serialize;
use sha2::{Digest, Sha256};
use windows::Win32::Foundation::{WAIT_EVENT, WAIT_FAILED, WAIT_OBJECT_0, WAIT_TIMEOUT};
use windows::Win32::Security::Cryptography::{BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG};
use windows::Win32::System::Threading::{GetProcessHandleCount, WaitForMultipleObjects};
use zeroize::Zeroizing;

use crate::credential::{CredentialError, CredentialErrorCode, SecretString};
use crate::credential_windows::{CredentialSlot, WindowsCredentialStore};
use crate::o020_controller_evidence::{ArtifactReference, EvidenceRun};
use crate::o020_controller_process::{
    alive_process_count, run_probe, spawn_probe, terminate_and_reap, ChildOutputEvidence,
};
use crate::o020_crash_barrier::ControllerBarrier;

const BARRIER_WAIT_MS: u32 = 10_000;
const CHECKPOINTS: [&str; 6] = [
    "stage-write",
    "backup-write",
    "active-write",
    "active-verify",
    "delete-backup",
    "delete-staging",
];

#[derive(Serialize)]
#[rustfmt::skip]
pub struct SlotPresence { active: bool, staging: bool, backup: bool }

#[derive(Serialize)]
#[rustfmt::skip]
pub struct CleanupEvidence { cleanup_attempted: bool, revoked: bool, all_slots_absent: bool, residual_processes: u32, residual_handles: i64, cleanup_error: Option<&'static str> }

#[derive(Serialize)]
#[rustfmt::skip]
struct BarrierEvidence { barrier_id_hash: String, reached_at_ms: u128, kill_at_ms: u128, reap_at_ms: u128, reached: bool, killed: bool, reaped: bool, release_signaled: bool }

#[derive(Serialize)]
#[rustfmt::skip]
pub struct CheckpointEvidence { schema_version: u8, case_id: String, checkpoint: &'static str, numeric_exit: i32, child_pid: u32, slot_presence: SlotPresence, recovered: bool, barrier: BarrierEvidence, streams: ChildOutputEvidence, behavior_status: &'static str, primary_error: Option<&'static str> }

#[derive(Serialize)]
#[rustfmt::skip]
pub struct ControllerManifest { schema_version: u8, run_id: String, evidence_root_policy: &'static str, controller_build_hash: String, probe_build_hash: String, expected_checkpoints: [&'static str; 6], checkpoints: Vec<CheckpointEvidence>, artifacts: Vec<ArtifactReference>, artifact_status: &'static str, behavior_status: &'static str, cleanup: CleanupEvidence, overall_status: &'static str, blocking_reason: Option<&'static str> }

/// 六 checkpoint 真实矩阵：provision -> crash(checkpoint) -> hard-kill -> reap -> load 恢复
/// -> active 恒时比较 -> cleanup。失败路径仍尽力落盘 partial manifest 并返回 Err。
#[rustfmt::skip]
pub fn run_matrix(secret_bytes: Zeroizing<Vec<u8>>) -> Result<ControllerManifest, CredentialError> {
    let secret = SecretString::parse_test_bytes(secret_bytes)?;
    let replacement = replacement_for(&secret)?;
    let run_id = random_id("run")?;
    let evidence_run = EvidenceRun::create(&run_id)?;
    let mut pids = Vec::new();
    // 归因预热：进程运行时与 Credential Manager 的一次性初始化吸收进句柄基线，
    // 避免首次 spawn / 首次 CredWriteW 的缓存句柄污染 residual_handles 观测。
    if let Ok(outcome) = run_probe("load", &run_id, None, None, &[]) { pids.push(outcome.pid); }
    let warmup_suffix = random_id("warm")?;
    if let Ok(outcome) = run_probe("provision", &warmup_suffix, None, None, secret.expose().as_bytes()) { pids.push(outcome.pid); }
    if let Ok(store) = WindowsCredentialStore::for_test_suffix(&warmup_suffix) {
        let _ = store.load_slot_for_test(CredentialSlot::Active);
        let _ = store.slot_exists_for_test(CredentialSlot::Active);
        match store.revoke_for_cleanup() {
            Ok(()) => {}
            Err(_) => {}
        }
    }
    let _ = run_probe("revoke", &warmup_suffix, None, None, &[]);
    let handles_before = handle_count()?;
    let controller_build_hash = hash_file(std::env::current_exe().map_err(|_| controller_error())?)?;
    let probe_build_hash = hash_file(crate::o020_controller_process::fixed_probe_path()?)?;
    let mut checkpoints = Vec::with_capacity(CHECKPOINTS.len());
    let mut artifacts = Vec::with_capacity(CHECKPOINTS.len());
    let mut primary_error = None;
    let mut cleanup_error = None;
    let mut revoked_ok = true;
    let mut slots_absent_observed = true;
    for (index, checkpoint) in CHECKPOINTS.iter().copied().enumerate() {
        if primary_error.is_some() { break; }
        let suffix = match random_id("case") { Ok(v) => v, Err(_) => { primary_error = Some("RANDOM_ID_FAILED"); break; } };
        let barrier = match random_id("barrier") { Ok(v) => v, Err(_) => { primary_error = Some("RANDOM_ID_FAILED"); break; } };
        let store = match WindowsCredentialStore::for_test_suffix(&suffix) { Ok(v) => v, Err(_) => { primary_error = Some("STORE_CREATE_FAILED"); break; } };
        let (case_result, case_pids) = run_case(&store, &suffix, &barrier, checkpoint, &secret, &replacement, format!("{run_id}:{:02}", index + 1));
        pids.extend(case_pids);
        let cleanup_failed = cleanup_case(&store).is_err();
        if cleanup_failed { revoked_ok = false; if cleanup_error.is_none() { cleanup_error = Some("CLEANUP_FAILED"); } }
        match slot_presence(&store) {
            Ok(p) if !p.active && !p.staging && !p.backup => {}
            _ => slots_absent_observed = false,
        }
        match case_result {
            Ok(mut evidence) => {
                if evidence.behavior_status != "pass" || cleanup_failed {
                    if evidence.behavior_status == "pass" { evidence.behavior_status = "fail"; evidence.primary_error = Some("CLEANUP_FAILED"); }
                    if primary_error.is_none() { primary_error = Some("CASE_OR_CLEANUP_FAILED"); }
                    if let Ok(artifact) = evidence_run.write_checkpoint(index + 1, checkpoint, &evidence) { artifacts.push(artifact); }
                    checkpoints.push(evidence);
                    break;
                }
                match evidence_run.write_checkpoint(index + 1, checkpoint, &evidence) {
                    Ok(artifact) => artifacts.push(artifact),
                    Err(_) => { evidence.behavior_status = "fail"; evidence.primary_error = Some("ARTIFACT_WRITE_FAILED"); if primary_error.is_none() { primary_error = Some("ARTIFACT_WRITE_FAILED"); } }
                }
                checkpoints.push(evidence);
            }
            Err(_) => { if primary_error.is_none() { primary_error = Some("CASE_FAILED"); } break; }
        }
    }
    let residual_processes = alive_process_count(&pids).unwrap_or_else(|_| { if cleanup_error.is_none() { cleanup_error = Some("RESIDUAL_SCAN_FAILED"); } u32::MAX });
    let residual_handles = i64::from(handle_count()?) - i64::from(handles_before);
    let behavior_pass = checkpoints.len() == CHECKPOINTS.len() && artifacts.len() == CHECKPOINTS.len() && checkpoints.iter().all(|e| e.behavior_status == "pass") && primary_error.is_none();
    let cleanup_pass = cleanup_error.is_none() && revoked_ok && slots_absent_observed && residual_processes == 0 && residual_handles == 0;
    let complete = behavior_pass && cleanup_pass;
    let blocking_reason = if complete { None } else { primary_error.or(cleanup_error).or(Some("RESIDUAL_OR_CLEANUP_NOT_PASS")) };
    let artifact_count = artifacts.len();
    let manifest = ControllerManifest {
        schema_version: 1, run_id, evidence_root_policy: "local-app-data-v1",
        controller_build_hash, probe_build_hash, expected_checkpoints: CHECKPOINTS,
        checkpoints, artifacts,
        artifact_status: if artifact_count == CHECKPOINTS.len() { "complete" } else { "partial" },
        behavior_status: if behavior_pass { "pass" } else { "not-observed" },
        cleanup: CleanupEvidence { cleanup_attempted: true, revoked: revoked_ok, all_slots_absent: slots_absent_observed, residual_processes, residual_handles, cleanup_error },
        overall_status: if complete { "observed-pass" } else { "blocked" },
        blocking_reason,
    };
    // 无论成败都尽力持久化 manifest（失败时为 partial），stdout 由 bin 层输出。
    let _ = evidence_run.write_manifest(&manifest);
    if complete { Ok(manifest) } else { Err(controller_error()) }
}

#[rustfmt::skip]
fn run_case(store: &WindowsCredentialStore, suffix: &str, barrier: &str, checkpoint: &'static str, original: &SecretString, replacement: &SecretString, case_id: String) -> (Result<CheckpointEvidence, CredentialError>, Vec<u32>) {
    let mut pids = Vec::new();
    let provision = match run_probe("provision", suffix, None, None, original.expose().as_bytes()) { Ok(o) => { pids.push(o.pid); o }, Err(e) => return (Err(e), pids) };
    if provision.actual_exit != 0 || !provision.reaped { return (Err(controller_error()), pids); }
    let started = Instant::now();
    let barrier_owner = match ControllerBarrier::create(barrier) { Ok(o) => o, Err(e) => return (Err(e), pids) };
    let child = match spawn_probe("crash", suffix, Some(checkpoint), Some(barrier), replacement.expose().as_bytes()) { Ok(c) => c, Err(e) => return (Err(e), pids) };
    pids.push(child.pid());
    let state = unsafe { WaitForMultipleObjects(&[barrier_owner.reached, child.raw_handle()], false, BARRIER_WAIT_MS) };
    let disposition = if state == WAIT_OBJECT_0 { "reached" } else if state == WAIT_EVENT(WAIT_OBJECT_0.0 + 1) { "child-first" } else if state == WAIT_TIMEOUT { "timeout" } else if state == WAIT_FAILED { "wait-failed" } else { "wait-failed" };
    let reached = state == WAIT_OBJECT_0;
    // 所有分支统一走真实回收：child-first/timeout/wait-failed 也必须有界 reap 并记录真实 outcome。
    let outcome = match terminate_and_reap(child) { Ok(o) => o, Err(e) => return (Err(e), pids) };
    let barrier_evidence = BarrierEvidence {
        barrier_id_hash: format!("{:x}", Sha256::digest(barrier.as_bytes())),
        reached_at_ms: if reached { started.elapsed().as_millis() } else { 0 },
        kill_at_ms: started.elapsed().as_millis(),
        reap_at_ms: started.elapsed().as_millis(),
        reached, killed: outcome.killed, reaped: outcome.reaped, release_signaled: false,
    };
    if !reached {
        // 负面路径 fail-closed：真实 outcome 与 disposition 进入证据，行为判 fail。
        let evidence = CheckpointEvidence {
            schema_version: 1, case_id, checkpoint,
            numeric_exit: outcome.actual_exit, child_pid: outcome.pid,
            slot_presence: slot_presence(store).unwrap_or(SlotPresence { active: false, staging: false, backup: false }),
            recovered: false, barrier: barrier_evidence, streams: outcome.output,
            behavior_status: "fail", primary_error: Some(disposition),
        };
        return (Ok(evidence), pids);
    }
    if !outcome.killed || !outcome.reaped || outcome.actual_exit != 30 || !outcome.output.clean() { return (Err(controller_error()), pids); }
    let load = match run_probe("load", suffix, None, None, &[]) { Ok(o) => { pids.push(o.pid); o }, Err(e) => return (Err(e), pids) };
    if load.actual_exit != 0 || !load.reaped { return (Err(controller_error()), pids); }
    let observed = match store.load_slot_for_test(CredentialSlot::Active) { Ok(v) => v, Err(e) => return (Err(e), pids) };
    let expected = expected_active(checkpoint, original, replacement);
    let recovered = observed.as_ref().map(|v| constant_time_equal(v.expose().as_bytes(), expected.expose().as_bytes())).unwrap_or(false);
    if !recovered { return (Err(controller_error()), pids); }
    let evidence = CheckpointEvidence {
        schema_version: 1, case_id, checkpoint,
        numeric_exit: outcome.actual_exit, child_pid: outcome.pid,
        slot_presence: slot_presence(store).unwrap_or(SlotPresence { active: false, staging: false, backup: false }),
        recovered, barrier: barrier_evidence, streams: outcome.output,
        behavior_status: "pass", primary_error: None,
    };
    (Ok(evidence), pids)
}

#[rustfmt::skip]
fn cleanup_case(store: &WindowsCredentialStore) -> Result<(), CredentialError> { let e = store.revoke_for_cleanup().err(); let absent = all_slots_absent(store).unwrap_or(false); match (e, absent) { (None, true) => Ok(()), _ => Err(controller_error()) } }
#[rustfmt::skip]
fn slot_presence(store: &WindowsCredentialStore) -> Result<SlotPresence, CredentialError> { Ok(SlotPresence { active: store.slot_exists_for_test(CredentialSlot::Active)?, staging: store.slot_exists_for_test(CredentialSlot::Staging)?, backup: store.slot_exists_for_test(CredentialSlot::Backup)? }) }
#[rustfmt::skip]
fn all_slots_absent(store: &WindowsCredentialStore) -> Result<bool, CredentialError> { let s = slot_presence(store)?; Ok(!s.active && !s.staging && !s.backup) }
#[rustfmt::skip]
fn hash_file(path: impl AsRef<std::path::Path>) -> Result<String, CredentialError> { let b = std::fs::read(path).map_err(|_| controller_error())?; Ok(format!("{:x}", Sha256::digest(b))) }
#[rustfmt::skip]
fn handle_count() -> Result<u32, CredentialError> { let mut c = 0; unsafe { GetProcessHandleCount(windows::Win32::System::Threading::GetCurrentProcess(), &mut c) }.map_err(|_| controller_error())?; Ok(c) }
#[rustfmt::skip]
fn random_id(prefix: &str) -> Result<String, CredentialError> { let mut r = [0_u8; 16]; if unsafe { BCryptGenRandom(None, &mut r, BCRYPT_USE_SYSTEM_PREFERRED_RNG) }.is_err() { return Err(controller_error()); } Ok(format!("{prefix}-{:x}", Sha256::digest(r))) }
#[rustfmt::skip]
fn replacement_for(original: &SecretString) -> Result<SecretString, CredentialError> { let mut b = Zeroizing::new(original.expose().as_bytes().to_vec()); b[0] = if b[0] == b'Z' { b'Y' } else { b'Z' }; SecretString::parse_test_bytes(b) }
#[rustfmt::skip]
fn expected_active<'a>(checkpoint: &str, original: &'a SecretString, replacement: &'a SecretString) -> &'a SecretString { match checkpoint { "active-write" | "active-verify" | "delete-backup" | "delete-staging" => replacement, _ => original } }
#[rustfmt::skip]
fn constant_time_equal(left: &[u8], right: &[u8]) -> bool { left.len() == right.len() && left.iter().zip(right).fold(0_u8, |d, (a, b)| d | (a ^ b)) == 0 }
#[rustfmt::skip]
fn controller_error() -> CredentialError { CredentialError::new(CredentialErrorCode::CredentialRecoveryFailed) }
