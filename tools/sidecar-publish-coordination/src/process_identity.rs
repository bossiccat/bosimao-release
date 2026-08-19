//! Real Windows process identity probe (audit P0-3).
//!
//! Semantics:
//! - `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` failing with
//!   ERROR_INVALID_PARAMETER (87) proves the PID slot is unused -> Absent.
//! - Failing with any other error (typically ERROR_ACCESS_DENIED) means the
//!   process exists but cannot be queried -> Unavailable (fail-closed: the
//!   caller must refuse to reclaim, never guess).
//! - Success -> `GetProcessTimes` yields the creation FILETIME, rendered as
//!   RFC3339 UTC; the identity string is a SHA-256 over the raw creation
//!   FILETIME plus the full process image path, so a PID reuse by a
//!   different executable never matches the previous owner.

use crate::owner::ProcessIdentity;
use sha2::{Digest, Sha256};

pub fn probe(pid: u32) -> ProcessIdentity {
    if pid == 0 {
        return ProcessIdentity::Unavailable {
            reason: "pid 0 is never a valid owner".to_owned(),
        };
    }
    match os_probe(pid) {
        Ok(identity) => identity,
        Err(reason) => ProcessIdentity::Unavailable { reason },
    }
}

#[cfg(windows)]
fn os_probe(pid: u32) -> Result<ProcessIdentity, String> {
    use windows_sys::Win32::Foundation::{
        CloseHandle, GetLastError, ERROR_INVALID_PARAMETER, INVALID_HANDLE_VALUE,
    };
    use windows_sys::Win32::System::Threading::{
        GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };

    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if handle == INVALID_HANDLE_VALUE || handle.is_null() {
        let code = unsafe { GetLastError() };
        if code == ERROR_INVALID_PARAMETER {
            return Ok(ProcessIdentity::Absent);
        }
        return Err(format!("OpenProcess({pid}) failed with win32 error {code}"));
    }

    let mut creation = windows_sys::Win32::Foundation::FILETIME::default();
    let mut exit_time = windows_sys::Win32::Foundation::FILETIME::default();
    let mut kernel = windows_sys::Win32::Foundation::FILETIME::default();
    let mut user = windows_sys::Win32::Foundation::FILETIME::default();
    let ok = unsafe {
        GetProcessTimes(handle, &mut creation, &mut exit_time, &mut kernel, &mut user)
    };
    let image = query_image_path(handle);
    unsafe { CloseHandle(handle) };

    if ok == 0 {
        return Err(format!(
            "GetProcessTimes({pid}) failed with win32 error {}",
            unsafe { GetLastError() }
        ));
    }

    let raw_creation = [
        creation.dwLowDateTime,
        creation.dwHighDateTime,
    ];
    let creation_time = filetime_to_rfc3339(raw_creation[0], raw_creation[1])?;
    let identity = identity_string(raw_creation, image.as_deref().unwrap_or(""));

    Ok(ProcessIdentity::Verified {
        creation_time,
        identity,
    })
}

#[cfg(windows)]
fn query_image_path(
    handle: windows_sys::Win32::Foundation::HANDLE,
) -> Option<String> {
    use windows_sys::Win32::System::Threading::QueryFullProcessImageNameW;

    let mut buffer = [0u16; 1024];
    let mut length = buffer.len() as u32;
    let ok = unsafe {
        QueryFullProcessImageNameW(handle, 0, buffer.as_mut_ptr(), &mut length)
    };
    if ok == 0 {
        return None;
    }
    Some(String::from_utf16_lossy(&buffer[..length as usize]).to_ascii_lowercase())
}

#[cfg(not(windows))]
fn os_probe(_pid: u32) -> Result<ProcessIdentity, String> {
    Err("process identity probe is windows-only".to_owned())
}

/// FILETIME (100ns since 1601-01-01 UTC) -> `YYYY-MM-DDTHH:MM:SS.mmmZ`.
fn filetime_to_rfc3339(low: u32, high: u32) -> Result<String, String> {
    let intervals = (low as u64) | ((high as u64) << 32);
    // 116444736000000000 intervals between 1601-01-01 and 1970-01-01.
    if intervals < 116_444_736_000_000_000 {
        return Err("process creation time predates the unix epoch".to_owned());
    }
    let since_epoch = intervals - 116_444_736_000_000_000;
    let seconds = (since_epoch / 10_000_000) as i64;
    let millis = (since_epoch % 10_000_000) / 10_000;

    let days = seconds.div_euclid(86_400);
    let seconds_of_day = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let hour = seconds_of_day / 3_600;
    let minute = (seconds_of_day % 3_600) / 60;
    let second = seconds_of_day % 60;

    Ok(format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{millis:03}Z"
    ))
}

/// Howard Hinnant's civil-from-days algorithm.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn identity_string(raw_creation: [u32; 2], image: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(raw_creation[0].to_le_bytes());
    hasher.update(raw_creation[1].to_le_bytes());
    hasher.update(image.as_bytes());
    let digest = hasher.finalize();
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filetime_formats_epoch_correctly() {
        // 1970-01-01T00:00:00.000Z == 116444736000000000 intervals
        // == low 0xD53E8000, high 27111902.
        assert_eq!(
            filetime_to_rfc3339(0xd53e_8000, 27_111_902).unwrap(),
            "1970-01-01T00:00:00.000Z"
        );
    }

    #[test]
    fn filetime_rejects_pre_epoch() {
        assert!(filetime_to_rfc3339(0, 0).is_err());
    }

    #[test]
    fn probe_rejects_pid_zero() {
        assert!(matches!(
            probe(0),
            ProcessIdentity::Unavailable { .. }
        ));
    }

    #[test]
    fn civil_algorithm_matches_known_dates() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(19_723), (2024, 1, 1));
    }

    #[cfg(windows)]
    #[test]
    fn probing_current_process_verifies_identity() {
        let identity = probe(std::process::id());
        match identity {
            ProcessIdentity::Verified {
                creation_time,
                identity,
            } => {
                assert!(creation_time.ends_with('Z'));
                assert!(!identity.is_empty());
            }
            other => panic!("expected Verified for the running process, got {other:?}"),
        }
    }

    #[cfg(windows)]
    #[test]
    fn probing_garbage_pid_reports_absent() {
        // 0x4FFFFFFF is in the reserved range: OpenProcess -> INVALID_PARAM.
        assert_eq!(probe(0x4FFF_FFFF), ProcessIdentity::Absent);
    }
}
