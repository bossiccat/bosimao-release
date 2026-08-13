#![cfg(all(windows, feature = "credential-test-support"))]

use std::io::Read;
use std::process::ExitCode;

use jax_pet::credential::{CredentialError, CredentialErrorCode, SecretString};
use jax_pet::o020_probe::{self, ProbeCheckpoint};
use serde_json::json;
use zeroize::Zeroizing;

const SUCCESS: u8 = 0;
const INVALID_ARGS: u8 = 10;
const UNSUPPORTED_PLATFORM: u8 = 11;
const SECRET_INPUT_INVALID: u8 = 12;
const CREDENTIAL_MISSING: u8 = 20;
const CREDENTIAL_READ_FAILED: u8 = 21;
const CREDENTIAL_WRITE_FAILED: u8 = 22;
const CREDENTIAL_DELETE_FAILED: u8 = 23;
const CREDENTIAL_RECOVERY_FAILED: u8 = 24;
const CREDENTIAL_BUSY_OR_TIMEOUT: u8 = 25;
const CREDENTIAL_ROTATION_FAILED: u8 = 26;
const CHECKPOINT_REACHED: u8 = 30;
const INTERNAL_FAIL: u8 = 40;
const MAX_STDIN: usize = 512;

struct Args {
    operation: String,
    suffix: String,
    checkpoint: Option<ProbeCheckpoint>,
    barrier: String,
    hold_ms: u64,
}

fn parse_args() -> Result<Args, ()> {
    let mut args = std::env::args().skip(1);
    let mut operation = None;
    let mut suffix = None;
    let mut checkpoint = None;
    let mut barrier = None;
    let mut hold_ms = 0;
    while let Some(flag) = args.next() {
        let value = args.next().ok_or(())?;
        match flag.as_str() {
            "--op" if operation.is_none() => operation = Some(value),
            "--suffix" if suffix.is_none() => suffix = Some(value),
            "--checkpoint" if checkpoint.is_none() => {
                checkpoint = Some(ProbeCheckpoint::parse(&value).ok_or(())?)
            }
            "--barrier" if barrier.is_none() => {
                validate_opaque(&value)?;
                barrier = Some(value);
            }
            "--hold-ms" if hold_ms == 0 => hold_ms = value.parse().map_err(|_| ())?,
            _ => return Err(()),
        }
    }
    let operation = operation.ok_or(())?;
    if !matches!(
        operation.as_str(),
        "provision" | "load" | "rotate" | "revoke" | "hold-lock" | "crash"
    ) {
        return Err(());
    }
    let suffix = suffix.ok_or(())?;
    if validate_opaque(&suffix).is_err() || suffix.len() > 96 {
        return Err(());
    }
    let crash = operation == "crash";
    if hold_ms > 60_000 || crash != checkpoint.is_some() || crash != barrier.is_some() {
        return Err(());
    }
    if operation != "hold-lock" && hold_ms != 0 {
        return Err(());
    }
    Ok(Args {
        operation,
        suffix,
        checkpoint,
        barrier: barrier.unwrap_or_default(),
        hold_ms,
    })
}

fn validate_opaque(value: &str) -> Result<(), ()> {
    if value.is_empty()
        || value.len() > 128
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    {
        return Err(());
    }
    Ok(())
}

fn read_secret() -> Result<SecretString, u8> {
    let mut bytes = Zeroizing::new(Vec::new());
    std::io::stdin()
        .take((MAX_STDIN + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| SECRET_INPUT_INVALID)?;
    if bytes.len() > MAX_STDIN {
        return Err(SECRET_INPUT_INVALID);
    }
    let owned = std::mem::take(&mut *bytes);
    SecretString::parse_utf8(owned).map_err(|_| SECRET_INPUT_INVALID)
}

fn status_for(code: u8) -> &'static str {
    match code {
        SUCCESS => "SUCCESS",
        INVALID_ARGS => "INVALID_ARGS",
        UNSUPPORTED_PLATFORM => "UNSUPPORTED_PLATFORM",
        SECRET_INPUT_INVALID => "SECRET_INPUT_INVALID",
        CREDENTIAL_MISSING => "CREDENTIAL_MISSING",
        CREDENTIAL_READ_FAILED => "CREDENTIAL_READ_FAILED",
        CREDENTIAL_WRITE_FAILED => "CREDENTIAL_WRITE_FAILED",
        CREDENTIAL_DELETE_FAILED => "CREDENTIAL_DELETE_FAILED",
        CREDENTIAL_RECOVERY_FAILED => "CREDENTIAL_RECOVERY_FAILED",
        CREDENTIAL_BUSY_OR_TIMEOUT => "CREDENTIAL_BUSY_OR_TIMEOUT",
        CREDENTIAL_ROTATION_FAILED => "CREDENTIAL_ROTATION_FAILED",
        CHECKPOINT_REACHED => "CHECKPOINT_REACHED",
        _ => "INTERNAL_FAIL",
    }
}

fn emit(status: &str, operation: &str, exit: u8) -> ExitCode {
    println!(
        "{}",
        json!({"status": status, "op": operation, "exit": exit})
    );
    ExitCode::from(exit)
}

fn map_error(error: CredentialError) -> u8 {
    match error.code {
        CredentialErrorCode::CredentialMissing => CREDENTIAL_MISSING,
        CredentialErrorCode::CredentialReadDenied => CREDENTIAL_READ_FAILED,
        CredentialErrorCode::CredentialWriteFailed => CREDENTIAL_WRITE_FAILED,
        CredentialErrorCode::CredentialDeleteFailed => CREDENTIAL_DELETE_FAILED,
        CredentialErrorCode::CredentialRecoveryFailed => CREDENTIAL_RECOVERY_FAILED,
        CredentialErrorCode::CredentialBusy => CREDENTIAL_BUSY_OR_TIMEOUT,
        CredentialErrorCode::CredentialRotationFailed => CREDENTIAL_ROTATION_FAILED,
        CredentialErrorCode::UnsupportedPlatform => UNSUPPORTED_PLATFORM,
        CredentialErrorCode::CredentialCorrupt | CredentialErrorCode::CredentialRevoked => {
            INTERNAL_FAIL
        }
    }
}

fn run(args: &Args) -> Result<(), u8> {
    match args.operation.as_str() {
        "provision" => o020_probe::provision(&args.suffix, read_secret()?).map_err(map_error),
        "load" => o020_probe::read(&args.suffix)
            .map(|_| ())
            .map_err(map_error),
        "rotate" => o020_probe::rotate(&args.suffix, read_secret()?).map_err(map_error),
        "revoke" => o020_probe::revoke(&args.suffix).map_err(map_error),
        "hold-lock" => o020_probe::hold(&args.suffix, args.hold_ms)
            .map(|_| ())
            .map_err(map_error),
        "crash" => {
            o020_probe::inject_checkpoint(
                &args.suffix,
                &read_secret()?,
                args.checkpoint.unwrap(),
                &args.barrier,
            )
            .map_err(map_error)?;
            Err(CHECKPOINT_REACHED)
        }
        _ => Err(INVALID_ARGS),
    }
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(args) => args,
        Err(()) => return emit("INVALID_ARGS", "invalid", INVALID_ARGS),
    };
    let operation = args.operation.as_str();
    match run(&args) {
        Ok(()) => emit("SUCCESS", operation, SUCCESS),
        Err(code) => emit(status_for(code), operation, code),
    }
}
