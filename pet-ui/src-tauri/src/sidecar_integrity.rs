use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};

use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::sidecar::{IntegritySpec, SidecarError, SidecarSpec};
use crate::sidecar_runtime_trust::validate_runtime_trust;

#[derive(Clone, Deserialize)]
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
    // 生产可信门的策略版本（对应 `scripts/lib/sidecar-trust.js` 的 TRUST_VERSION）。
    //
    // 为什么是 `Option`：本机 current-installed 的两个 generation 是 2026-09-05
    // 构建的，manifest 里没有这个键。用 `String` 除了 `deny_unknown_fields` 之外
    // 还会因为"缺字段"反序列化失败 ⇒ 每台机器上 sidecar 都拒绝 spawn。
    //
    // `deny_unknown_fields` 保持不变：未知键依旧一律拒绝，只有这一个具名键可选。
    trust_version: Option<String>,
    target_triple: String,
    electron_version: String,
    trtc_sdk_version: String,
    sidecar_package_lock_sha256: String,
    external_bin: ExternalBinManifest,
    native_files: Vec<RuntimeFile>,
    runtime_files: Vec<RuntimeFile>,
    bundle_resources: BTreeMap<String, String>,
}

/// 生产可信门的策略版本。必须与 `scripts/lib/sidecar-trust.js` 的 `TRUST_VERSION`
/// 一致 —— `scripts/test/sidecar-package.test.js` 的
/// "the native closed set is one set across every production copy" 同族不变式锁
/// 会同时读这两处（跨语言），任一侧单独改动即变红。
const TRUST_VERSION: &str = "1.0.0";

/// 「版本化之前」的基线版本号：2026-09-05 构建的 generation 的 manifest 里
/// 没有 `trust_version` 键，它们是在策略版本恰为 1.0.0 时构建的。
/// 一旦 TRUST_VERSION 被 bump，缺键的旧 generation 会立刻失配 ⇒ 强制重建；
/// 这是刻意保留、有到期条件的历史基线，不是"缺字段就放行"。
const PRE_VERSIONING_TRUST_VERSION: &str = "1.0.0";

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
    // RP-07 P0（2026-09-02）：debug.log 是 Chromium 在 CWD（= generation 目录）
    // 写的再生产物（registration_protocol_win.cc 等内部诊断，不受
    // JAX_SIDECAR_LOG_DIR 控制）。staging 捕获时它可能被写进 provenance 的
    // runtime_files（v4m 安装实测 3830 项含 debug.log），但安装侧既不保证其
    // 存在、也不保证内容一致。actual 侧已在 list_runtime_files 豁免；此处
    // expected 侧同步豁免，否则首次 spawn 前完整性门即熔断
    // （RuntimeSetMismatch / RuntimeHashMismatch）→ watchdog fused。
    // 豁免不弱化安全边界：manifest 本身受 ManifestDigestMismatch 保护，且
    // actual 侧从不对其做 hash 校验（两侧若不对偶只会制造假阳性熔断）。
    let declared: Vec<RuntimeFile> = manifest
        .runtime_files
        .iter()
        .filter(|file| file.path != "debug.log")
        .cloned()
        .collect();
    let runtime_by_path = validate_runtime_entries(&declared, runtime_dir)?;
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

/// 策略版本判定，与 `scripts/lib/sidecar-trust.js` 里
/// `assertProductionTrust` 的比对同语义：缺键 ⇒ 视为版本化之前的基线。
/// 抽成独立函数是为了让它能被单元测试直接钉住，而不必构造完整的 `SidecarSpec`。
fn trust_version_acceptable(declared: Option<&str>) -> bool {
    declared.unwrap_or(PRE_VERSIONING_TRUST_VERSION) == TRUST_VERSION
}

fn validate_metadata(
    manifest: &ProvenanceManifest,
    spec: &SidecarSpec,
) -> Result<(), SidecarError> {
    if manifest.schema_version != 1
        // 策略版本比对：把"策略变了"变成机械后果 —— 按旧策略构建的 generation
        // 即使仍躺在磁盘上、pointer 也仍指向它，也会在这里被拒。
        // 缺键 ⇒ 视为版本化之前的基线（见 PRE_VERSIONING_TRUST_VERSION）。
        || !trust_version_acceptable(manifest.trust_version.as_deref())
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
            != Some("jrt/")
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
        // RP-07 补充（2026-09-02）：Chromium 自身会在 CWD（= generation 目录）写
        // debug.log（registration_protocol_win.cc 等内部诊断），不受
        // JAX_SIDECAR_LOG_DIR 控制。它与 logs/ 同属可再生运行时产物，不豁免会使
        // 首次运行后每次 spawn 都 RuntimeSetMismatch → watchdog 熔断（本机实测）。
        Some("debug.log"),
    ]
    .into_iter()
    .flatten()
    {
        files.remove(excluded);
    }
    // RP-07 P0 兜底（2026-09-01）：sidecar 运行期在 runtime_dir 下生成的 logs/
    //（logger.js / phone.js 诊断产物）不属于 provenance 闭集；主修复是日志
    // 重定向（JAX_SIDECAR_LOG_DIR），此处排除可再生运行时产物防止
    // RuntimeSetMismatch。仅豁免顶层 logs/ 前缀。
    files.retain(|relative| !relative.starts_with("logs/"));
    Ok(files)
}

fn normalized_relative(root: &Path, file: PathBuf) -> Result<String, SidecarError> {
    Ok(file
        .strip_prefix(root)
        .map_err(|_| SidecarError::ManifestInvalid)?
        .to_string_lossy()
        .replace('\\', "/"))
}

// 2026-09-19：`ProvenanceManifest` 此前**零测试覆盖** —— 它是 `deny_unknown_fields`
// 的生产启动路径解析点，改错一个字段名的后果是全量 sidecar 拒绝 spawn，
// 而构建侧的 `--verify-only`（纯 Node）完全测不到。下面这组用例把三件事钉住：
//   1. 带 `trust_version` 的 manifest 能解析，且缺键形态（旧 generation）也能解析；
//   2. 策略版本判定在 bump 前后两种形态下都正确；
//   3. `deny_unknown_fields` **没有**被放宽成"可选键 = 爱加什么加什么"。
#[cfg(test)]
mod provenance_manifest_tests {
    use super::*;
    use serde_json::json;

    fn native_entries() -> Vec<serde_json::Value> {
        [
            "resources/app/node_modules/trtc-electron-sdk/build/Release/trtc_electron_sdk.node",
            "resources/app/node_modules/trtc-electron-sdk/build/Release/liteav.dll",
            "resources/app/node_modules/trtc-electron-sdk/build/Release/txffmpeg.dll",
            "resources/app/node_modules/trtc-electron-sdk/build/Release/txsoundtouch.dll",
            "resources/app/node_modules/trtc-electron-sdk/build/Release/liteav_media_server.exe",
        ]
        .iter()
        .map(|path| json!({ "path": path, "sha256": "a".repeat(64) }))
        .collect()
    }

    /// 一份形状完整的 manifest（键集合与 `createProvenance` 的输出一致）。
    /// `extra` 用来注入或替换单个顶层键。
    fn manifest(extra: Option<(&str, serde_json::Value)>) -> String {
        let mut map = serde_json::Map::new();
        map.insert("schema_version".into(), json!(1));
        map.insert("build_script_version".into(), json!("1.0.0"));
        map.insert("target_triple".into(), json!("x86_64-pc-windows-msvc"));
        map.insert("electron_version".into(), json!("31.7.7"));
        map.insert("trtc_sdk_version".into(), json!("13.4.802-beta.3"));
        map.insert(
            "sidecar_package_lock_sha256".into(),
            json!("b".repeat(64)),
        );
        map.insert(
            "external_bin".into(),
            json!({
                "build_input_file": "jax-rtc-sidecar-x86_64-pc-windows-msvc.exe",
                "installed_file": "jax-rtc-sidecar.exe",
                "target_triple": "x86_64-pc-windows-msvc",
                "sha256": "c".repeat(64),
            }),
        );
        map.insert("native_files".into(), json!(native_entries()));
        map.insert("runtime_files".into(), json!(native_entries()));
        map.insert(
            "bundle_resources".into(),
            json!({ "binaries/jax-rtc-sidecar-runtime/": "jrt/" }),
        );
        if let Some((key, value)) = extra {
            map.insert(key.to_string(), value);
        }
        serde_json::Value::Object(map).to_string()
    }

    fn parse(text: &str) -> Result<ProvenanceManifest, serde_json::Error> {
        serde_json::from_str(text)
    }

    #[test]
    fn accepts_a_manifest_carrying_the_current_trust_version() {
        let parsed = parse(&manifest(Some(("trust_version", json!(TRUST_VERSION)))))
            .expect("带 trust_version 的 manifest 必须能解析");
        assert_eq!(parsed.trust_version.as_deref(), Some(TRUST_VERSION));
        assert!(trust_version_acceptable(parsed.trust_version.as_deref()));
    }

    #[test]
    fn accepts_a_pre_versioning_manifest_without_the_key() {
        // 本机 current-installed 的两个 generation 就是这种形态（2026-09-05 构建）。
        let parsed = parse(&manifest(None)).expect("缺 trust_version 的旧 manifest 必须仍能解析");
        assert_eq!(parsed.trust_version, None);
        assert!(trust_version_acceptable(parsed.trust_version.as_deref()));
    }

    #[test]
    fn the_absent_key_judgement_expires_when_the_policy_version_is_bumped() {
        // 自适配断言：只要"基线 == 当前策略版本"成立，缺键就放行；
        // 一旦 bump，缺键必须立刻失配 ⇒ 旧 generation 强制重建。
        assert_eq!(
            trust_version_acceptable(None),
            PRE_VERSIONING_TRUST_VERSION == TRUST_VERSION,
            "缺键的判定必须等价于「基线与当前策略版本相同」——bump 之后必须失配",
        );
    }

    #[test]
    fn rejects_a_stale_declared_trust_version() {
        let parsed = parse(&manifest(Some(("trust_version", json!("0.9.0")))))
            .expect("非空字符串在解析层是合法的，只应被策略判定拒绝");
        assert!(!trust_version_acceptable(parsed.trust_version.as_deref()));
    }

    #[test]
    fn a_wrongly_typed_trust_version_fails_deserialization() {
        assert!(parse(&manifest(Some(("trust_version", json!(123))))).is_err());
    }

    #[test]
    fn deny_unknown_fields_is_not_relaxed() {
        // "可选键"只对 trust_version 这一个具名键开口，不是放宽成任意键。
        assert!(parse(&manifest(Some(("untrusted_extension", json!(true))))).is_err());
    }
}
