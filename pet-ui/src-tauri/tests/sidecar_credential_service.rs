mod support;

use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use jax_pet::credential::{
    CredentialError, CredentialErrorCode, CredentialProvider, CredentialStatus, SecretString,
};
use jax_pet::sidecar::{SidecarSpec, SidecarState, SidecarSupervisor};
use jax_pet::sidecar_credential::{SidecarCredentialService, SidecarLaunchError};

const SECRET: &[u8; 32] = b"test-credential-32-bytes-value!!";

#[derive(Default)]
struct ProviderState {
    loads: usize,
    revokes: usize,
    results: VecDeque<Result<Vec<u8>, CredentialError>>,
    stopped_file: Option<PathBuf>,
}

#[derive(Clone)]
struct FakeProvider(Arc<Mutex<ProviderState>>);

impl FakeProvider {
    fn with(results: Vec<Result<Vec<u8>, CredentialError>>) -> Self {
        Self(Arc::new(Mutex::new(ProviderState {
            results: results.into(),
            ..ProviderState::default()
        })))
    }

    fn loads(&self) -> usize {
        self.0.lock().unwrap().loads
    }
    fn revokes(&self) -> usize {
        self.0.lock().unwrap().revokes
    }
    fn require_stopped_file(&self, path: PathBuf) {
        self.0.lock().unwrap().stopped_file = Some(path);
    }
}

impl CredentialProvider for FakeProvider {
    fn status(&self) -> CredentialStatus {
        CredentialStatus::Ready
    }
    fn load_active(&self) -> Result<SecretString, CredentialError> {
        let mut state = self.0.lock().unwrap();
        state.loads += 1;
        let value = state
            .results
            .pop_front()
            .unwrap_or_else(|| Ok(SECRET.to_vec()))?;
        SecretString::parse_utf8(value)
    }
    fn provision(&self, _: SecretString) -> Result<(), CredentialError> {
        Ok(())
    }
    fn rotate(&self, _: SecretString) -> Result<(), CredentialError> {
        Ok(())
    }
    fn revoke(&self) -> Result<(), CredentialError> {
        let mut state = self.0.lock().unwrap();
        if let Some(path) = &state.stopped_file {
            assert!(path.exists(), "child must stop before credential deletion");
        }
        state.revokes += 1;
        Ok(())
    }
}

#[test]
fn credential_stub() {
    let args: Vec<String> = std::env::args().collect();
    let Some(report) = arg_value(&args, "--stub-report=") else {
        return;
    };
    let credential_in_argv = args.iter().any(|arg| arg.as_bytes() == SECRET);
    let injected = std::env::var("VOICE_SIDECAR_CREDENTIAL").is_ok();
    std::fs::write(
        &report,
        format!("argv_secret={credential_in_argv}\nenv_secret={injected}"),
    )
    .unwrap();
    let mut line = String::new();
    let _ = std::io::stdin().read_line(&mut line);
    if let Some(stopped) = arg_value(&args, "--stub-stopped=") {
        std::fs::write(stopped, "stopped").unwrap();
    }
}

fn arg_value(args: &[String], prefix: &str) -> Option<String> {
    args.iter()
        .find_map(|arg| arg.strip_prefix(prefix).map(str::to_owned))
}

fn supervisor(tag: &str, hash: String, stopped: Option<&PathBuf>) -> (SidecarSupervisor, PathBuf) {
    let report = std::env::temp_dir().join(format!(
        "credential-report-{tag}-{}.txt",
        std::process::id()
    ));
    let mut args = vec![
        "--exact".into(),
        "credential_stub".into(),
        "--nocapture".into(),
        "--".into(),
        format!("--stub-report={}", report.display()),
        "--role=sidecar".into(),
    ];
    if let Some(path) = stopped {
        args.push(format!("--stub-stopped={}", path.display()));
    }
    let fixture = support::sidecar_fixture();
    let expected_sha256 = if hash.len() == 64 && hash.chars().all(|byte| byte == '0') {
        hash
    } else {
        binary_hash_for(&fixture.binary_path)
    };
    (
        SidecarSupervisor::new(SidecarSpec {
            binary_path: fixture.binary_path,
            expected_sha256,
            integrity: fixture.integrity,
            args,
            ca_cert_path: PathBuf::from("certs/ca.crt"),
            graceful_timeout: Duration::from_secs(5),
            kill_timeout: Duration::from_secs(1),
        }),
        report,
    )
}

fn binary_hash() -> String {
    binary_hash_for(&std::env::current_exe().unwrap())
}

fn binary_hash_for(path: &std::path::Path) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(std::fs::read(path).unwrap()))
}

fn wait_exit(supervisor: &mut SidecarSupervisor) {
    for _ in 0..100 {
        if supervisor.try_wait().is_some() {
            return;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn invalid_binary_never_reads_credential() {
    let provider = FakeProvider::with(vec![Ok(SECRET.to_vec())]);
    let mut service = SidecarCredentialService::new(provider.clone());
    let fixture = support::sidecar_fixture();
    let mut missing = SidecarSupervisor::new(SidecarSpec {
        binary_path: PathBuf::from("missing-sidecar.exe"),
        expected_sha256: String::new(),
        integrity: fixture.integrity,
        args: vec!["--role=sidecar".into()],
        ca_cert_path: PathBuf::from("certs/ca.crt"),
        graceful_timeout: Duration::from_millis(1),
        kill_timeout: Duration::from_millis(1),
    });
    assert!(service.start_initial(&mut missing).is_err());
    let (mut mismatch, _) = supervisor("hash", "0".repeat(64), None);
    assert!(service.start_initial(&mut mismatch).is_err());
    assert_eq!(provider.loads(), 0);
}

#[test]
fn missing_or_corrupt_credential_never_spawns() {
    for error in [
        CredentialErrorCode::CredentialMissing,
        CredentialErrorCode::CredentialCorrupt,
    ] {
        let provider = FakeProvider::with(vec![Err(CredentialError::new(error))]);
        let mut service = SidecarCredentialService::new(provider);
        let (mut supervisor, report) = supervisor("invalid-credential", binary_hash(), None);
        assert!(service.start_initial(&mut supervisor).is_err());
        assert_eq!(supervisor.state(), SidecarState::Stopped);
        assert!(!report.exists());
    }
}

#[test]
fn every_unexpected_restart_reloads_and_child_receives_only_narrow_secret() {
    let provider = FakeProvider::with(vec![Ok(SECRET.to_vec()); 3]);
    let mut service = SidecarCredentialService::new(provider.clone());
    let (mut supervisor, report) = supervisor("restart", binary_hash(), None);
    service.start_initial(&mut supervisor).unwrap();
    supervisor.stop().unwrap();
    service
        .restart_after_unexpected_exit(&mut supervisor)
        .unwrap();
    supervisor.stop().unwrap();
    service
        .restart_after_unexpected_exit(&mut supervisor)
        .unwrap();
    assert_eq!(provider.loads(), 3);
    for _ in 0..100 {
        if report.exists() {
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    let report = std::fs::read_to_string(report).unwrap();
    assert!(report.contains("argv_secret=false"));
    assert!(report.contains("env_secret=true"));
    supervisor.stop().unwrap();
}

#[test]
fn never_ran_user_stop_and_revoked_refuse_restart_without_reading() {
    let provider = FakeProvider::with(vec![Ok(SECRET.to_vec()); 2]);
    let mut service = SidecarCredentialService::new(provider.clone());
    let (mut supervisor, _) = supervisor("policies", binary_hash(), None);
    assert!(matches!(
        service.restart_after_unexpected_exit(&mut supervisor),
        Err(SidecarLaunchError::RestartNotAllowed)
    ));
    assert_eq!(provider.loads(), 0);
    service.start_initial(&mut supervisor).unwrap();
    service.stop(&mut supervisor).unwrap();
    assert!(service
        .restart_after_unexpected_exit(&mut supervisor)
        .is_err());
    assert_eq!(provider.loads(), 1);
    service.revoke_and_stop(&mut supervisor).unwrap();
    assert!(service
        .restart_after_unexpected_exit(&mut supervisor)
        .is_err());
    assert_eq!(provider.loads(), 1);
}

#[test]
fn revoke_stops_before_delete_and_is_idempotent() {
    let stopped =
        std::env::temp_dir().join(format!("credential-stopped-{}.txt", std::process::id()));
    let _ = std::fs::remove_file(&stopped);
    let provider = FakeProvider::with(vec![Ok(SECRET.to_vec())]);
    provider.require_stopped_file(stopped.clone());
    let mut service = SidecarCredentialService::new(provider.clone());
    let (mut supervisor, _) = supervisor("revoke", binary_hash(), Some(&stopped));
    service.start_initial(&mut supervisor).unwrap();
    service.revoke_and_stop(&mut supervisor).unwrap();
    service.revoke_and_stop(&mut supervisor).unwrap();
    assert_eq!(provider.revokes(), 1);
    assert!(!service.run_policy().restart_allowed && service.run_policy().revoked);
    wait_exit(&mut supervisor);
}
