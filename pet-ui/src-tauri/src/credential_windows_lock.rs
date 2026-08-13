#![cfg(windows)]

use std::collections::HashMap;
use std::marker::PhantomData;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock, Weak};
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};
use windows::core::{PCWSTR, PWSTR};
use windows::Win32::Foundation::{
    CloseHandle, GetLastError, LocalFree, HANDLE, HLOCAL, WAIT_ABANDONED, WAIT_OBJECT_0,
    WAIT_TIMEOUT,
};
use windows::Win32::Security::Authorization::{
    ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
};
use windows::Win32::Security::{
    GetTokenInformation, TokenUser, PSECURITY_DESCRIPTOR, SECURITY_ATTRIBUTES, TOKEN_QUERY,
    TOKEN_USER,
};
use windows::Win32::System::Threading::{
    CreateMutexW, GetCurrentProcess, OpenProcessToken, ReleaseMutex, WaitForSingleObject,
};

use crate::credential::{CredentialError, CredentialErrorCode};
use crate::credential_transaction::{CredentialTransactionLock, LockState};

const LOCK_WAIT_MS: u32 = 5_000;

type ProcessLockRegistry = Mutex<HashMap<String, Weak<AtomicBool>>>;
static PROCESS_LOCKS: OnceLock<ProcessLockRegistry> = OnceLock::new();

fn shared_process_lock(name: &str) -> Arc<AtomicBool> {
    let registry = PROCESS_LOCKS.get_or_init(|| Mutex::new(HashMap::new()));
    let mut locks = registry
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if let Some(lock) = locks.get(name).and_then(Weak::upgrade) {
        return lock;
    }
    let lock = Arc::new(AtomicBool::new(false));
    locks.insert(name.to_owned(), Arc::downgrade(&lock));
    lock
}

pub(crate) struct Win32TransactionLock {
    process_lock: Arc<AtomicBool>,
    mutex_name: Vec<u16>,
    security_descriptor_sddl: String,
    wait_ms: u32,
}

impl Win32TransactionLock {
    pub(crate) fn current_user(
        prefix: &str,
        suffix: Option<&str>,
    ) -> Result<Self, CredentialError> {
        Self::current_user_with_wait(prefix, suffix, LOCK_WAIT_MS)
    }

    fn current_user_with_wait(
        prefix: &str,
        suffix: Option<&str>,
        wait_ms: u32,
    ) -> Result<Self, CredentialError> {
        let sid = current_user_sid()?;
        let sid_hash = format!("{:x}", Sha256::digest(sid.as_bytes()));
        let name = match suffix {
            Some(suffix) => format!("{prefix}.{sid_hash}.{suffix}"),
            None => format!("{prefix}.{sid_hash}"),
        };
        let sddl = format!("D:P(A;;0x1F0001;;;SY)(A;;0x1F0001;;;{sid})");
        Ok(Self {
            process_lock: shared_process_lock(&name),
            mutex_name: wide(&name),
            security_descriptor_sddl: sddl,
            wait_ms,
        })
    }
}

pub(crate) struct Win32TransactionGuard {
    process_lock: Arc<AtomicBool>,
    handle: HANDLE,
    _thread_affine: PhantomData<Rc<()>>,
}

impl Drop for Win32TransactionGuard {
    fn drop(&mut self) {
        unsafe {
            let _ = ReleaseMutex(self.handle);
            let _ = CloseHandle(self.handle);
        }
        self.process_lock.store(false, Ordering::Release);
    }
}

impl CredentialTransactionLock for Win32TransactionLock {
    type Guard = Win32TransactionGuard;

    fn acquire(&self) -> Result<(Self::Guard, LockState), CredentialError> {
        let deadline = Instant::now() + Duration::from_millis(self.wait_ms.into());
        while self
            .process_lock
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            if Instant::now() >= deadline {
                return Err(busy());
            }
            std::thread::yield_now();
        }
        let process_lock = Arc::clone(&self.process_lock);
        let mut security_descriptor =
            SecurityDescriptor::from_sddl(&self.security_descriptor_sddl)?;
        let mut attributes = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: security_descriptor.as_mut_ptr(),
            bInheritHandle: false.into(),
        };
        let handle = match unsafe {
            CreateMutexW(
                Some(&mut attributes),
                false,
                PCWSTR(self.mutex_name.as_ptr()),
            )
        } {
            Ok(handle) => handle,
            Err(_) => {
                self.process_lock.store(false, Ordering::Release);
                return Err(os_error(CredentialErrorCode::CredentialRecoveryFailed));
            }
        };
        let disposition = unsafe { WaitForSingleObject(handle, self.wait_ms) };
        let state = if disposition == WAIT_OBJECT_0 {
            LockState::Acquired
        } else if disposition == WAIT_ABANDONED {
            LockState::Abandoned
        } else if disposition == WAIT_TIMEOUT {
            unsafe {
                let _ = CloseHandle(handle);
            }
            self.process_lock.store(false, Ordering::Release);
            return Err(busy());
        } else {
            unsafe {
                let _ = CloseHandle(handle);
            }
            self.process_lock.store(false, Ordering::Release);
            return Err(os_error(CredentialErrorCode::CredentialRecoveryFailed));
        };
        Ok((
            Win32TransactionGuard {
                process_lock,
                handle,
                _thread_affine: PhantomData,
            },
            state,
        ))
    }
}

pub(crate) struct SecurityDescriptor {
    pointer: PSECURITY_DESCRIPTOR,
}

impl SecurityDescriptor {
    pub(crate) fn current_user_and_system() -> Result<Self, CredentialError> {
        let sid = current_user_sid()?;
        Self::from_sddl(&format!("D:P(A;;GA;;;SY)(A;;GA;;;{sid})"))
    }

    pub(crate) fn as_mut_ptr(&mut self) -> *mut core::ffi::c_void {
        self.pointer.0
    }

    #[cfg(feature = "credential-test-support")]
    pub(crate) fn as_psd(&mut self) -> PSECURITY_DESCRIPTOR {
        self.pointer
    }

    pub(crate) fn from_sddl(value: &str) -> Result<Self, CredentialError> {
        let value = wide(value);
        let mut descriptor = PSECURITY_DESCRIPTOR::default();
        unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                PCWSTR(value.as_ptr()),
                SDDL_REVISION_1,
                &mut descriptor,
                None,
            )
            .map_err(|_| os_error(CredentialErrorCode::CredentialRecoveryFailed))?;
        }
        Ok(Self {
            pointer: descriptor,
        })
    }
}

impl Drop for SecurityDescriptor {
    fn drop(&mut self) {
        unsafe {
            LocalFree(Some(HLOCAL(self.pointer.0)));
        }
    }
}

fn current_user_sid() -> Result<String, CredentialError> {
    let mut token = HANDLE::default();
    unsafe {
        OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token)
            .map_err(|_| os_error(CredentialErrorCode::CredentialReadDenied))?;
    }
    let token = OwnedHandle(token);
    let mut required = 0;
    unsafe {
        let _ = GetTokenInformation(token.0, TokenUser, None, 0, &mut required);
    }
    if required == 0 {
        return Err(os_error(CredentialErrorCode::CredentialReadDenied));
    }
    let mut buffer = vec![0_u8; required as usize];
    unsafe {
        GetTokenInformation(
            token.0,
            TokenUser,
            Some(buffer.as_mut_ptr().cast()),
            required,
            &mut required,
        )
        .map_err(|_| os_error(CredentialErrorCode::CredentialReadDenied))?;
        let user = &*(buffer.as_ptr().cast::<TOKEN_USER>());
        let mut sid = PWSTR::null();
        ConvertSidToStringSidW(user.User.Sid, &mut sid)
            .map_err(|_| os_error(CredentialErrorCode::CredentialReadDenied))?;
        let sid = LocalWideString(sid);
        sid.0
            .to_string()
            .map_err(|_| CredentialError::new(CredentialErrorCode::CredentialReadDenied))
    }
}

struct LocalWideString(PWSTR);

impl Drop for LocalWideString {
    fn drop(&mut self) {
        unsafe {
            LocalFree(Some(HLOCAL(self.0.as_ptr().cast())));
        }
    }
}

struct OwnedHandle(HANDLE);

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.0);
        }
    }
}

fn os_error(code: CredentialErrorCode) -> CredentialError {
    CredentialError::with_os_code(code, unsafe { GetLastError().0 })
}

fn busy() -> CredentialError {
    CredentialError::new(CredentialErrorCode::CredentialBusy)
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}
