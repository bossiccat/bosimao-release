use crate::owner::{
    classify_owner_bytes, create_owner_file, OwnerState, ProcessIdentity,
};
use crate::windows_mutex::{mutex_name_for_root, NamedMutex, WaitStatus};
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, PartialEq, Eq)]
pub enum ReleaseOutcome {
    Ok,
    OwnerMismatch,
    LeaseLost,
}

#[derive(Debug)]
pub struct Lease {
    pub lease_id: String,
    pub token: String,
    pub runtime_root: PathBuf,
}

pub struct Coordinator {
    runtime_root: PathBuf,
    mutex: NamedMutex,
    identity_resolver: Box<dyn Fn(u32) -> ProcessIdentity + Send>,
    held: Option<HeldLease>,
}

struct HeldLease {
    lease_id: String,
    token: String,
}

#[derive(Debug)]
pub enum AcquireError {
    Busy,
    LockPoisoned,
    OwnerInvalid,
    IdentityUnavailable,
    Io(String),
}

impl std::fmt::Display for AcquireError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AcquireError::Busy => write!(f, "publish coordination mutex is busy"),
            AcquireError::LockPoisoned => write!(f, "mutex wait failed"),
            AcquireError::OwnerInvalid => write!(f, "existing publish lock owner is malformed"),
            AcquireError::IdentityUnavailable => write!(f, "owner process identity is unavailable"),
            AcquireError::Io(message) => write!(f, "io error: {message}"),
        }
    }
}

impl std::error::Error for AcquireError {}

impl Coordinator {
    pub fn new(runtime_root: &Path) -> Result<Self, String> {
        Self::new_with_identity(runtime_root, default_identity_probe)
    }

    pub fn new_with_identity<F>(runtime_root: &Path, resolver: F) -> Result<Self, String>
    where
        F: Fn(u32) -> ProcessIdentity + Send + 'static,
    {
        let root_str = runtime_root
            .to_str()
            .ok_or_else(|| "runtime root is not valid UTF-8".to_owned())?;
        let name = mutex_name_for_root(root_str)?;
        let mutex = NamedMutex::open(&name)?;
        Ok(Self {
            runtime_root: runtime_root.to_path_buf(),
            mutex,
            identity_resolver: Box::new(resolver),
            held: None,
        })
    }

    pub fn acquire(&mut self, owner_json: String) -> Result<Lease, AcquireError> {
        if self.held.is_some() {
            return Err(AcquireError::Busy);
        }
        match self.mutex.wait(5000) {
            WaitStatus::Object | WaitStatus::Abandoned => {}
            WaitStatus::Timeout => return Err(AcquireError::Busy),
            WaitStatus::Failed(_) => return Err(AcquireError::LockPoisoned),
        }

        let lock_path = self.runtime_root.join("publish.lock");
        if lock_path.exists() {
            let bytes = fs::read(&lock_path).map_err(|e| AcquireError::Io(e.to_string()))?;
            let state = classify_owner_bytes(&bytes, &self.identity_resolver);
            match state {
                OwnerState::Live => {
                    return Err(AcquireError::Busy);
                }
                OwnerState::Absent | OwnerState::PidReused => {
                    // Proven-dead owner: safe to reclaim inside the mutex.
                    fs::remove_file(&lock_path)
                        .map_err(|e| AcquireError::Io(e.to_string()))?;
                }
                OwnerState::Invalid => {
                    return Err(AcquireError::OwnerInvalid);
                }
                OwnerState::IdentityUnavailable => {
                    return Err(AcquireError::IdentityUnavailable);
                }
            }
        }

        let bytes = format!("{owner_json}\n");
        create_owner_file(&lock_path, bytes.as_bytes())
            .map_err(|e| AcquireError::Io(e.to_string()))?;

        let token = extract_token(&owner_json).ok_or(AcquireError::OwnerInvalid)?;
        let lease_id = format!("lease-{token}");
        self.held = Some(HeldLease {
            lease_id: lease_id.clone(),
            token: token.clone(),
        });
        Ok(Lease {
            lease_id,
            token,
            runtime_root: self.runtime_root.clone(),
        })
    }

    pub fn publish(
        &mut self,
        lease_id: &str,
        temporary_path: PathBuf,
        current_path: PathBuf,
    ) -> Result<(), String> {
        let held = self
            .held
            .as_ref()
            .ok_or_else(|| "no active lease".to_owned())?;
        if held.lease_id != lease_id {
            return Err("lease id mismatch".to_owned());
        }
        // Still holding the named mutex: the atomic pointer replace is the
        // linearization point of the whole publish protocol (ADR-027).
        crate::pointer_commit::commit_pointer(&temporary_path, &current_path)
            .map_err(|error| error.to_string())
    }

    pub fn release(&mut self, lease: &Lease, expected_token: &str) -> Result<ReleaseOutcome, String> {
        let held = match self.held.as_ref() {
            Some(held) => held,
            None => return Ok(ReleaseOutcome::LeaseLost),
        };
        if held.lease_id != lease.lease_id {
            return Err("lease id mismatch on release".to_owned());
        }
        if held.token != expected_token {
            return Ok(ReleaseOutcome::OwnerMismatch);
        }
        let lock_path = self.runtime_root.join("publish.lock");
        fs::remove_file(&lock_path).map_err(|e| e.to_string())?;
        self.held = None;
        self.mutex.release().map_err(|e| format!("ReleaseMutex failed: {e}"))?;
        Ok(ReleaseOutcome::Ok)
    }
}

impl Drop for Coordinator {
    fn drop(&mut self) {
        if self.held.is_some() {
            let _ = self.mutex.release();
        }
    }
}

fn extract_token(owner_json: &str) -> Option<String> {
    let value: serde_json::Value = serde_json::from_str(owner_json).ok()?;
    value.get("token")?.as_str().map(str::to_owned)
}

fn default_identity_probe(pid: u32) -> ProcessIdentity {
    crate::process_identity::probe(pid)
}
