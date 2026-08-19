//! Atomic pointer commit via the Windows replace API (audit P0-2).
//!
//! Invariants:
//! 1. `current.json` is replaced atomically: readers observe either the old
//!    pointer or the new one, never a partially written file.
//! 2. The replace preserves the target's ACL (REPLACEFILE_WRITE_THROUGH
//!    semantics via ReplaceFileW) and keeps a recovery backup.
//! 3. Failure paths fail closed: on any error the original file must remain
//!    intact and the error is reported with the native error code.

use std::path::Path;

#[derive(Debug, PartialEq, Eq)]
pub enum PointerCommitError {
    Native(u32),
    Unsupported,
}

impl std::fmt::Display for PointerCommitError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PointerCommitError::Native(code) => {
                write!(f, "native pointer commit failed with win32 error {code}")
            }
            PointerCommitError::Unsupported => {
                write!(f, "pointer commit is unsupported on this platform")
            }
        }
    }
}

impl std::error::Error for PointerCommitError {}

/// Atomically replace `current_path` with `temporary_path`.
///
/// `ReplaceFileW` keeps the replacement metadata-preserving and leaves a
/// `.bak` recovery copy of the original. Returns Ok(()) when the pointer now
/// points at the new bytes.
#[cfg(windows)]
fn wide(path: &Path) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    path.as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

#[cfg(windows)]
pub fn commit_pointer(
    temporary_path: &Path,
    current_path: &Path,
) -> Result<(), PointerCommitError> {
    use windows_sys::Win32::Foundation::GetLastError;
    use windows_sys::Win32::Storage::FileSystem::{
        ReplaceFileW, REPLACEFILE_WRITE_THROUGH,
    };

    let replaced = wide(current_path);
    let replacement = wide(temporary_path);
    // Backup name: current.json -> current.json.bak-<rand-free deterministic suffix>
    let backup = {
        let mut name = current_path.as_os_str().to_os_string();
        name.push(".bak");
        wide(Path::new(&name))
    };

    let ok = unsafe {
        ReplaceFileW(
            replaced.as_ptr(),
            replacement.as_ptr(),
            backup.as_ptr(),
            REPLACEFILE_WRITE_THROUGH,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if ok == 0 {
        let code = unsafe { GetLastError() };
        // 2 = ERROR_FILE_NOT_FOUND: first publish has no current.json yet.
        // Fall back to a plain rename which is atomic on NTFS same-volume.
        if code == 2 {
            return first_publish_rename(temporary_path, current_path);
        }
        return Err(PointerCommitError::Native(code));
    }
    Ok(())
}

#[cfg(windows)]
fn first_publish_rename(
    temporary_path: &Path,
    current_path: &Path,
) -> Result<(), PointerCommitError> {
    use windows_sys::Win32::Foundation::GetLastError;
    use windows_sys::Win32::Storage::FileSystem::MoveFileExW;
    use windows_sys::Win32::Storage::FileSystem::MOVEFILE_WRITE_THROUGH;

    let from = wide(temporary_path);
    let to = wide(current_path);
    let ok = unsafe {
        MoveFileExW(
            from.as_ptr(),
            to.as_ptr(),
            MOVEFILE_WRITE_THROUGH,
        )
    };
    if ok == 0 {
        return Err(PointerCommitError::Native(unsafe { GetLastError() }));
    }
    Ok(())
}

#[cfg(not(windows))]
pub fn commit_pointer(
    _temporary_path: &Path,
    _current_path: &Path,
) -> Result<(), PointerCommitError> {
    Err(PointerCommitError::Unsupported)
}
