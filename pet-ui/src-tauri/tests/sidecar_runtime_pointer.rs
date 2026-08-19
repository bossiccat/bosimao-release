//! Sidecar runtime pointer resolver 集成测试（ADR-027 §3）。
//!
//! 用临时目录构造 current.json + immutable generation 夹具，覆盖正例与全部反例：
//! malformed/truncated pointer、unknown fields、wrong generation id、
//! pointer/generation/provenance 摘要不匹配、missing/extra payload、traversal、
//! symlink/reparse，以及 snapshot 语义（resolve 后改写 current.json 不影响已解析结果）。
//!
//! 不依赖生产 fixture；每个测试用独立临时目录。

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use jax_pet::sidecar_runtime_pointer::{resolve_sidecar_runtime, ResolverError, ResolvedRuntime};
use sha2::{Digest, Sha256};

static NEXT_FIXTURE: AtomicUsize = AtomicUsize::new(0);

fn sha256_bytes(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

const VALID_HASH_A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const VALID_HASH_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

struct RuntimeFixture {
    root: PathBuf,
    runtime_root: PathBuf,
    generation: String,
    manifest_sha256: String,
    generation_dir: PathBuf,
    binary_sha256: String,
}

impl RuntimeFixture {
    fn build() -> Self {
        let base = std::env::temp_dir().join(format!(
            "sidecar-runtime-pointer-{}-{}",
            std::process::id(),
            NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
        ));
        let runtime_root = base.join("jax-rtc-sidecar-runtime");
        let provenance: Vec<u8> = br#"{"schema_version":1,"kind":"fixture-provenance"}"#.to_vec();
        let manifest_sha256 = sha256_bytes(&provenance);
        let generation = format!("g-{manifest_sha256}");
        let generation_dir = runtime_root.join("generations").join(&generation);

        std::fs::create_dir_all(generation_dir.join("resources/app/native")).expect("mkdir payload");

        // provenance bytes（generation id 与 manifest_sha256 都由此派生）
        std::fs::write(
            generation_dir.join("jax-rtc-sidecar.provenance.json"),
            &provenance,
        )
        .expect("write provenance");
        std::fs::write(
            generation_dir.join("jax-rtc-sidecar.provenance.sha256"),
            format!("{manifest_sha256}\n"),
        )
        .expect("write provenance sha");

        // binary + 独立 sha 文件
        let binary: Vec<u8> = b"MZ-fixture-sidecar-binary".to_vec();
        let binary_sha256 = sha256_bytes(&binary);
        std::fs::write(generation_dir.join("jax-rtc-sidecar.exe"), &binary).expect("write binary");
        std::fs::write(
            generation_dir.join("jax-rtc-sidecar.exe.sha256"),
            format!("{binary_sha256}\n"),
        )
        .expect("write binary sha");

        // 额外 payload
        let payload = generation_dir.join("resources/app/native/foo.txt");
        std::fs::write(&payload, b"payload-foo").expect("write payload");

        // generation.json 闭集
        let mut files = BTreeMap::new();
        files.insert(
            "jax-rtc-sidecar.exe".to_string(),
            binary_sha256.clone(),
        );
        files.insert(
            "jax-rtc-sidecar.exe.sha256".to_string(),
            sha256_bytes(format!("{binary_sha256}\n").as_bytes()),
        );
        files.insert(
            "jax-rtc-sidecar.provenance.json".to_string(),
            manifest_sha256.clone(),
        );
        files.insert(
            "jax-rtc-sidecar.provenance.sha256".to_string(),
            sha256_bytes(format!("{manifest_sha256}\n").as_bytes()),
        );
        files.insert(
            "resources/app/native/foo.txt".to_string(),
            sha256_bytes(b"payload-foo"),
        );
        write_generation_metadata(&generation_dir, &generation, &manifest_sha256, &files);
        write_current_pointer(&runtime_root, &generation, &manifest_sha256);

        Self {
            root: base,
            runtime_root,
            generation,
            manifest_sha256,
            generation_dir,
            binary_sha256,
        }
    }

    fn resolve(&self) -> Result<ResolvedRuntime, ResolverError> {
        resolve_sidecar_runtime(&self.runtime_root, &self.manifest_sha256)
    }
}

fn write_current_pointer(runtime_root: &Path, generation: &str, manifest_sha256: &str) {
    let json = serde_json::json!({
        "schema_version": 1,
        "generation": generation,
        "manifest_sha256": manifest_sha256,
    });
    std::fs::write(runtime_root.join("current.json"), serde_json::to_vec(&json).expect("ser"))
        .expect("write current.json");
}

fn write_generation_metadata(
    generation_dir: &Path,
    generation: &str,
    manifest_sha256: &str,
    files: &BTreeMap<String, String>,
) {
    let json = serde_json::json!({
        "schema_version": 1,
        "generation": generation,
        "manifest_sha256": manifest_sha256,
        "files": files,
    });
    std::fs::write(
        generation_dir.join("generation.json"),
        serde_json::to_vec(&json).expect("ser"),
    )
    .expect("write generation.json");
}

fn valid_generation_id() -> String {
    format!("g-{VALID_HASH_A}")
}

// ---- 正例 ----

#[test]
fn resolves_valid_generation_with_closed_payload() {
    let fixture = RuntimeFixture::build();
    let resolved = fixture.resolve().expect("valid fixture must resolve");
    assert_eq!(resolved.generation_root(), fixture.generation_dir);
    assert_eq!(
        resolved.binary_path(),
        fixture.generation_dir.join("jax-rtc-sidecar.exe")
    );
    assert_eq!(
        resolved.manifest_path(),
        fixture
            .generation_dir
            .join("jax-rtc-sidecar.provenance.json")
    );
    assert_eq!(resolved.manifest_sha256(), fixture.manifest_sha256);
    assert_eq!(resolved.generation(), fixture.generation);
    // 闭集：5 个 payload 文件全部记录在 expected hashes
    assert_eq!(resolved.expected_hashes().len(), 5);
    assert_eq!(
        resolved
            .expected_hashes()
            .get("jax-rtc-sidecar.exe")
            .map(String::as_str),
        Some(fixture.binary_sha256.as_str())
    );
}

#[test]
fn into_sidecar_spec_points_at_generation_dir() {
    let fixture = RuntimeFixture::build();
    let resolved = fixture.resolve().expect("valid fixture must resolve");
    let (spec, lease) = resolved.into_sidecar_spec(
        vec!["--role=sidecar".to_string()],
        PathBuf::from("certs/ca.crt"),
        Duration::from_secs(5),
        Duration::from_secs(3),
    );
    assert_eq!(lease.generation(), fixture.generation);
    assert_eq!(
        spec.binary_path,
        fixture.generation_dir.join("jax-rtc-sidecar.exe")
    );
    assert_eq!(spec.integrity.runtime_dir, fixture.generation_dir);
    assert_eq!(
        spec.integrity.manifest_path,
        fixture
            .generation_dir
            .join("jax-rtc-sidecar.provenance.json")
    );
    assert_eq!(spec.integrity.expected_manifest_sha256, fixture.manifest_sha256);
    assert_eq!(spec.expected_sha256, fixture.binary_sha256);
    assert_eq!(spec.args, vec!["--role=sidecar".to_string()]);
}

// ---- snapshot 语义 ----

#[test]
fn snapshot_survives_current_pointer_rewrite() {
    let fixture = RuntimeFixture::build();
    let resolved = fixture.resolve().expect("valid fixture must resolve");
    let generation_root = resolved.generation_root().to_path_buf();

    // 解析成功后改写 current.json：指向另一个不存在的 generation，并混入垃圾。
    std::fs::write(
        fixture.runtime_root.join("current.json"),
        format!(
            "{{\"schema_version\":1,\"generation\":\"{}\",\"manifest_sha256\":\"{}\"}}",
            valid_generation_id(),
            VALID_HASH_A
        ),
    )
    .expect("rewrite current.json");

    // 已解析结果仍指向原已验证的 immutable generation，路径与哈希不变。
    assert_eq!(resolved.generation_root(), generation_root);
    assert_eq!(resolved.generation(), fixture.generation);
    assert!(generation_root.is_dir(), "immutable generation still on disk");

    // 转换为 SidecarSpec 后仍指向原 generation（不重新解析 current.json）。
    let (spec, _lease) = resolved.into_sidecar_spec(
        vec![],
        PathBuf::from("certs/ca.crt"),
        Duration::from_secs(5),
        Duration::from_secs(3),
    );
    assert_eq!(spec.integrity.runtime_dir, fixture.generation_dir);
    assert_eq!(
        spec.binary_path,
        fixture.generation_dir.join("jax-rtc-sidecar.exe")
    );
}

// ---- pointer 反例 ----

#[test]
fn missing_current_pointer_fails_closed() {
    let fixture = RuntimeFixture::build();
    std::fs::remove_file(fixture.runtime_root.join("current.json")).expect("remove pointer");
    let error = fixture.resolve().expect_err("missing pointer must fail");
    assert!(matches!(error, ResolverError::PointerMissing(_)), "{error:?}");
}

#[test]
fn empty_current_pointer_fails_closed() {
    let fixture = RuntimeFixture::build();
    std::fs::write(fixture.runtime_root.join("current.json"), "").expect("empty pointer");
    let error = fixture.resolve().expect_err("empty pointer must fail");
    assert!(
        matches!(error, ResolverError::PointerInvalidJson(_)),
        "{error:?}"
    );
}

#[test]
fn truncated_current_pointer_fails_closed() {
    let fixture = RuntimeFixture::build();
    std::fs::write(
        fixture.runtime_root.join("current.json"),
        br#"{"schema_version":1,"generation":"#,
    )
    .expect("truncated pointer");
    let error = fixture.resolve().expect_err("truncated pointer must fail");
    assert!(
        matches!(error, ResolverError::PointerInvalidJson(_)),
        "{error:?}"
    );
}

#[test]
fn unknown_current_pointer_field_fails_closed() {
    let fixture = RuntimeFixture::build();
    let json = serde_json::json!({
        "schema_version": 1,
        "generation": fixture.generation,
        "manifest_sha256": fixture.manifest_sha256,
        "extra": "nope",
    });
    std::fs::write(
        fixture.runtime_root.join("current.json"),
        serde_json::to_vec(&json).expect("ser"),
    )
    .expect("write pointer");
    let error = fixture.resolve().expect_err("unknown field must fail");
    assert!(
        matches!(error, ResolverError::PointerUnknownField(_)),
        "{error:?}"
    );
}

#[test]
fn wrong_current_pointer_schema_fails_closed() {
    let fixture = RuntimeFixture::build();
    write_current_pointer(&fixture.runtime_root, &fixture.generation, &fixture.manifest_sha256);
    let json = serde_json::json!({
        "schema_version": 2,
        "generation": fixture.generation,
        "manifest_sha256": fixture.manifest_sha256,
    });
    std::fs::write(
        fixture.runtime_root.join("current.json"),
        serde_json::to_vec(&json).expect("ser"),
    )
    .expect("write pointer");
    let error = fixture.resolve().expect_err("wrong schema must fail");
    assert!(
        matches!(error, ResolverError::PointerUnknownSchema(_)),
        "{error:?}"
    );
}

#[test]
fn invalid_generation_id_format_fails_closed() {
    let fixture = RuntimeFixture::build();
    write_current_pointer(&fixture.runtime_root, "not-a-generation", &fixture.manifest_sha256);
    let error = fixture.resolve().expect_err("bad generation id must fail");
    assert!(
        matches!(error, ResolverError::PointerInvalidGeneration(_)),
        "{error:?}"
    );
}

#[test]
fn invalid_manifest_hash_format_fails_closed() {
    let fixture = RuntimeFixture::build();
    write_current_pointer(&fixture.runtime_root, &fixture.generation, "not-a-hash");
    let error = fixture.resolve().expect_err("bad manifest hash must fail");
    assert!(
        matches!(error, ResolverError::PointerInvalidManifestHash(_)),
        "{error:?}"
    );
}

// ---- generation 反例 ----

#[test]
fn missing_generation_dir_fails_closed() {
    let fixture = RuntimeFixture::build();
    std::fs::remove_dir_all(&fixture.generation_dir).expect("remove generation dir");
    let error = fixture.resolve().expect_err("missing generation dir must fail");
    assert!(
        matches!(error, ResolverError::GenerationDirMissing(_)),
        "{error:?}"
    );
}

#[test]
fn missing_generation_metadata_fails_closed() {
    let fixture = RuntimeFixture::build();
    std::fs::remove_file(fixture.generation_dir.join("generation.json"))
        .expect("remove generation.json");
    let error = fixture.resolve().expect_err("missing generation metadata must fail");
    assert!(
        matches!(error, ResolverError::GenerationMetadataMissing(_)),
        "{error:?}"
    );
}

#[test]
fn unknown_generation_metadata_field_fails_closed() {
    let fixture = RuntimeFixture::build();
    let mut files = BTreeMap::new();
    files.insert("x".to_string(), VALID_HASH_A.to_string());
    let json = serde_json::json!({
        "schema_version": 1,
        "generation": fixture.generation,
        "manifest_sha256": fixture.manifest_sha256,
        "files": files,
        "extra": "nope",
    });
    std::fs::write(
        fixture.generation_dir.join("generation.json"),
        serde_json::to_vec(&json).expect("ser"),
    )
    .expect("write generation.json");
    let error = fixture.resolve().expect_err("unknown metadata field must fail");
    assert!(
        matches!(error, ResolverError::GenerationMetadataUnknownField(_)),
        "{error:?}"
    );
}

#[test]
fn generation_id_mismatch_fails_closed() {
    let fixture = RuntimeFixture::build();
    // generation.json 的 generation 与 pointer 不一致（都是合法格式）。
    let other = valid_generation_id();
    let mut files = BTreeMap::new();
    files.insert("x".to_string(), VALID_HASH_A.to_string());
    write_generation_metadata(&fixture.generation_dir, &other, &fixture.manifest_sha256, &files);
    let error = fixture.resolve().expect_err("generation id mismatch must fail");
    assert!(
        matches!(error, ResolverError::GenerationIdMismatch { .. }),
        "{error:?}"
    );
}

#[test]
fn manifest_hash_mismatch_fails_closed() {
    let fixture = RuntimeFixture::build();
    // generation.json 的 manifest_sha256 与 pointer 不一致（都是合法 hash）。
    let mut files = BTreeMap::new();
    files.insert("x".to_string(), VALID_HASH_A.to_string());
    write_generation_metadata(&fixture.generation_dir, &fixture.generation, VALID_HASH_B, &files);
    let error = fixture.resolve().expect_err("manifest hash mismatch must fail");
    assert!(
        matches!(error, ResolverError::ManifestHashMismatch { .. }),
        "{error:?}"
    );
}

// ---- provenance 摘要反例 ----

#[test]
fn missing_provenance_fails_closed() {
    let fixture = RuntimeFixture::build();
    std::fs::remove_file(fixture.generation_dir.join("jax-rtc-sidecar.provenance.json"))
        .expect("remove provenance");
    let error = fixture.resolve().expect_err("missing provenance must fail");
    assert!(matches!(error, ResolverError::ProvenanceMissing(_)), "{error:?}");
}

#[test]
fn provenance_generation_mismatch_fails_closed() {
    let fixture = RuntimeFixture::build();
    // 篡改 provenance 字节，使 generation id 不再等于 g-<sha256(provenance)>。
    std::fs::write(
        fixture.generation_dir.join("jax-rtc-sidecar.provenance.json"),
        b"tampered-provenance",
    )
    .expect("tamper provenance");
    let error = fixture.resolve().expect_err("provenance generation mismatch must fail");
    assert!(
        matches!(error, ResolverError::ProvenanceGenerationMismatch { .. }),
        "{error:?}"
    );
}

#[test]
fn provenance_digest_mismatch_fails_closed() {
    // generation 与 provenance 派生一致，但 pointer/generation 的 manifest_sha256
    // 指向另一个合法 hash，破坏 "manifest_sha256 == sha256(provenance)"。
    let fixture = RuntimeFixture::build();
    let mut files = BTreeMap::new();
    files.insert("x".to_string(), VALID_HASH_A.to_string());
    write_generation_metadata(&fixture.generation_dir, &fixture.generation, VALID_HASH_B, &files);
    write_current_pointer(&fixture.runtime_root, &fixture.generation, VALID_HASH_B);
    let error = fixture.resolve().expect_err("provenance digest mismatch must fail");
    assert!(
        matches!(error, ResolverError::ProvenanceDigestMismatch { .. }),
        "{error:?}"
    );
}

#[test]
fn compiled_manifest_mismatch_fails_closed() {
    let fixture = RuntimeFixture::build();
    // pointer/generation/provenance 全部自洽，但 build-time compiled digest 不匹配。
    let error = resolve_sidecar_runtime(&fixture.runtime_root, VALID_HASH_B)
        .expect_err("compiled manifest mismatch must fail");
    assert!(
        matches!(error, ResolverError::CompiledManifestMismatch { .. }),
        "{error:?}"
    );
}

// ---- payload 闭集反例 ----

#[test]
fn missing_payload_fails_closed() {
    let fixture = RuntimeFixture::build();
    // 磁盘上删掉 foo.txt，但 generation.json 的 files 仍声明它。
    std::fs::remove_file(fixture.generation_dir.join("resources/app/native/foo.txt"))
        .expect("remove payload");
    let error = fixture.resolve().expect_err("missing payload must fail");
    assert!(matches!(error, ResolverError::PayloadMissing(_)), "{error:?}");
}

#[test]
fn extra_payload_fails_closed() {
    let fixture = RuntimeFixture::build();
    // 磁盘上多出一个 files 未声明的文件。
    std::fs::write(fixture.generation_dir.join("unexpected.dll"), b"extra")
        .expect("write extra file");
    let error = fixture.resolve().expect_err("extra payload must fail");
    assert!(matches!(error, ResolverError::ExtraPayload(_)), "{error:?}");
}

#[test]
fn payload_hash_mismatch_fails_closed() {
    let fixture = RuntimeFixture::build();
    // 篡改 foo.txt 内容但保留 files 里的旧 hash。
    std::fs::write(
        fixture.generation_dir.join("resources/app/native/foo.txt"),
        b"tampered-payload",
    )
    .expect("tamper payload");
    let error = fixture.resolve().expect_err("payload hash mismatch must fail");
    assert!(
        matches!(error, ResolverError::PayloadHashMismatch(_)),
        "{error:?}"
    );
}

#[test]
fn traversal_path_in_files_fails_closed() {
    let fixture = RuntimeFixture::build();
    let mut files = read_files_map(&fixture.generation_dir);
    files.insert("../escape.txt".to_string(), VALID_HASH_A.to_string());
    write_generation_metadata(&fixture.generation_dir, &fixture.generation, &fixture.manifest_sha256, &files);
    let error = fixture.resolve().expect_err("traversal path must fail");
    assert!(matches!(error, ResolverError::InvalidFilePath(_)), "{error:?}");
}

#[test]
fn absolute_path_in_files_fails_closed() {
    let fixture = RuntimeFixture::build();
    let mut files = read_files_map(&fixture.generation_dir);
    files.insert("/escape.txt".to_string(), VALID_HASH_A.to_string());
    write_generation_metadata(&fixture.generation_dir, &fixture.generation, &fixture.manifest_sha256, &files);
    let error = fixture.resolve().expect_err("absolute path must fail");
    assert!(matches!(error, ResolverError::InvalidFilePath(_)), "{error:?}");
}

#[test]
fn symlink_payload_fails_closed() {
    let fixture = RuntimeFixture::build();
    let outside = fixture.root.join("outside.dll");
    std::fs::write(&outside, b"outside").expect("write outside file");
    let link = fixture
        .generation_dir
        .join("resources/app/native/linked.dll");
    #[cfg(unix)]
    std::os::unix::fs::symlink(&outside, &link).expect("create symlink");
    #[cfg(windows)]
    std::os::windows::fs::symlink_file(&outside, &link).expect("create symlink");
    // 把 symlink 也登记进 files，确保拒绝原因是 symlink 而非 extra。
    let mut files = read_files_map(&fixture.generation_dir);
    files.insert(
        "resources/app/native/linked.dll".to_string(),
        VALID_HASH_A.to_string(),
    );
    write_generation_metadata(&fixture.generation_dir, &fixture.generation, &fixture.manifest_sha256, &files);
    let error = fixture.resolve().expect_err("symlink payload must fail");
    assert!(matches!(error, ResolverError::PayloadSymlink(_)), "{error:?}");
}

fn read_files_map(generation_dir: &Path) -> BTreeMap<String, String> {
    let bytes = std::fs::read(generation_dir.join("generation.json")).expect("read generation.json");
    let value: serde_json::Value = serde_json::from_slice(&bytes).expect("parse generation.json");
    value
        .get("files")
        .and_then(|f| f.as_object())
        .expect("files object")
        .iter()
        .map(|(k, v)| (k.clone(), v.as_str().expect("hash string").to_string()))
        .collect()
}
