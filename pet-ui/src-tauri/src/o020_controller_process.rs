#![cfg(all(windows, feature = "credential-test-support"))]

use std::io::{Read, Write};
use std::os::windows::io::AsRawHandle;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread::JoinHandle;

use serde::Serialize;
use sha2::{Digest, Sha256};
use windows::Win32::Foundation::{HANDLE, WAIT_OBJECT_0};
use windows::Win32::System::Threading::{
    GetExitCodeProcess, TerminateProcess, WaitForSingleObject,
};
use zeroize::Zeroizing;

use crate::credential::{CredentialError, CredentialErrorCode, SIDECAR_CREDENTIAL_ENV};

pub(crate) const REAP_WAIT_MS: u32 = 5_000;
const MAX_CHILD_OUTPUT: u64 = 16 * 1024;

#[derive(Debug, Serialize)]
pub(crate) struct StreamEvidence {
    pub(crate) digest: Option<String>,
    pub(crate) total_bytes: u64,
    pub(crate) truncated: bool,
    pub(crate) forbidden_token_detected: bool,
}

#[derive(Debug, Serialize)]
pub(crate) struct ChildOutputEvidence {
    pub(crate) stdout_digest: Option<String>,
    pub(crate) stdout_total_bytes: u64,
    pub(crate) stdout_truncated: bool,
    pub(crate) stdout_forbidden_token_detected: bool,
    pub(crate) stderr_digest: Option<String>,
    pub(crate) stderr_total_bytes: u64,
    pub(crate) stderr_truncated: bool,
    pub(crate) stderr_forbidden_token_detected: bool,
}

impl ChildOutputEvidence {
    pub(crate) fn clean(&self) -> bool {
        !self.stdout_truncated
            && !self.stderr_truncated
            && !self.stdout_forbidden_token_detected
            && !self.stderr_forbidden_token_detected
    }
}

#[derive(Debug)]
pub(crate) struct ProcessOutcome {
    pub(crate) actual_exit: i32,
    pub(crate) killed: bool,
    pub(crate) reaped: bool,
    pub(crate) pid: u32,
    pub(crate) output: ChildOutputEvidence,
}

pub(crate) struct SpawnedProbe {
    pub(crate) child: Child,
    stdout: JoinHandle<Result<StreamEvidence, CredentialError>>,
    stderr: JoinHandle<Result<StreamEvidence, CredentialError>>,
}

impl SpawnedProbe {
    pub(crate) fn raw_handle(&self) -> HANDLE {
        HANDLE(self.child.as_raw_handle() as *mut _)
    }
    pub(crate) fn pid(&self) -> u32 {
        self.child.id()
    }
    fn finish_output(self) -> Result<ChildOutputEvidence, CredentialError> {
        let stdout = self.stdout.join().map_err(|_| process_error())??;
        let stderr = self.stderr.join().map_err(|_| process_error())??;
        Ok(ChildOutputEvidence {
            stdout_digest: stdout.digest,
            stdout_total_bytes: stdout.total_bytes,
            stdout_truncated: stdout.truncated,
            stdout_forbidden_token_detected: stdout.forbidden_token_detected,
            stderr_digest: stderr.digest,
            stderr_total_bytes: stderr.total_bytes,
            stderr_truncated: stderr.truncated,
            stderr_forbidden_token_detected: stderr.forbidden_token_detected,
        })
    }
}

pub(crate) fn spawn_probe(
    operation: &str,
    suffix: &str,
    checkpoint: Option<&str>,
    barrier: Option<&str>,
    secret: &[u8],
) -> Result<SpawnedProbe, CredentialError> {
    let mut command = Command::new(fixed_probe_path()?);
    command
        .args(["--op", operation, "--suffix", suffix])
        .env_remove(SIDECAR_CREDENTIAL_ENV)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(value) = checkpoint {
        command.args(["--checkpoint", value]);
    }
    if let Some(value) = barrier {
        command.args(["--barrier", value]);
    }
    let mut child = command.spawn().map_err(|_| process_error())?;
    let mut stdin = child.stdin.take().ok_or_else(process_error)?;
    if !secret.is_empty() {
        stdin.write_all(secret).map_err(|_| process_error())?;
    }
    drop(stdin);
    let stdout = child.stdout.take().ok_or_else(process_error)?;
    let stderr = child.stderr.take().ok_or_else(process_error)?;
    let stdout_tokens = forbidden_tokens(secret, suffix, barrier);
    let stderr_tokens = forbidden_tokens(secret, suffix, barrier);
    Ok(SpawnedProbe {
        child,
        stdout: std::thread::spawn(move || drain_stream(stdout, stdout_tokens)),
        stderr: std::thread::spawn(move || drain_stream(stderr, stderr_tokens)),
    })
}

pub(crate) fn run_probe(
    operation: &str,
    suffix: &str,
    checkpoint: Option<&str>,
    barrier: Option<&str>,
    secret: &[u8],
) -> Result<ProcessOutcome, CredentialError> {
    let mut probe = spawn_probe(operation, suffix, checkpoint, barrier, secret)?;
    let pid = probe.pid();
    let status = probe.child.wait().map_err(|_| process_error())?;
    let actual_exit = status.code().ok_or_else(process_error)?;
    let output = probe.finish_output()?;
    Ok(ProcessOutcome {
        actual_exit,
        killed: false,
        reaped: true,
        pid,
        output,
    })
}

pub(crate) fn terminate_and_reap(
    mut probe: SpawnedProbe,
) -> Result<ProcessOutcome, CredentialError> {
    let handle = probe.raw_handle();
    let pid = probe.pid();
    let already_exited = probe.child.try_wait().map_err(|_| process_error())?;
    let killed = if already_exited.is_none() {
        unsafe { TerminateProcess(handle, 30) }.map_err(|_| process_error())?;
        true
    } else {
        false
    };
    if unsafe { WaitForSingleObject(handle, REAP_WAIT_MS) } != WAIT_OBJECT_0 {
        return Err(process_error());
    }
    let mut code = 0;
    unsafe { GetExitCodeProcess(handle, &mut code) }.map_err(|_| process_error())?;
    let status = probe.child.wait().map_err(|_| process_error())?;
    let actual_exit = status.code().unwrap_or(code as i32);
    let output = probe.finish_output()?;
    Ok(ProcessOutcome {
        actual_exit,
        killed,
        reaped: true,
        pid,
        output,
    })
}

fn forbidden_tokens(secret: &[u8], suffix: &str, barrier: Option<&str>) -> Vec<Zeroizing<Vec<u8>>> {
    let mut tokens = Vec::new();
    if !secret.is_empty() {
        tokens.push(Zeroizing::new(secret.to_vec()));
    }
    tokens.push(Zeroizing::new(suffix.as_bytes().to_vec()));
    if let Some(value) = barrier {
        tokens.push(Zeroizing::new(value.as_bytes().to_vec()));
    }
    for value in [
        b"JaxPet/com.jax.pet/voice-sidecar/v1".as_slice(),
        b"Global\\JaxPet.O020Barrier.v1".as_slice(),
        b"Global\\JaxPet.VoiceSidecarCredential.v1".as_slice(),
    ] {
        tokens.push(Zeroizing::new(value.to_vec()));
    }
    tokens
}

fn drain_stream(
    mut stream: impl Read,
    forbidden: Vec<Zeroizing<Vec<u8>>>,
) -> Result<StreamEvidence, CredentialError> {
    let mut total_bytes = 0_u64;
    let mut captured = Zeroizing::new(Vec::new());
    let mut buffer = [0_u8; 4096];
    loop {
        let read = stream.read(&mut buffer).map_err(|_| process_error())?;
        if read == 0 {
            break;
        }
        total_bytes = total_bytes.saturating_add(read as u64);
        let remaining = MAX_CHILD_OUTPUT.saturating_sub(captured.len() as u64) as usize;
        captured.extend_from_slice(&buffer[..read.min(remaining)]);
    }
    let truncated = total_bytes > MAX_CHILD_OUTPUT;
    let forbidden_token_detected = forbidden.iter().any(|token| {
        !token.is_empty()
            && captured
                .windows(token.len())
                .any(|window| window == token.as_slice())
    });
    let digest = if truncated || forbidden_token_detected {
        None
    } else {
        Some(format!("{:x}", Sha256::digest(captured.as_slice())))
    };
    Ok(StreamEvidence {
        digest,
        total_bytes,
        truncated,
        forbidden_token_detected,
    })
}

pub(crate) fn fixed_probe_path() -> Result<PathBuf, CredentialError> {
    let exe = std::env::current_exe().map_err(|_| process_error())?;
    Ok(exe
        .parent()
        .ok_or_else(process_error)?
        .join("o020_credential_probe.exe"))
}

/// 对给定 PID 集合做最终存活核验（真实 OS 观察，非内存自证）。
/// 只统计映像名为 o020_credential_probe.exe 的进程，排除 PID 复用导致的误报；
/// 打不开句柄视为已退出；命中 probe 且句柄未 signaled（WAIT_TIMEOUT/WAIT_FAILED）
/// 一律保守计为存活，保证 fail-closed。
pub(crate) fn alive_process_count(pids: &[u32]) -> Result<u32, CredentialError> {
    use windows::core::PWSTR;
    use windows::Win32::Foundation::{CloseHandle, WAIT_OBJECT_0};
    use windows::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, WaitForSingleObject, PROCESS_NAME_WIN32,
        PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SYNCHRONIZE,
    };
    let mut alive = 0_u32;
    for &pid in pids {
        let handle = unsafe {
            OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
                false,
                pid,
            )
        };
        let Ok(handle) = handle else { continue };
        let mut buffer = [0_u16; 512];
        let mut size = buffer.len() as u32;
        let is_probe = unsafe {
            QueryFullProcessImageNameW(
                handle,
                PROCESS_NAME_WIN32,
                PWSTR(buffer.as_mut_ptr()),
                &mut size,
            )
        }
        .is_ok()
            && std::path::Path::new(&String::from_utf16_lossy(&buffer[..size as usize]))
                .file_name()
                .map(|name| name == "o020_credential_probe.exe")
                .unwrap_or(false);
        let state = unsafe { WaitForSingleObject(handle, 0) };
        if is_probe && state != WAIT_OBJECT_0 {
            alive += 1;
        }
        unsafe { CloseHandle(handle) };
    }
    Ok(alive)
}

fn process_error() -> CredentialError {
    CredentialError::new(CredentialErrorCode::CredentialRecoveryFailed)
}
