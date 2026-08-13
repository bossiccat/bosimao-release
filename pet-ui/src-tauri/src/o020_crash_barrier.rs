#![cfg(all(windows, feature = "credential-test-support"))]

use windows::core::PCWSTR;
use windows::Win32::Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0};
use windows::Win32::Security::SECURITY_ATTRIBUTES;
use windows::Win32::System::Threading::{CreateEventW, SetEvent, WaitForSingleObject, INFINITE};

use crate::credential::{CredentialError, CredentialErrorCode};
use crate::credential_windows_lock::SecurityDescriptor;

const BARRIER_PREFIX: &str = "Global\\JaxPet.O020Barrier.v1";

pub(crate) fn event_names(id: &str) -> (String, String) {
    (
        format!("{BARRIER_PREFIX}.{id}.reached"),
        format!("{BARRIER_PREFIX}.{id}.release"),
    )
}

pub(crate) struct CrashBarrier {
    reached: OwnedHandle,
    release: OwnedHandle,
}

pub(crate) struct ControllerBarrier {
    pub(crate) reached: HANDLE,
    _reached_owner: OwnedHandle,
    _release_owner: OwnedHandle,
}

impl ControllerBarrier {
    pub(crate) fn create(id: &str) -> Result<Self, CredentialError> {
        let (reached_name, release_name) = event_names(id);
        let reached_owner = create_event(&reached_name)?;
        let release_owner = create_event(&release_name)?;
        Ok(Self {
            reached: reached_owner.0,
            _reached_owner: reached_owner,
            _release_owner: release_owner,
        })
    }
}

impl CrashBarrier {
    pub(crate) fn create(id: &str) -> Result<Self, CredentialError> {
        let (reached, release) = event_names(id);
        Ok(Self {
            reached: create_event(&reached)?,
            release: create_event(&release)?,
        })
    }

    pub(crate) fn notify_and_wait(&self) -> Result<(), CredentialError> {
        unsafe { SetEvent(self.reached.0) }.map_err(|_| barrier_error())?;
        let disposition = unsafe { WaitForSingleObject(self.release.0, INFINITE) };
        if disposition == WAIT_OBJECT_0 {
            Err(barrier_error())
        } else {
            Err(barrier_error())
        }
    }
}

fn create_event(name: &str) -> Result<OwnedHandle, CredentialError> {
    let mut descriptor = SecurityDescriptor::current_user_and_system()?;
    let mut attributes = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor.as_mut_ptr(),
        bInheritHandle: false.into(),
    };
    let name = wide(name);
    unsafe { CreateEventW(Some(&mut attributes), true, false, PCWSTR(name.as_ptr())) }
        .map(OwnedHandle)
        .map_err(|_| barrier_error())
}

struct OwnedHandle(HANDLE);

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.0);
        }
    }
}

fn barrier_error() -> CredentialError {
    CredentialError::new(CredentialErrorCode::CredentialRecoveryFailed)
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}
