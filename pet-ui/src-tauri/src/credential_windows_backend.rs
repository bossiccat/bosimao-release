#![cfg(windows)]

use std::ffi::c_void;
use std::sync::Arc;

use windows::core::{PCWSTR, PWSTR};
use windows::Win32::Foundation::{GetLastError, ERROR_NOT_FOUND};
use windows::Win32::Security::Credentials::{
    CredDeleteW, CredFree, CredReadW, CredWriteW, CREDENTIALW, CRED_FLAGS,
    CRED_PERSIST_LOCAL_MACHINE, CRED_TYPE_GENERIC,
};
use zeroize::Zeroizing;

use crate::credential::{CredentialError, CredentialErrorCode, SecretString};
use crate::credential_transaction::{CredentialBackend, CredentialSlot};

#[derive(Clone)]
pub(crate) struct CredentialTargets {
    active: Arc<str>,
    staging: Arc<str>,
    backup: Arc<str>,
}

impl CredentialTargets {
    pub(crate) fn new(active: String, staging: String, backup: String) -> Self {
        Self {
            active: active.into(),
            staging: staging.into(),
            backup: backup.into(),
        }
    }

    fn get(&self, slot: CredentialSlot) -> &str {
        match slot {
            CredentialSlot::Active => &self.active,
            CredentialSlot::Staging => &self.staging,
            CredentialSlot::Backup => &self.backup,
        }
    }
}

#[derive(Clone)]
pub(crate) struct Win32CredentialBackend {
    targets: CredentialTargets,
}

impl Win32CredentialBackend {
    pub(crate) fn new(targets: CredentialTargets) -> Self {
        Self { targets }
    }
}

struct CredBuffer {
    pointer: *mut CREDENTIALW,
}

impl Drop for CredBuffer {
    fn drop(&mut self) {
        if !self.pointer.is_null() {
            unsafe { CredFree(self.pointer.cast::<c_void>()) };
        }
    }
}

impl CredentialBackend for Win32CredentialBackend {
    fn read(&self, slot: CredentialSlot) -> Result<Option<SecretString>, CredentialError> {
        let target = wide(self.targets.get(slot));
        let mut pointer = std::ptr::null_mut();
        unsafe {
            if CredReadW(
                PCWSTR(target.as_ptr()),
                CRED_TYPE_GENERIC,
                None,
                &mut pointer,
            )
            .is_err()
            {
                let code = GetLastError().0;
                if code == ERROR_NOT_FOUND.0 {
                    return Ok(None);
                }
                return Err(read_error(code));
            }
        }
        let credential = CredBuffer { pointer };
        let value = unsafe { &*credential.pointer };
        let blob = unsafe {
            std::slice::from_raw_parts(value.CredentialBlob, value.CredentialBlobSize as usize)
        };
        let copied = Zeroizing::new(blob.to_vec());
        SecretString::parse_zeroizing(copied).map(Some)
    }

    fn write(&self, slot: CredentialSlot, value: &SecretString) -> Result<(), CredentialError> {
        let mut target = wide(self.targets.get(slot));
        let blob = value.expose().as_bytes();
        let credential = CREDENTIALW {
            Flags: CRED_FLAGS(0),
            Type: CRED_TYPE_GENERIC,
            TargetName: PWSTR(target.as_mut_ptr()),
            CredentialBlobSize: blob.len() as u32,
            CredentialBlob: blob.as_ptr() as *mut u8,
            Persist: CRED_PERSIST_LOCAL_MACHINE,
            ..Default::default()
        };
        unsafe {
            CredWriteW(&credential, 0).map_err(|_| {
                CredentialError::with_os_code(
                    CredentialErrorCode::CredentialWriteFailed,
                    GetLastError().0,
                )
            })
        }
    }

    fn delete(&self, slot: CredentialSlot) -> Result<(), CredentialError> {
        let target = wide(self.targets.get(slot));
        unsafe {
            match CredDeleteW(PCWSTR(target.as_ptr()), CRED_TYPE_GENERIC, None) {
                Ok(()) => Ok(()),
                Err(_) if GetLastError() == ERROR_NOT_FOUND => Ok(()),
                Err(_) => Err(CredentialError::with_os_code(
                    CredentialErrorCode::CredentialDeleteFailed,
                    GetLastError().0,
                )),
            }
        }
    }
}

fn read_error(os_code: u32) -> CredentialError {
    CredentialError::with_os_code(CredentialErrorCode::CredentialReadDenied, os_code)
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}
