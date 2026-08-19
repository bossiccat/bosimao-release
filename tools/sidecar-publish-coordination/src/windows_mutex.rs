use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

#[derive(Debug, PartialEq, Eq)]
pub enum WaitStatus {
    Object,
    Timeout,
    Abandoned,
    Failed(u32),
}

pub fn mutex_name_for_root(runtime_root: &str) -> Result<String, String> {
    let path = PathBuf::from(runtime_root);
    if !path.is_absolute() {
        return Err("runtime root must be absolute".to_owned());
    }
    // Fail-closed identity: prefer the kernel-final DOS path (resolves
    // junctions/reparse/8.3 aliases); lexical canonical form is only used
    // when the directory does not exist yet (first acquire creates it).
    let identity_path = match final_path_for_root(&path) {
        Ok(Some(final_path)) => final_path,
        Ok(None) => canonical_root(&path)?,
        Err(reason) => return Err(reason),
    };
    let digest = sha256_hex(identity_path.as_bytes());
    Ok(format!(r"Local\jax-sidecar-publish-v1-{digest}"))
}

/// Returns the kernel-final DOS path for an existing directory, or None when
/// the directory does not exist. Any other failure (access denied, handle
/// errors) is propagated as an error: callers must fail closed instead of
/// silently falling back to a spoofable lexical spelling.
#[cfg(windows)]
fn final_path_for_root(path: &Path) -> Result<Option<String>, String> {
    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, GetFinalPathNameByHandleW, FILE_FLAG_BACKUP_SEMANTICS,
        FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
    };

    let wide: Vec<u16> = std::ffi::OsStr::new(path)
        .to_str()
        .ok_or_else(|| "runtime root is not valid UTF-8".to_owned())?
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect();

    let handle = unsafe {
        CreateFileW(
            wide.as_ptr(),
            0, // FILE_QUERY_ATTRIBUTES-level access; we only query the name
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            std::ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        let code = unsafe { GetLastError() };
        // 3 = ERROR_PATH_NOT_FOUND, 2 = ERROR_FILE_NOT_FOUND: the runtime root
        // has not been created yet. This is the only sanctioned fallback.
        if code == 2 || code == 3 {
            return Ok(None);
        }
        return Err(format!("CreateFileW on runtime root failed: {code}"));
    }

    let mut buffer = vec![0u16; 1024];
    let len = unsafe {
        GetFinalPathNameByHandleW(
            handle,
            buffer.as_mut_ptr(),
            buffer.len() as u32,
            windows_sys::Win32::Storage::FileSystem::FILE_NAME_NORMALIZED
                | windows_sys::Win32::Storage::FileSystem::VOLUME_NAME_DOS,
        )
    };
    if len == 0 {
        let code = unsafe { GetLastError() };
        unsafe { CloseHandle(handle) };
        return Err(format!("GetFinalPathNameByHandleW failed: {code}"));
    }
    unsafe { CloseHandle(handle) };

    if len as usize >= buffer.len() {
        buffer.resize(len as usize + 1, 0);
        // Second pass with the reported required length.
        let handle_again = unsafe {
            CreateFileW(
                wide.as_ptr(),
                0,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                std::ptr::null(),
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS,
                std::ptr::null_mut(),
            )
        };
        if handle_again == INVALID_HANDLE_VALUE {
            let code = unsafe { GetLastError() };
            return Err(format!("CreateFileW retry on runtime root failed: {code}"));
        }
        let len2 = unsafe {
            GetFinalPathNameByHandleW(
                handle_again,
                buffer.as_mut_ptr(),
                buffer.len() as u32,
                windows_sys::Win32::Storage::FileSystem::FILE_NAME_NORMALIZED
                    | windows_sys::Win32::Storage::FileSystem::VOLUME_NAME_DOS,
            )
        };
        unsafe { CloseHandle(handle_again) };
        if len2 == 0 || len2 as usize >= buffer.len() {
            return Err("GetFinalPathNameByHandleW retry failed".to_owned());
        }
        buffer.truncate(len2 as usize);
    } else {
        buffer.truncate(len as usize);
    }

    let mut final_path = String::from_utf16_lossy(&buffer)
        .trim_start_matches(r"\\?\")
        .trim_start_matches(r"\\.\")
        .to_ascii_lowercase();
    // A drive-relative result would keep the name ambiguous; require a fully
    // qualified final path before accepting it as mutex identity.
    if final_path.len() < 3
        || final_path.as_bytes().get(1) != Some(&b':')
        || final_path.as_bytes().get(2) != Some(&b'\\')
    {
        return Err(format!("final path is not a drive-qualified path: {final_path}"));
    }
    // Normalize a trailing separator (rare, e.g. drive roots).
    while final_path.ends_with('\\') && final_path.len() > 3 {
        final_path.pop();
    }
    Ok(Some(final_path))
}

#[cfg(not(windows))]
fn final_path_for_root(_path: &Path) -> Result<Option<String>, String> {
    // Non-Windows builds have no reparse-point aliasing surface to defend
    // against; lexical canonicalization remains the identity.
    Ok(None)
}

fn canonical_root(path: &Path) -> Result<String, String> {
    let normalized = path
        .to_str()
        .ok_or_else(|| "runtime root is not valid UTF-8".to_owned())?
        .replace('/', "\\");
    if normalized.len() < 3
        || normalized.as_bytes().get(1) != Some(&b':')
        || normalized.as_bytes().get(2) != Some(&b'\\')
    {
        return Err("runtime root must use an absolute drive path".to_owned());
    }

    let mut components = Vec::new();
    for component in normalized[3..].split('\\') {
        match component {
            "" | "." => {}
            ".." => {
                if components.pop().is_none() {
                    return Err("runtime root escapes its drive root".to_owned());
                }
            }
            value => components.push(value),
        }
    }
    let suffix = components.join("\\");
    let value = if suffix.is_empty() {
        format!("{}:\\", &normalized[..1])
    } else {
        format!("{}:\\{}", &normalized[..1], suffix)
    };
    Ok(value.to_ascii_lowercase())
}

fn sha256_hex(value: &[u8]) -> String {
    let digest = Sha256::digest(value);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(windows)]
pub struct NamedMutex {
    handle: windows_sys::Win32::Foundation::HANDLE,
}

#[cfg(windows)]
impl NamedMutex {
    pub fn open(name: &str) -> Result<Self, String> {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Foundation::GetLastError;
        use windows_sys::Win32::System::Threading::CreateMutexW;

        let wide: Vec<u16> = std::ffi::OsStr::new(name)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        let handle = unsafe { CreateMutexW(std::ptr::null(), 0, wide.as_ptr()) };
        if handle.is_null() {
            return Err(format!("CreateMutexW failed: {}", unsafe { GetLastError() }));
        }
        Ok(Self { handle })
    }

    pub fn wait(&self, timeout_ms: u32) -> WaitStatus {
        use windows_sys::Win32::Foundation::{GetLastError, WAIT_ABANDONED, WAIT_FAILED, WAIT_OBJECT_0, WAIT_TIMEOUT};
        use windows_sys::Win32::System::Threading::WaitForSingleObject;

        match unsafe { WaitForSingleObject(self.handle, timeout_ms) } {
            WAIT_OBJECT_0 => WaitStatus::Object,
            WAIT_TIMEOUT => WaitStatus::Timeout,
            WAIT_ABANDONED => WaitStatus::Abandoned,
            WAIT_FAILED => WaitStatus::Failed(unsafe { GetLastError() }),
            _ => WaitStatus::Failed(unsafe { GetLastError() }),
        }
    }

    pub fn release(&self) -> Result<(), u32> {
        use windows_sys::Win32::Foundation::GetLastError;
        use windows_sys::Win32::System::Threading::ReleaseMutex;

        if unsafe { ReleaseMutex(self.handle) } == 0 {
            return Err(unsafe { GetLastError() });
        }
        Ok(())
    }
}

#[cfg(windows)]
impl Drop for NamedMutex {
    fn drop(&mut self) {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.handle) };
    }
}

#[cfg(not(windows))]
pub struct NamedMutex;

#[cfg(not(windows))]
impl NamedMutex {
    pub fn open(_name: &str) -> Result<Self, String> {
        Err("Windows named mutex is unsupported on this platform".to_owned())
    }

    pub fn wait(&self, _timeout_ms: u32) -> WaitStatus {
        WaitStatus::Failed(0)
    }

    pub fn release(&self) -> Result<(), u32> {
        Err(0)
    }
}
