//! O-018 切片 2：安全父进程编排契约。
//!
//! 先以跨平台 fake 证明稳定行为，再由 Windows 集成层证明 CreatePipe /
//! inherited stdin 的真实实现；任何失败都必须 fail-closed。

use std::sync::Mutex;
use std::time::Duration;

use jax_pet::credential::SecretString;
use jax_pet::provision_orchestrator::{
    ProvisionError, ProvisionOrchestrator, ProvisionTransport, HELPER_SUCCESS,
};

const SECRET: &[u8; 32] = b"o018-slice2-test-secret-value!!!";

#[derive(Debug, Clone, Copy)]
enum FakeResult {
    Exit(u32),
    SpawnFailed,
    WriteFailed,
    TimedOut,
}

struct FakeTransport {
    result: FakeResult,
    calls: Mutex<Vec<(usize, Duration)>>,
}

impl FakeTransport {
    fn new(result: FakeResult) -> Self {
        Self {
            result,
            calls: Mutex::new(Vec::new()),
        }
    }
}

impl ProvisionTransport for FakeTransport {
    fn run(&self, secret: &SecretString, timeout: Duration) -> Result<u32, ProvisionError> {
        self.calls
            .lock()
            .expect("calls lock")
            .push((secret.expose().len(), timeout));
        match self.result {
            FakeResult::Exit(code) => Ok(code),
            FakeResult::SpawnFailed => Err(ProvisionError::SpawnFailed),
            FakeResult::WriteFailed => Err(ProvisionError::PipeWriteFailed),
            FakeResult::TimedOut => Err(ProvisionError::WaitTimedOut),
        }
    }
}

fn secret() -> SecretString {
    SecretString::parse_utf8(SECRET.to_vec()).expect("test secret is valid")
}

#[test]
fn success_requires_zero_helper_exit_and_uses_fixed_helper_path_only() {
    let transport = FakeTransport::new(FakeResult::Exit(HELPER_SUCCESS));
    let orchestrator = ProvisionOrchestrator::new(transport, Duration::from_secs(10));
    orchestrator
        .provision(secret())
        .expect("zero exit is success");

    let calls = orchestrator.transport().calls.lock().expect("calls lock");
    assert_eq!(calls.as_slice(), &[(32, Duration::from_secs(10))]);
}

#[test]
fn every_transport_failure_is_fail_closed() {
    for (result, expected) in [
        (FakeResult::SpawnFailed, ProvisionError::SpawnFailed),
        (FakeResult::WriteFailed, ProvisionError::PipeWriteFailed),
        (FakeResult::TimedOut, ProvisionError::WaitTimedOut),
    ] {
        let orchestrator =
            ProvisionOrchestrator::new(FakeTransport::new(result), Duration::from_millis(100));
        assert_eq!(orchestrator.provision(secret()), Err(expected));
    }
}

#[test]
fn nonzero_helper_exit_is_never_accepted() {
    for code in [1, 2, 3, 255] {
        let orchestrator = ProvisionOrchestrator::new(
            FakeTransport::new(FakeResult::Exit(code)),
            Duration::from_secs(1),
        );
        assert_eq!(
            orchestrator.provision(secret()),
            Err(ProvisionError::HelperFailed(code))
        );
    }
}

#[test]
fn orchestrator_has_no_secret_argv_or_environment_api() {
    let source = include_str!("../src/provision_orchestrator.rs");
    assert!(!source.contains("Command::arg"));
    assert!(!source.contains("Command::args"));
    assert!(!source.contains("Command::env"));
    assert!(!source.contains("std::env::set_var"));
    assert!(!source.contains("std::fs::write"));
    assert!(!source.contains("helper: &Path"));
    assert!(!source.contains("pub fn fixed_helper_path"));
}

#[cfg(windows)]
#[test]
fn windows_adapter_uses_inherited_stdin_and_explicit_handle_cleanup_contract() {
    let source = include_str!("../src/provision_orchestrator_windows.rs");
    for required in [
        "CreatePipe",
        "SetHandleInformation",
        "CreateProcessW",
        "STARTF_USESTDHANDLES",
        "PROC_THREAD_ATTRIBUTE_HANDLE_LIST",
        "WriteFile",
        "WaitForSingleObject",
        "TerminateProcess",
        "CloseHandle",
    ] {
        assert!(
            source.contains(required),
            "missing Windows primitive {required}"
        );
    }
    for forbidden in ["Command::env", "std::env::set_var", "std::fs::write"] {
        assert!(
            !source.contains(forbidden),
            "forbidden transport {forbidden}"
        );
    }
}

#[cfg(windows)]
#[test]
fn windows_adapter_has_valid_output_handles_and_reaps_every_started_child_failure() {
    let source = include_str!("../src/provision_orchestrator_windows.rs");
    assert!(
        source.contains("CreateFileW"),
        "stdout/stderr require valid handles"
    );
    assert!(
        source.contains("w!(\"NUL\")"),
        "helper output must not leak to logs"
    );
    assert!(!source.contains("hStdOutput = HANDLE::default()"));
    assert!(!source.contains("hStdError = HANDLE::default()"));
    assert!(source.contains("let mut output_security_descriptor ="));
    assert!(source.contains("output_security_descriptor.as_mut_ptr()"));
    assert!(source.contains("process.terminate_and_reap()?;"));
    assert!(source.contains("if let Err(error) = write_result"));
    assert!(!source.contains("let _ = process.terminate_and_reap()"));
    assert!(!source.contains("let _ = self.terminate_and_reap()"));
    let terminate = source
        .find("TerminateProcess(self.process.raw(), 3)")
        .unwrap();
    let reap_wait = source[terminate..]
        .find("WaitForSingleObject(self.process.raw(), 5_000)")
        .unwrap();
    assert!(
        reap_wait > 0,
        "reap wait must run after every terminate attempt"
    );
    assert!(!source.contains("if terminated.is_err() {\n            return Err"));
}

#[cfg(windows)]
#[test]
fn windows_pipe_handles_are_owned_before_inherit_flag_can_fail() {
    let source = include_str!("../src/provision_orchestrator_windows.rs");
    let ownership = source
        .find("let pipe = Self {")
        .expect("pipe RAII ownership");
    let inherit_flag = source
        .find("SetHandleInformation(pipe.write.raw()")
        .expect("parent write inherit flag cleared");
    assert!(
        ownership < inherit_flag,
        "both pipe handles must be RAII-owned first"
    );
}

#[cfg(windows)]
#[test]
fn windows_adapter_rejects_relative_helper_and_owns_initialized_attribute_list() {
    let source = include_str!("../src/provision_orchestrator_windows.rs");
    assert!(source.contains("if !path.is_absolute()"));
    assert!(source.contains("handles: Box<[HANDLE; 2]>"));
    assert!(source.contains("Some(attributes.handles.as_ptr().cast())"));
    assert!(source.contains("std::mem::size_of_val(attributes.handles.as_ref())"));
    assert!(!source.contains("std::mem::size_of_val(&attributes.handles)"));
    assert!(source.contains("initialized: false"));
    assert!(source.contains("attributes.initialized = true"));
    assert!(source.contains("if self.initialized"));
}
