#![cfg(windows)]
use crate::credential::SecretString;
use crate::credential_windows_lock::SecurityDescriptor;
use crate::provision_orchestrator::{ProvisionError, ProvisionTransport, HELPER_NAME};
use std::path::Path;
use std::time::Duration;
use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::{
    CloseHandle, SetHandleInformation, HANDLE, HANDLE_FLAGS, HANDLE_FLAG_INHERIT, WAIT_OBJECT_0,
    WAIT_TIMEOUT,
};
use windows::Win32::Security::SECURITY_ATTRIBUTES;
use windows::Win32::Storage::FileSystem::{
    CreateFileW, WriteFile, FILE_ATTRIBUTE_NORMAL, FILE_GENERIC_WRITE, FILE_SHARE_MODE,
    FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
};
use windows::Win32::System::Pipes::CreatePipe;
use windows::Win32::System::Threading::{
    CreateProcessW, DeleteProcThreadAttributeList, GetExitCodeProcess,
    InitializeProcThreadAttributeList, TerminateProcess, UpdateProcThreadAttribute,
    WaitForSingleObject, EXTENDED_STARTUPINFO_PRESENT, LPPROC_THREAD_ATTRIBUTE_LIST,
    PROCESS_INFORMATION, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, STARTF_USESTDHANDLES, STARTUPINFOEXW,
};
pub struct WindowsProvisionTransport;

impl ProvisionTransport for WindowsProvisionTransport {
    fn run(&self, secret: &SecretString, timeout: Duration) -> Result<u32, ProvisionError> {
        let helper = fixed_helper_path()?;
        let mut pipe = InheritedStdinPipe::new()?;
        let mut process = ChildProcess::spawn(&helper, pipe.read.raw())?;
        pipe.close_child_end();
        let write_result = pipe.write_all(secret.expose().as_bytes());
        pipe.close_parent_end();
        if let Err(error) = write_result {
            process.terminate_and_reap()?;
            return Err(error);
        }
        process.wait(timeout)
    }
}

struct OwnedHandle(Option<HANDLE>);

impl OwnedHandle {
    fn new(handle: HANDLE) -> Self {
        Self(Some(handle))
    }

    fn raw(&self) -> HANDLE {
        self.0.unwrap_or_default()
    }

    fn close(&mut self) {
        if let Some(handle) = self.0.take() {
            if !handle.is_invalid() {
                unsafe {
                    let _ = CloseHandle(handle);
                }
            }
        }
    }
}

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        self.close();
    }
}

struct InheritedStdinPipe {
    read: OwnedHandle,
    write: OwnedHandle,
    _security_descriptor: SecurityDescriptor,
}

impl InheritedStdinPipe {
    fn new() -> Result<Self, ProvisionError> {
        let mut read = HANDLE::default();
        let mut write = HANDLE::default();
        let mut security_descriptor = SecurityDescriptor::current_user_and_system()
            .map_err(|_| ProvisionError::SpawnFailed)?;
        let attributes = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: security_descriptor.as_mut_ptr(),
            bInheritHandle: true.into(),
        };
        unsafe {
            CreatePipe(&mut read, &mut write, Some(&attributes), 0)
                .map_err(|_| ProvisionError::SpawnFailed)?;
        }
        let pipe = Self {
            read: OwnedHandle::new(read),
            write: OwnedHandle::new(write),
            _security_descriptor: security_descriptor,
        };
        unsafe {
            SetHandleInformation(pipe.write.raw(), HANDLE_FLAG_INHERIT.0, HANDLE_FLAGS(0))
                .map_err(|_| ProvisionError::SpawnFailed)?;
        }
        Ok(pipe)
    }

    fn write_all(&self, bytes: &[u8]) -> Result<(), ProvisionError> {
        let mut offset = 0;
        while offset < bytes.len() {
            let mut written = 0;
            unsafe {
                WriteFile(
                    self.write.raw(),
                    Some(&bytes[offset..]),
                    Some(&mut written),
                    None,
                )
                .map_err(|_| ProvisionError::PipeWriteFailed)?;
            }
            if written == 0 {
                return Err(ProvisionError::PipeWriteFailed);
            }
            offset += written as usize;
        }
        Ok(())
    }

    fn close_child_end(&mut self) {
        self.read.close();
    }

    fn close_parent_end(&mut self) {
        self.write.close();
    }
}

struct ChildProcess {
    process: OwnedHandle,
    _thread: OwnedHandle,
}

impl ChildProcess {
    fn spawn(helper: &Path, stdin: HANDLE) -> Result<Self, ProvisionError> {
        let helper = helper_wide(helper)?;
        let mut output_security_descriptor = SecurityDescriptor::current_user_and_system()
            .map_err(|_| ProvisionError::SpawnFailed)?;
        let mut output_attributes = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: output_security_descriptor.as_mut_ptr(),
            bInheritHandle: true.into(),
        };
        let null_handle = unsafe {
            CreateFileW(
                w!("NUL"),
                FILE_GENERIC_WRITE.0,
                FILE_SHARE_MODE(FILE_SHARE_READ.0 | FILE_SHARE_WRITE.0),
                Some(&mut output_attributes),
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                None,
            )
            .map_err(|_| ProvisionError::SpawnFailed)?
        };
        let output = OwnedHandle::new(null_handle);
        let handles = [stdin, output.raw()];
        let attributes = AttributeList::new(&handles)?;
        let mut startup = STARTUPINFOEXW::default();
        startup.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = stdin;
        startup.StartupInfo.hStdOutput = output.raw();
        startup.StartupInfo.hStdError = output.raw();
        startup.lpAttributeList = attributes.pointer;
        let mut info = PROCESS_INFORMATION::default();
        let inherit_handles = true;
        unsafe {
            CreateProcessW(
                PCWSTR(helper.as_ptr()),
                None,
                None,
                None,
                inherit_handles,
                EXTENDED_STARTUPINFO_PRESENT,
                None,
                PCWSTR::null(),
                &startup.StartupInfo,
                &mut info,
            )
            .map_err(|_| ProvisionError::SpawnFailed)?;
        }
        drop(attributes);
        drop(output);
        Ok(Self {
            process: OwnedHandle::new(info.hProcess),
            _thread: OwnedHandle::new(info.hThread),
        })
    }

    fn terminate_and_reap(&mut self) -> Result<(), ProvisionError> {
        let _termination = unsafe { TerminateProcess(self.process.raw(), 3) };
        let state = unsafe { WaitForSingleObject(self.process.raw(), 5_000) };
        if state == WAIT_OBJECT_0 {
            Ok(())
        } else {
            Err(ProvisionError::WaitFailed)
        }
    }

    fn wait(&mut self, timeout: Duration) -> Result<u32, ProvisionError> {
        let millis = timeout.as_millis().min(u32::MAX as u128) as u32;
        let state = unsafe { WaitForSingleObject(self.process.raw(), millis) };
        if state == WAIT_TIMEOUT {
            if self.terminate_and_reap().is_err() {
                return Err(ProvisionError::WaitFailed);
            }
            return Err(ProvisionError::WaitTimedOut);
        }
        if state != WAIT_OBJECT_0 {
            self.terminate_and_reap()?;
            return Err(ProvisionError::WaitFailed);
        }
        let mut code = 0;
        unsafe {
            GetExitCodeProcess(self.process.raw(), &mut code)
                .map_err(|_| ProvisionError::WaitFailed)?;
        }
        Ok(code)
    }
}

struct AttributeList {
    storage: Vec<usize>,
    handles: Box<[HANDLE; 2]>,
    pointer: LPPROC_THREAD_ATTRIBUTE_LIST,
    initialized: bool,
}

impl AttributeList {
    fn new(handles: &[HANDLE; 2]) -> Result<Self, ProvisionError> {
        let mut size = 0;
        unsafe {
            let _ = InitializeProcThreadAttributeList(None, 1, None, &mut size);
        }
        if size == 0 {
            return Err(ProvisionError::SpawnFailed);
        }
        let units = size.div_ceil(std::mem::size_of::<usize>());
        let mut storage = vec![0usize; units];
        let pointer = LPPROC_THREAD_ATTRIBUTE_LIST(storage.as_mut_ptr().cast());
        let mut attributes = Self {
            storage,
            handles: Box::new(*handles),
            pointer,
            initialized: false,
        };
        unsafe {
            InitializeProcThreadAttributeList(Some(attributes.pointer), 1, None, &mut size)
                .map_err(|_| ProvisionError::SpawnFailed)?;
        }
        attributes.initialized = true;
        unsafe {
            UpdateProcThreadAttribute(
                attributes.pointer,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                Some(attributes.handles.as_ptr().cast()),
                std::mem::size_of_val(attributes.handles.as_ref()),
                None,
                None,
            )
            .map_err(|_| ProvisionError::SpawnFailed)?;
        }
        Ok(attributes)
    }
}

impl Drop for AttributeList {
    fn drop(&mut self) {
        if self.initialized {
            unsafe { DeleteProcThreadAttributeList(self.pointer) }
        }
        self.storage.fill(0);
    }
}

fn fixed_helper_path() -> Result<std::path::PathBuf, ProvisionError> {
    let launcher = std::env::current_exe().map_err(|_| ProvisionError::SpawnFailed)?;
    let directory = launcher.parent().ok_or(ProvisionError::SpawnFailed)?;
    Ok(directory.join(HELPER_NAME))
}

fn helper_wide(path: &Path) -> Result<Vec<u16>, ProvisionError> {
    let text = path.to_str().ok_or(ProvisionError::SpawnFailed)?;
    if !path.is_absolute() || text.is_empty() {
        return Err(ProvisionError::SpawnFailed);
    }
    Ok(text.encode_utf16().chain(std::iter::once(0)).collect())
}
