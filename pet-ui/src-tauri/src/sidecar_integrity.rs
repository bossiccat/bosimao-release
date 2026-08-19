use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};

use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::sidecar::{IntegritySpec, SidecarError, SidecarSpec};
use crate::sidecar_runtime_trust::validate_runtime_trust;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RuntimeFile {
    path: String,
    sha256: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExternalBinManifest {
    build_input_file: String,
    installed_file: String,
    target_triple: String,
    sha256: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ProvenanceManifest {
    schema_version: u32,
    build_script_version: String,
    target_triple: String,
    electron_version: String,
    trtc_sdk_version: String,
    sidecar_package_lock_sha256: String,
    external_bin: ExternalBinManifest,
    native_files: Vec<RuntimeFile>,
    runtime_files: Vec<RuntimeFile>,
    bundle_resources: BTreeMap<String, String>,
}

/// 判定 symlink 或 Windows reparse point（junction/mount point/symlink）。
/// `metadata` 必须是 `symlink_metadata` 的结果（不跟随链接）。
pub(crate) fn is_symlink_or_reparse(metadata: &std::fs::Metadata) -> bool {
    if metadata.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return true;
        }
    }
    false
}

fn ensure_regular_not_symlink(path: &Path) -> Result<(), ()> {
    let metadata = std::fs::symlink_metadata(path).map_err(|_| ())?;
    if is_symlink_or_reparse(&metadata) || !metadata.is_file() {
        return Err(());
    }
    Ok(())
}

pub(crate) fn validate_hash(value: &str) -> Result<(), String> {
    if value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        Ok(())
    } else {
        Err(value.to_string())
    }
}

pub(crate) fn sha256_file(path: &Path) -> Result<String, SidecarError> {
    let bytes =
        std::fs::read(path).map_err(|error| SidecarError::SpawnFailed(error.to_string()))?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

pub(crate) fn validate_runtime(spec: &SidecarSpec) -> Result<(), SidecarError> {
    let IntegritySpec {
        manifest_path,
        expected_manifest_sha256: expected_digest,
        runtime_dir,
    } = &spec.integrity;
    ensure_regular_not_symlink(manifest_path)
        .map_err(|_| SidecarError::ManifestMissing(manifest_path.clone()))?;
    ensure_regular_not_symlink(&spec.binary_path)
        .map_err(|_| SidecarError::BinaryMissing(spec.binary_path.clone()))?;
    validate_hash(expected_digest).map_err(|invalid| {
        if invalid.is_empty() {
            SidecarError::ManifestDigestMissing
        } else {
            SidecarError::ManifestDigestInvalid(invalid)
        }
    })?;
    let actual_digest = sha256_file(manifest_path)?;
    if actual_digest != *expected_digest {
        return Err(SidecarError::ManifestDigestMismatch {
            expected: expected_digest.clone(),
            actual: actual_digest,
        });
    }
    let bytes = std::fs::read(manifest_path)
        .map_err(|error| SidecarError::SpawnFailed(error.to_string()))?;
    let manifest: ProvenanceManifest =
        serde_json::from_slice(&bytes).map_err(|_| SidecarError::ManifestInvalid)?;
    validate_metadata(&manifest, spec)?;
    let runtime_by_path = validate_runtime_entries(&manifest.runtime_files, runtime_dir)?;
    validate_native_subset(&manifest.native_files, &runtime_by_path)?;
    let actual = list_runtime_files(runtime_dir, manifest_path)?;
    if actual != runtime_by_path.keys().cloned().collect() {
        return Err(SidecarError::RuntimeSetMismatch);
    }
    // hash 完整性全部通过后，再执行生产可信门（体积 + PE 头），
    // 保证既有篡改场景（RuntimeHashMismatch/ManifestDigestMismatch）语义不变。
    validate_runtime_trust(spec)?;
    Ok(())
}

fn validate_runtime_entries(
    entries: &[RuntimeFile],
    runtime_dir: &Path,
) -> Result<BTreeMap<String, String>, SidecarError> {
    let mut recorded = BTreeMap::new();
    for item in entries {
        validate_path(&item.path)?;
        validate_hash(&item.sha256).map_err(|_| SidecarError::ManifestInvalid)?;
        if recorded
            .insert(item.path.clone(), item.sha256.clone())
            .is_some()
        {
            return Err(SidecarError::ManifestInvalid);
        }
        let file = runtime_dir.join(&item.path);
        if !file.is_file() || sha256_file(&file)? != item.sha256 {
            return Err(SidecarError::RuntimeHashMismatch(file));
        }
    }
    Ok(recorded)
}

fn validate_native_subset(
    native: &[RuntimeFile],
    runtime: &BTreeMap<String, String>,
) -> Result<(), SidecarError> {
    const REQUIRED: [&str; 5] = [
        "resources/app/node_modules/trtc-electron-sdk/build/Release/trtc_electron_sdk.node",
        "resources/app/node_modules/trtc-electron-sdk/build/Release/liteav.dll",
        "resources/app/node_modules/trtc-electron-sdk/build/Release/txffmpeg.dll",
        "resources/app/node_modules/trtc-electron-sdk/build/Release/txsoundtouch.dll",
        "resources/app/node_modules/trtc-electron-sdk/build/Release/liteav_media_server.exe",
    ];
    let mut paths = BTreeSet::new();
    for item in native {
        validate_path(&item.path)?;
        validate_hash(&item.sha256).map_err(|_| SidecarError::ManifestInvalid)?;
        if !paths.insert(item.path.clone()) || runtime.get(&item.path) != Some(&item.sha256) {
            return Err(SidecarError::ManifestInvalid);
        }
    }
    if paths != REQUIRED.into_iter().map(str::to_string).collect() {
        return Err(SidecarError::ManifestInvalid);
    }
    Ok(())
}

pub(crate) fn validate_path(value: &str) -> Result<(), SidecarError> {
    let path = Path::new(value);
    if value.is_empty()
        || value.contains('\\')
        || path.is_absolute()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(SidecarError::ManifestInvalid);
    }
    Ok(())
}

fn validate_metadata(
    manifest: &ProvenanceManifest,
    spec: &SidecarSpec,
) -> Result<(), SidecarError> {
    if manifest.schema_version != 1
        || manifest.target_triple != "x86_64-pc-windows-msvc"
        || manifest.external_bin.installed_file
            != spec
                .binary_path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or_default()
        || manifest.external_bin.build_input_file
            != format!(
                "jax-rtc-sidecar-{}.exe",
                manifest.external_bin.target_triple
            )
        || manifest.external_bin.target_triple != "x86_64-pc-windows-msvc"
        || manifest.external_bin.target_triple != manifest.target_triple
        || manifest.external_bin.sha256 != spec.expected_sha256
        || manifest.build_script_version.is_empty()
        || manifest.electron_version.is_empty()
        || manifest.trtc_sdk_version.is_empty()
        || validate_hash(&manifest.sidecar_package_lock_sha256).is_err()
        || manifest.bundle_resources.len() != 1
        || manifest
            .bundle_resources
            .get("binaries/jax-rtc-sidecar-runtime/")
            .map(String::as_str)
            != Some("jax-rtc-sidecar-runtime/")
    {
        return Err(SidecarError::ManifestInvalid);
    }
    Ok(())
}

fn list_runtime_files(root: &Path, manifest_path: &Path) -> Result<BTreeSet<String>, SidecarError> {
    fn visit(
        root: &Path,
        current: &Path,
        files: &mut BTreeSet<String>,
    ) -> Result<(), SidecarError> {
        for entry in std::fs::read_dir(current)
            .map_err(|error| SidecarError::SpawnFailed(error.to_string()))?
        {
            let entry = entry.map_err(|error| SidecarError::SpawnFailed(error.to_string()))?;
            let entry_path = entry.path();
            let metadata = std::fs::symlink_metadata(&entry_path)
                .map_err(|error| SidecarError::SpawnFailed(error.to_string()))?;
            if is_symlink_or_reparse(&metadata) {
                return Err(SidecarError::ManifestInvalid);
            }
            if metadata.is_dir() {
                visit(root, &entry_path, files)?;
            } else if metadata.is_file() {
                files.insert(normalized_relative(root, entry_path)?);
            }
        }
        Ok(())
    }
    let mut files = BTreeSet::new();
    visit(root, root, &mut files)?;
    for excluded in [
        manifest_path.file_name().and_then(|name| name.to_str()),
        Some("jax-rtc-sidecar.exe.sha256"),
        Some("jax-rtc-sidecar.provenance.sha256"),
        // generation 布局下 runtime_dir 即 selected generation 目录，其中
        // generation.json 是 pointer 协议元数据，不属于 provenance 的 runtime_files。
        Some("generation.json"),
    ]
    .into_iter()
    .flatten()
    {
        files.remove(excluded);
    }
    Ok(files)
}

fn normalized_relative(root: &Path, file: PathBuf) -> Result<String, SidecarError> {
    Ok(file
        .strip_prefix(root)
        .map_err(|_| SidecarError::ManifestInvalid)?
        .to_string_lossy()
        .replace('\\', "/"))
}
