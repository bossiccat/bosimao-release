//! ADR-027 Windows native atomic `current.json` pointer replacement helper.
//!
//! Exposes exactly two operations, selected by the first argument:
//!
//! - `replace <temporaryPath> <currentPath>`: atomically replace an existing
//!   pointer using [`ReplaceFileW`] with `REPLACEFILE_WRITE_THROUGH`. The old
//!   pointer is preserved until the replacement completes; there is no unlink
//!   window.
//! - `create <temporaryPath> <currentPath>`: create the first pointer using
//!   [`MoveFileExW`] with `MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH`.
//!
//! Both operations require the temporary and current pointers to live on the
//! same NTFS volume; a cross-volume request is rejected before any call is
//! issued. On failure the old pointer is never unlinked by this helper.
//!
//! Structured JSON is written to stdout (`operation`, `success`, decimal
//! `nativeErrorCode`), diagnostics to stderr, and the exit code is non-zero on
//! failure. `nativeErrorCode` is `GetLastError()` converted to decimal.

#[cfg(windows)]
mod imp {
    use std::ffi::{OsStr, OsString};
    use std::os::windows::ffi::OsStrExt;

    use windows_sys::Win32::Foundation::{GetLastError, ERROR_NOT_SAME_DEVICE};
    use windows_sys::Win32::Storage::FileSystem::{
        GetFullPathNameW, GetVolumePathNameW, MoveFileExW, ReplaceFileW,
        MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH, REPLACEFILE_WRITE_THROUGH,
    };

    enum Operation {
        Replace,
        Create,
    }

    impl Operation {
        fn from(value: &str) -> Option<Operation> {
            match value {
                "replace" => Some(Operation::Replace),
                "create" => Some(Operation::Create),
                _ => None,
            }
        }

        fn name(&self) -> &'static str {
            match self {
                Operation::Replace => "replace",
                Operation::Create => "create",
            }
        }
    }

    fn to_wide(value: &OsStr) -> Vec<u16> {
        value.encode_wide().chain(std::iter::once(0)).collect()
    }

    fn full_path(value: &OsStr) -> Result<String, u32> {
        let wide = to_wide(value);
        let mut buffer = vec![0u16; 32768];
        let length = unsafe {
            GetFullPathNameW(
                wide.as_ptr(),
                buffer.len() as u32,
                buffer.as_mut_ptr(),
                std::ptr::null_mut(),
            )
        };
        if length == 0 || length as usize >= buffer.len() {
            return Err(unsafe { GetLastError() });
        }
        Ok(String::from_utf16_lossy(&buffer[..length as usize]))
    }

    fn volume_path_name(value: &OsStr) -> Result<String, u32> {
        let wide = to_wide(value);
        let mut buffer = vec![0u16; 32768];
        let ok = unsafe { GetVolumePathNameW(wide.as_ptr(), buffer.as_mut_ptr(), buffer.len() as u32) };
        if ok == 0 {
            return Err(unsafe { GetLastError() });
        }
        let end = buffer.iter().position(|&c| c == 0).unwrap_or(buffer.len());
        Ok(String::from_utf16_lossy(&buffer[..end]))
    }

    fn same_volume(temporary: &OsStr, current: &OsStr) -> Result<bool, u32> {
        let temporary_volume = volume_path_name(temporary)?;
        let current_volume = volume_path_name(current)?;
        Ok(temporary_volume.eq_ignore_ascii_case(&current_volume))
    }

    fn json_escape(value: &str) -> String {
        let mut out = String::with_capacity(value.len());
        for c in value.chars() {
            match c {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
                c => out.push(c),
            }
        }
        out
    }

    fn fail(operation: &str, code: u32, context: &str) -> ! {
        println!(
            "{{\"operation\":\"{}\",\"success\":false,\"nativeErrorCode\":{}}}",
            operation, code
        );
        eprintln!(
            "sidecar-pointer-replace: {}: native error {}",
            context, code
        );
        std::process::exit(1);
    }

    pub fn main() {
        let args: Vec<OsString> = std::env::args_os().skip(1).collect();
        if args.len() != 3 {
            eprintln!("usage: sidecar-pointer-replace <replace|create> <temporaryPath> <currentPath>");
            std::process::exit(2);
        }
        let operation = match Operation::from(args[0].to_str().unwrap_or("")) {
            Some(operation) => operation,
            None => {
                eprintln!("invalid operation (expected replace or create)");
                std::process::exit(2);
            }
        };
        let operation_name = operation.name();

        let temporary_full = match full_path(&args[1]) {
            Ok(path) => path,
            Err(code) => fail(operation_name, code, "resolving temporary path"),
        };
        let current_full = match full_path(&args[2]) {
            Ok(path) => path,
            Err(code) => fail(operation_name, code, "resolving current path"),
        };

        match same_volume(OsStr::new(&temporary_full), OsStr::new(&current_full)) {
            Ok(true) => {}
            Ok(false) => fail(
                operation_name,
                ERROR_NOT_SAME_DEVICE,
                "temporary and current pointers must share one NTFS volume",
            ),
            Err(code) => fail(operation_name, code, "determining volume"),
        }

        let temporary_wide = to_wide(OsStr::new(&temporary_full));
        let current_wide = to_wide(OsStr::new(&current_full));

        let ok = match operation {
            Operation::Replace => unsafe {
                ReplaceFileW(
                    current_wide.as_ptr(),
                    temporary_wide.as_ptr(),
                    std::ptr::null(),
                    REPLACEFILE_WRITE_THROUGH,
                    std::ptr::null(),
                    std::ptr::null(),
                )
            },
            Operation::Create => unsafe {
                MoveFileExW(
                    temporary_wide.as_ptr(),
                    current_wide.as_ptr(),
                    MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
                )
            },
        };

        if ok == 0 {
            let code = unsafe { GetLastError() };
            fail(operation_name, code, "native pointer replacement failed");
        }

        println!(
            "{{\"operation\":\"{}\",\"success\":true,\"nativeErrorCode\":0,\"temporaryPath\":\"{}\",\"currentPath\":\"{}\"}}",
            operation_name,
            json_escape(&temporary_full),
            json_escape(&current_full)
        );
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn json_escape_handles_special_characters() {
            assert_eq!(json_escape("a\"b\\c\n"), "a\\\"b\\\\c\\n");
            assert_eq!(json_escape("plain"), "plain");
            assert_eq!(json_escape("\u{1}"), "\\u0001");
        }

        #[test]
        fn same_volume_is_true_within_one_directory() {
            let directory = std::env::temp_dir();
            let first = directory.join("jax_vol_a.tmp");
            let second = directory.join("jax_vol_b.tmp");
            let result = same_volume(
                OsStr::new(first.to_str().unwrap()),
                OsStr::new(second.to_str().unwrap()),
            )
            .unwrap();
            assert!(result);
        }

        #[test]
        fn parses_operations() {
            assert!(matches!(Operation::from("replace"), Some(Operation::Replace)));
            assert!(matches!(Operation::from("create"), Some(Operation::Create)));
            assert!(Operation::from("bogus").is_none());
        }
    }
}

#[cfg(windows)]
fn main() {
    imp::main();
}

#[cfg(not(windows))]
fn main() {
    eprintln!("sidecar-pointer-replace: unsupported platform; Windows NTFS only");
    std::process::exit(2);
}
