//! sidecar_runtime_pointer.rs — ADR-027 §3 Rust reader：immutable generation 快照解析。
//!
//! resolver 输入是稳定根目录（`<resource_dir>/jax-rtc-sidecar-runtime`）与 build-time
//! `COMPILED_MANIFEST_SHA256`，输出一个不可变 generation 的 typed `ResolvedRuntime`。
//! 任何失败都 fail closed：返回结构化 [`ResolverError`] 并拒绝启动，绝不构造
//! fallback sentinel 路径，也绝不在错误里静默降级。

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::{Path, PathBuf};

use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::sidecar::{IntegritySpec, SidecarSpec};
use crate::sidecar_integrity::{is_symlink_or_reparse, validate_hash, validate_path};

/// 持有 generation 租约的 RAII guard（ADR-027 §5）。
///
/// 语义：解析成功返回的 snapshot 必须存活到 sidecar child 退出，之后由
/// `SidecarSupervisor` 在 child 退出时显式释放。跨进程 lease 文件创建、
/// `reader-gc.lock` barrier 与租约续期属于后续任务，当前 `Drop` 不落盘，
/// 仅承担"持有即有效"的生命周期契约。
#[derive(Debug)]
pub struct GenerationLease {
    generation: String,
}

impl GenerationLease {
    fn new(generation: String) -> Self {
        Self { generation }
    }

    pub fn generation(&self) -> &str {
        &self.generation
    }
}

impl Drop for GenerationLease {
    fn drop(&mut self) {
        // 跨进程 lease 文件移除延后到后续任务；此处不执行任何 I/O。
    }
}

/// 一次解析得到的不可变 generation 快照。
#[derive(Debug)]
pub struct ResolvedRuntime {
    generation_root: PathBuf,
    binary_path: PathBuf,
    manifest_path: PathBuf,
    manifest_sha256: String,
    expected_hashes: BTreeMap<String, String>,
    lease: GenerationLease,
}

impl ResolvedRuntime {
    pub fn generation_root(&self) -> &Path {
        &self.generation_root
    }

    pub fn binary_path(&self) -> &Path {
        &self.binary_path
    }

    pub fn manifest_path(&self) -> &Path {
        &self.manifest_path
    }

    pub fn manifest_sha256(&self) -> &str {
        &self.manifest_sha256
    }

    pub fn generation(&self) -> &str {
        self.lease.generation()
    }

    /// generation.json 声明的闭集 payload 哈希（relpath -> sha256）。
    pub fn expected_hashes(&self) -> &BTreeMap<String, String> {
        &self.expected_hashes
    }

    /// 消费快照，产出启动契约与租约。调用方必须把租约存活到 child 退出。
    pub fn into_sidecar_spec(
        self,
        args: Vec<String>,
        ca_cert_path: PathBuf,
        graceful_timeout: std::time::Duration,
        kill_timeout: std::time::Duration,
    ) -> (SidecarSpec, GenerationLease) {
        let binary_sha_path = self.generation_root.join("jax-rtc-sidecar.exe.sha256");
        // 该文件已通过闭集校验，缺失/读取失败属于极端竞态，fail closed。
        let expected_sha256 = std::fs::read_to_string(&binary_sha_path)
            .map(|s| s.trim().to_string())
            .unwrap_or_default();
        let spec = SidecarSpec {
            binary_path: self.binary_path.clone(),
            expected_sha256,
            integrity: IntegritySpec {
                manifest_path: self.manifest_path.clone(),
                expected_manifest_sha256: self.manifest_sha256.clone(),
                runtime_dir: self.generation_root.clone(),
            },
            args,
            ca_cert_path,
            graceful_timeout,
            kill_timeout,
        };
        (spec, self.lease)
    }
}

/// 结构化、可诊断的 resolver 失败。无 fallback sentinel 路径。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResolverError {
    ResourceDir(String),
    PointerMissing(PathBuf),
    PointerInvalidJson(PathBuf),
    PointerUnknownField(PathBuf),
    PointerUnknownSchema(PathBuf),
    PointerInvalidGeneration(PathBuf),
    PointerInvalidManifestHash(PathBuf),
    GenerationDirMissing(PathBuf),
    GenerationMetadataMissing(PathBuf),
    GenerationMetadataInvalidJson(PathBuf),
    GenerationMetadataUnknownField(PathBuf),
    GenerationMetadataUnknownSchema(PathBuf),
    GenerationIdMismatch { pointer: String, metadata: String },
    ManifestHashMismatch { pointer: String, metadata: String },
    ProvenanceMissing(PathBuf),
    ProvenanceGenerationMismatch { generation: String, actual: String },
    ProvenanceDigestMismatch { expected: String, actual: String },
    CompiledManifestMismatch { expected: String, actual: String },
    InvalidFilePath(String),
    InvalidFileHash { path: String, hash: String },
    PayloadMissing(PathBuf),
    ExtraPayload(String),
    PayloadHashMismatch(PathBuf),
    PayloadSymlink(PathBuf),
    Io(String),
}

impl fmt::Display for ResolverError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ResolverError::ResourceDir(detail) => write!(f, "resource dir unavailable: {detail}"),
            ResolverError::PointerMissing(path) => {
                write!(f, "current pointer missing: {}", path.display())
            }
            ResolverError::PointerInvalidJson(path) => {
                write!(f, "current pointer invalid json: {}", path.display())
            }
            ResolverError::PointerUnknownField(path) => {
                write!(f, "current pointer has unknown field: {}", path.display())
            }
            ResolverError::PointerUnknownSchema(path) => {
                write!(f, "current pointer unknown schema: {}", path.display())
            }
            ResolverError::PointerInvalidGeneration(path) => {
                write!(f, "current pointer invalid generation id: {}", path.display())
            }
            ResolverError::PointerInvalidManifestHash(path) => {
                write!(f, "current pointer invalid manifest hash: {}", path.display())
            }
            ResolverError::GenerationDirMissing(path) => {
                write!(f, "generation dir missing: {}", path.display())
            }
            ResolverError::GenerationMetadataMissing(path) => {
                write!(f, "generation metadata missing: {}", path.display())
            }
            ResolverError::GenerationMetadataInvalidJson(path) => {
                write!(f, "generation metadata invalid json: {}", path.display())
            }
            ResolverError::GenerationMetadataUnknownField(path) => {
                write!(f, "generation metadata unknown field: {}", path.display())
            }
            ResolverError::GenerationMetadataUnknownSchema(path) => {
                write!(f, "generation metadata unknown schema: {}", path.display())
            }
            ResolverError::GenerationIdMismatch { pointer, metadata } => write!(
                f,
                "generation id mismatch: pointer={pointer} metadata={metadata}"
            ),
            ResolverError::ManifestHashMismatch { pointer, metadata } => write!(
                f,
                "manifest hash mismatch: pointer={pointer} metadata={metadata}"
            ),
            ResolverError::ProvenanceMissing(path) => {
                write!(f, "provenance missing: {}", path.display())
            }
            ResolverError::ProvenanceGenerationMismatch {
                generation,
                actual,
            } => write!(
                f,
                "provenance digest does not match generation id: generation={generation} sha256={actual}"
            ),
            ResolverError::ProvenanceDigestMismatch { expected, actual } => write!(
                f,
                "manifest hash does not match provenance bytes: expected={expected} actual={actual}"
            ),
            ResolverError::CompiledManifestMismatch { expected, actual } => write!(
                f,
                "manifest hash does not match compiled digest: expected={expected} actual={actual}"
            ),
            ResolverError::InvalidFilePath(path) => write!(f, "invalid payload path: {path}"),
            ResolverError::InvalidFileHash { path, hash } => {
                write!(f, "invalid payload hash for {path}: {hash}")
            }
            ResolverError::PayloadMissing(path) => {
                write!(f, "payload missing: {}", path.display())
            }
            ResolverError::ExtraPayload(path) => write!(f, "extra payload file: {path}"),
            ResolverError::PayloadHashMismatch(path) => {
                write!(f, "payload hash mismatch: {}", path.display())
            }
            ResolverError::PayloadSymlink(path) => {
                write!(f, "payload symlink/reparse point: {}", path.display())
            }
            ResolverError::Io(detail) => write!(f, "io error: {detail}"),
        }
    }
}

/// 以一次 `read` 获取 current.json 完整 bytes，解析并严格校验 schema。
#[derive(Debug, PartialEq, Eq)]
struct CurrentPointer {
    generation: String,
    manifest_sha256: String,
}

#[derive(Debug, PartialEq, Eq)]
struct GenerationMetadata {
    generation: String,
    manifest_sha256: String,
    files: BTreeMap<String, String>,
}

const GENERATION_PREFIX: &str = "g-";
const POINTER_KEYS: [&str; 3] = ["schema_version", "generation", "manifest_sha256"];
const GENERATION_KEYS: [&str; 4] = ["schema_version", "generation", "manifest_sha256", "files"];

fn sha256_of_bytes(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn sha256_of_file(path: &Path) -> Result<String, ResolverError> {
    let bytes = std::fs::read(path).map_err(|e| ResolverError::Io(e.to_string()))?;
    Ok(sha256_of_bytes(&bytes))
}

fn is_valid_generation_id(value: &str) -> bool {
    value.len() == 66
        && value.starts_with(GENERATION_PREFIX)
        && validate_hash(&value[2..]).is_ok()
}

fn object_keys_exact(value: &Value, expected: &[&str]) -> Result<(), ()> {
    let object = value.as_object().ok_or(())?;
    let mut keys: Vec<&str> = object.keys().map(String::as_str).collect();
    keys.sort_unstable();
    let mut expected_keys: Vec<&str> = expected.to_vec();
    expected_keys.sort_unstable();
    if keys == expected_keys {
        Ok(())
    } else {
        Err(())
    }
}

fn parse_pointer(bytes: &[u8], path: &Path) -> Result<CurrentPointer, ResolverError> {
    let value: Value = serde_json::from_slice(bytes).map_err(|_| ResolverError::PointerInvalidJson(path.to_path_buf()))?;
    if object_keys_exact(&value, &POINTER_KEYS).is_err() {
        return Err(ResolverError::PointerUnknownField(path.to_path_buf()));
    }
    let schema_version = value["schema_version"].as_u64().unwrap_or(0);
    if schema_version != 1 {
        return Err(ResolverError::PointerUnknownSchema(path.to_path_buf()));
    }
    let generation = value["generation"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    let manifest_sha256 = value["manifest_sha256"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    if !is_valid_generation_id(&generation) {
        return Err(ResolverError::PointerInvalidGeneration(path.to_path_buf()));
    }
    if validate_hash(&manifest_sha256).is_err() {
        return Err(ResolverError::PointerInvalidManifestHash(path.to_path_buf()));
    }
    Ok(CurrentPointer {
        generation,
        manifest_sha256,
    })
}

fn parse_generation_metadata(
    bytes: &[u8],
    path: &Path,
) -> Result<GenerationMetadata, ResolverError> {
    let value: Value = serde_json::from_slice(bytes)
        .map_err(|_| ResolverError::GenerationMetadataInvalidJson(path.to_path_buf()))?;
    if object_keys_exact(&value, &GENERATION_KEYS).is_err() {
        return Err(ResolverError::GenerationMetadataUnknownField(
            path.to_path_buf(),
        ));
    }
    let schema_version = value["schema_version"].as_u64().unwrap_or(0);
    if schema_version != 1 {
        return Err(ResolverError::GenerationMetadataUnknownSchema(
            path.to_path_buf(),
        ));
    }
    let generation = value["generation"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    let manifest_sha256 = value["manifest_sha256"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    let mut files = BTreeMap::new();
    if let Some(object) = value["files"].as_object() {
        for (rel, hash) in object {
            files.insert(rel.clone(), hash.as_str().unwrap_or_default().to_string());
        }
    } else {
        return Err(ResolverError::GenerationMetadataUnknownField(
            path.to_path_buf(),
        ));
    }
    Ok(GenerationMetadata {
        generation,
        manifest_sha256,
        files,
    })
}

fn relative_path(root: &Path, file: &Path) -> Result<String, ResolverError> {
    let relative = file
        .strip_prefix(root)
        .map_err(|_| ResolverError::InvalidFilePath(file.display().to_string()))?;
    Ok(relative.to_string_lossy().replace('\\', "/"))
}

/// 枚举 generation 目录 payload（排除 generation.json），拒绝 symlink/reparse 与
/// 非 regular 文件，返回 relpath -> sha256。
fn walk_generation_payload(root: &Path) -> Result<BTreeMap<String, String>, ResolverError> {
    fn visit(
        root: &Path,
        current: &Path,
        out: &mut BTreeMap<String, String>,
    ) -> Result<(), ResolverError> {
        for entry in
            std::fs::read_dir(current).map_err(|e| ResolverError::Io(e.to_string()))?
        {
            let entry = entry.map_err(|e| ResolverError::Io(e.to_string()))?;
            let entry_path = entry.path();
            let metadata = std::fs::symlink_metadata(&entry_path)
                .map_err(|e| ResolverError::Io(e.to_string()))?;
            if is_symlink_or_reparse(&metadata) {
                return Err(ResolverError::PayloadSymlink(entry_path));
            }
            if metadata.is_dir() {
                visit(root, &entry_path, out)?;
            } else if metadata.is_file() {
                out.insert(
                    relative_path(root, &entry_path)?,
                    sha256_of_file(&entry_path)?,
                );
            } else {
                return Err(ResolverError::PayloadSymlink(entry_path));
            }
        }
        Ok(())
    }
    let mut files = BTreeMap::new();
    visit(root, root, &mut files)?;
    files.remove("generation.json");
    Ok(files)
}

/// ADR-027 §3 reader 主入口。
pub fn resolve_sidecar_runtime(
    runtime_root: &Path,
    compiled_manifest_sha256: &str,
) -> Result<ResolvedRuntime, ResolverError> {
    // 1. 一次 read 获取 current.json 完整 bytes。
    let pointer_path = runtime_root.join("current.json");
    let pointer_bytes =
        std::fs::read(&pointer_path).map_err(|_| ResolverError::PointerMissing(pointer_path.clone()))?;
    let pointer = parse_pointer(&pointer_bytes, &pointer_path)?;

    // 2. 定位 generations/<id>，限制在 generation 内并拒绝 symlink/reparse。
    let generation_dir = runtime_root
        .join("generations")
        .join(&pointer.generation);
    let generation_meta = std::fs::symlink_metadata(&generation_dir)
        .map_err(|_| ResolverError::GenerationDirMissing(generation_dir.clone()))?;
    if !generation_meta.is_dir() || is_symlink_or_reparse(&generation_meta) {
        return Err(ResolverError::GenerationDirMissing(generation_dir));
    }

    // 3. 读取并校验 generation.json（严格 schema）。
    let metadata_path = generation_dir.join("generation.json");
    let metadata_bytes = std::fs::read(&metadata_path)
        .map_err(|_| ResolverError::GenerationMetadataMissing(metadata_path.clone()))?;
    let metadata = parse_generation_metadata(&metadata_bytes, &metadata_path)?;

    // 4. generation id 与 manifest digest 的 pointer/metadata 一致性。
    if metadata.generation != pointer.generation {
        return Err(ResolverError::GenerationIdMismatch {
            pointer: pointer.generation,
            metadata: metadata.generation,
        });
    }
    if metadata.manifest_sha256 != pointer.manifest_sha256 {
        return Err(ResolverError::ManifestHashMismatch {
            pointer: pointer.manifest_sha256,
            metadata: metadata.manifest_sha256,
        });
    }

    // 5. provenance 字节摘要必须与 generation id 与 manifest_sha256 一致。
    let provenance_path = generation_dir.join("jax-rtc-sidecar.provenance.json");
    let provenance_meta = std::fs::symlink_metadata(&provenance_path)
        .map_err(|_| ResolverError::ProvenanceMissing(provenance_path.clone()))?;
    if !provenance_meta.is_file() || is_symlink_or_reparse(&provenance_meta) {
        return Err(ResolverError::ProvenanceMissing(provenance_path));
    }
    let provenance_sha = sha256_of_file(&provenance_path)?;
    let expected_generation = format!("{GENERATION_PREFIX}{provenance_sha}");
    if pointer.generation != expected_generation {
        return Err(ResolverError::ProvenanceGenerationMismatch {
            generation: pointer.generation.clone(),
            actual: provenance_sha.clone(),
        });
    }
    if pointer.manifest_sha256 != provenance_sha {
        return Err(ResolverError::ProvenanceDigestMismatch {
            expected: pointer.manifest_sha256.clone(),
            actual: provenance_sha,
        });
    }

    // 6. manifest digest 必须与 build-time compiled digest 一致。
    if pointer.manifest_sha256 != compiled_manifest_sha256 {
        return Err(ResolverError::CompiledManifestMismatch {
            expected: compiled_manifest_sha256.to_string(),
            actual: pointer.manifest_sha256,
        });
    }

    // 7. 闭集 payload：路径/哈希格式 + 实际枚举与 generation.json 完全一致。
    for (rel, hash) in &metadata.files {
        validate_path(rel).map_err(|_| ResolverError::InvalidFilePath(rel.clone()))?;
        validate_hash(hash).map_err(|_| ResolverError::InvalidFileHash {
            path: rel.clone(),
            hash: hash.clone(),
        })?;
    }
    let actual = walk_generation_payload(&generation_dir)?;
    let actual_keys: BTreeSet<&String> = actual.keys().collect();
    let expected_keys: BTreeSet<&String> = metadata.files.keys().collect();
    for rel in actual_keys.difference(&expected_keys) {
        return Err(ResolverError::ExtraPayload((*rel).clone()));
    }
    for rel in expected_keys.difference(&actual_keys) {
        return Err(ResolverError::PayloadMissing(generation_dir.join(*rel)));
    }
    for (rel, hash) in &metadata.files {
        if actual.get(rel) != Some(hash) {
            return Err(ResolverError::PayloadHashMismatch(generation_dir.join(rel)));
        }
    }

    let binary_path = generation_dir.join("jax-rtc-sidecar.exe");
    let lease = GenerationLease::new(pointer.generation.clone());
    Ok(ResolvedRuntime {
        generation_root: generation_dir,
        binary_path,
        manifest_path: provenance_path,
        manifest_sha256: pointer.manifest_sha256,
        expected_hashes: metadata.files,
        lease,
    })
}
