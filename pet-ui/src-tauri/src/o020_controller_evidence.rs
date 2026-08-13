#![cfg(all(windows, feature = "credential-test-support"))]

use std::ffi::c_void;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::Serialize;
use sha2::{Digest, Sha256};
use windows::core::PCWSTR;
use windows::Win32::Security::{
    SetFileSecurityW, DACL_SECURITY_INFORMATION, PROTECTED_DACL_SECURITY_INFORMATION,
};
use windows::Win32::Storage::FileSystem::{
    GetDriveTypeW, GetFileAttributesW, MoveFileExW, FILE_ATTRIBUTE_REPARSE_POINT,
    INVALID_FILE_ATTRIBUTES, MOVEFILE_WRITE_THROUGH,
};
use windows::Win32::System::Com::CoTaskMemFree;
use windows::Win32::System::WindowsProgramming::DRIVE_REMOTE;
use windows::Win32::UI::Shell::{FOLDERID_LocalAppData, SHGetKnownFolderPath, KF_FLAG_DEFAULT};

use crate::credential::{CredentialError, CredentialErrorCode};
use crate::credential_windows_lock::SecurityDescriptor;

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactReference {
    pub artifact_relative_path: String,
    pub artifact_hash: String,
    pub byte_size: u64,
}

pub(crate) struct EvidenceRun {
    root: PathBuf,
}

impl EvidenceRun {
    pub(crate) fn create(run_id: &str) -> Result<Self, CredentialError> {
        let base = local_app_data()?
            .join("JaxPet")
            .join("test-evidence")
            .join("o020")
            .join("v1");
        reject_remote_or_reparse(&base)?;
        let root = base.join(run_id);
        fs::create_dir_all(root.join("checkpoints")).map_err(|_| evidence_error())?;
        for protected in [&base, &root, &root.join("checkpoints")] {
            protect_directory(protected)?;
        }
        reject_remote_or_reparse(&root)?;
        Ok(Self { root })
    }

    pub(crate) fn write_checkpoint<T: Serialize>(
        &self,
        ordinal: usize,
        checkpoint: &str,
        value: &T,
    ) -> Result<ArtifactReference, CredentialError> {
        let file_name = checkpoint_file(ordinal, checkpoint).ok_or_else(evidence_error)?;
        self.write_json(&format!("checkpoints/{file_name}"), value)
    }

    pub(crate) fn write_manifest<T: Serialize>(
        &self,
        value: &T,
    ) -> Result<ArtifactReference, CredentialError> {
        self.write_json("manifest.json", value)
    }

    fn write_json<T: Serialize>(
        &self,
        relative: &str,
        value: &T,
    ) -> Result<ArtifactReference, CredentialError> {
        let final_path = self.root.join(Path::new(relative));
        let temporary = temporary_path(&final_path)?;
        let mut bytes = serde_json::to_vec(value).map_err(|_| evidence_error())?;
        bytes.push(b'\n');
        let mut output = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)
            .map_err(|_| evidence_error())?;
        output.write_all(&bytes).map_err(|_| evidence_error())?;
        output.sync_all().map_err(|_| evidence_error())?;
        drop(output);
        move_write_through(&temporary, &final_path)?;
        let persisted = std::fs::read(&final_path).map_err(|_| evidence_error())?;
        if persisted != bytes {
            return Err(evidence_error());
        }
        serde_json::from_slice::<serde_json::Value>(&persisted).map_err(|_| evidence_error())?;
        Ok(ArtifactReference {
            artifact_relative_path: relative.replace('\\', "/"),
            artifact_hash: format!("{:x}", Sha256::digest(&persisted)),
            byte_size: persisted.len() as u64,
        })
    }
}

fn checkpoint_file(ordinal: usize, checkpoint: &str) -> Option<&'static str> {
    match (ordinal, checkpoint) {
        (1, "stage-write") => Some("01-stage-write.json"),
        (2, "backup-write") => Some("02-backup-write.json"),
        (3, "active-write") => Some("03-active-write.json"),
        (4, "active-verify") => Some("04-active-verify.json"),
        (5, "delete-backup") => Some("05-delete-backup.json"),
        (6, "delete-staging") => Some("06-delete-staging.json"),
        _ => None,
    }
}

fn local_app_data() -> Result<PathBuf, CredentialError> {
    let pointer = unsafe {
        SHGetKnownFolderPath(&FOLDERID_LocalAppData, KF_FLAG_DEFAULT, None)
            .map_err(|_| evidence_error())?
    };
    let result = unsafe { pointer.to_string() }
        .map(PathBuf::from)
        .map_err(|_| evidence_error());
    unsafe { CoTaskMemFree(Some(pointer.as_ptr().cast::<c_void>())) };
    result
}

fn temporary_path(final_path: &Path) -> Result<PathBuf, CredentialError> {
    use windows::Win32::Security::Cryptography::{
        BCryptGenRandom, BCRYPT_USE_SYSTEM_PREFERRED_RNG,
    };
    let mut random = [0_u8; 16];
    if unsafe { BCryptGenRandom(None, &mut random, BCRYPT_USE_SYSTEM_PREFERRED_RNG) }.is_err() {
        return Err(evidence_error());
    }
    let name = final_path
        .file_name()
        .ok_or_else(evidence_error)?
        .to_string_lossy();
    Ok(final_path.with_file_name(format!(".{name}.{:x}.tmp", Sha256::digest(random))))
}

fn protect_directory(path: &Path) -> Result<(), CredentialError> {
    if !path.exists() {
        return Ok(());
    }
    let mut descriptor = SecurityDescriptor::current_user_and_system()?;
    let path = wide(path);
    let information = DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION;
    let ok = unsafe { SetFileSecurityW(PCWSTR(path.as_ptr()), information, descriptor.as_psd()) };
    if ok.as_bool() {
        Ok(())
    } else {
        Err(evidence_error())
    }
}

fn reject_remote_or_reparse(path: &Path) -> Result<(), CredentialError> {
    let path_text = path.as_os_str().to_string_lossy();
    if path_text.starts_with("\\\\") || path_text.starts_with("//") {
        return Err(evidence_error());
    }
    let root = path.ancestors().last().ok_or_else(evidence_error)?;
    let root_wide = wide(root);
    if unsafe { GetDriveTypeW(PCWSTR(root_wide.as_ptr())) } == DRIVE_REMOTE {
        return Err(evidence_error());
    }
    for ancestor in path.ancestors().filter(|entry| entry.exists()) {
        let wide_path = wide(ancestor);
        let attributes = unsafe { GetFileAttributesW(PCWSTR(wide_path.as_ptr())) };
        if attributes == INVALID_FILE_ATTRIBUTES || attributes & FILE_ATTRIBUTE_REPARSE_POINT.0 != 0
        {
            return Err(evidence_error());
        }
    }
    Ok(())
}

fn move_write_through(from: &Path, to: &Path) -> Result<(), CredentialError> {
    let from = wide(from);
    let to = wide(to);
    unsafe {
        MoveFileExW(
            PCWSTR(from.as_ptr()),
            PCWSTR(to.as_ptr()),
            MOVEFILE_WRITE_THROUGH,
        )
    }
    .map_err(|_| evidence_error())
}
fn wide(path: &Path) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    path.as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}
fn evidence_error() -> CredentialError {
    CredentialError::new(CredentialErrorCode::CredentialRecoveryFailed)
}
