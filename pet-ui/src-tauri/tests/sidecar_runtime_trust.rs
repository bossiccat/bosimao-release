//! Tauri sidecar 生产可信门集成测试：hash 自洽不等于生产可信。
//!
//! 与 `scripts/lib/sidecar-trust.js` 同一策略：externalBin >= 4MB 且 PE `MZ`
//! 头；native 五个 >= 32KB 且 `MZ` 头；Electron 六个关键文件达到各自最小体积。
//! 即使攻击者重算全部 hash（整包替换微型 runtime），启动门也必须拒绝。

mod support;

use std::time::Duration;

use jax_pet::sidecar::{SidecarError, SidecarSpec, SidecarSupervisor};
use sha2::{Digest, Sha256};

fn sha256_of(path: &std::path::Path) -> String {
    let bytes = std::fs::read(path).expect("read binary");
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    format!("{:x}", hasher.finalize())
}

fn supervisor(fixture: support::SidecarFixture) -> SidecarSupervisor {
    SidecarSupervisor::new(SidecarSpec {
        binary_path: fixture.binary_path.clone(),
        expected_sha256: sha256_of(&fixture.binary_path),
        integrity: fixture.integrity,
        args: vec![],
        graceful_timeout: Duration::from_secs(1),
        kill_timeout: Duration::from_secs(1),
    })
}

#[test]
fn tiny_native_runtime_fails_closed_despite_self_consistent_hashes() {
    // 微型 native（10-23 bytes，hash 完全自洽）必须被生产可信门拒绝，
    // 即使 manifest/digest/externalBin 校验全部通过。
    let fixture = support::sidecar_fixture_tiny_native();
    let error = supervisor(fixture)
        .validate_binary()
        .expect_err("tiny native runtime must be rejected");
    assert!(
        matches!(error, SidecarError::RuntimeUntrusted),
        "expected RuntimeUntrusted, got {error:?}"
    );
}

#[test]
fn real_size_pe_closed_set_passes_runtime_trust() {
    // 真实尺寸 PE 闭集：binary >= 4MB 可执行 PE、native >= 32KB 且 MZ、
    // Electron 六个文件达到最小体积，必须通过完整校验。
    let fixture = support::sidecar_fixture();
    supervisor(fixture)
        .validate_binary()
        .expect("trusted runtime passes");
}

#[test]
fn shrunken_electron_runtime_file_fails_closed() {
    // 把 resources.pak 缩为微型文件并重算 manifest/digest（hash 自洽），
    // 启动门必须仍拒绝。
    let fixture = support::sidecar_fixture();
    let pak = fixture.integrity.runtime_dir.join("resources.pak");
    std::fs::write(&pak, "tiny-pak").expect("shrink runtime file");
    let runtime_dir = fixture.integrity.runtime_dir.clone();
    let manifest_path = fixture.integrity.manifest_path.clone();
    let manifest = std::fs::read(&manifest_path).expect("read manifest");
    let mut value: serde_json::Value = serde_json::from_slice(&manifest).expect("parse manifest");
    let runtime_files = value
        .get_mut("runtime_files")
        .and_then(|v| v.as_array_mut())
        .expect("runtime_files array");
    for entry in runtime_files.iter_mut() {
        if entry.get("path").and_then(|p| p.as_str()) == Some("resources.pak") {
            entry["sha256"] = serde_json::json!(sha256_of(&pak));
        }
    }
    std::fs::write(
        &manifest_path,
        serde_json::to_vec(&value).expect("serialize"),
    )
    .expect("rewrite manifest");
    let spec = SidecarSpec {
        binary_path: fixture.binary_path.clone(),
        expected_sha256: sha256_of(&fixture.binary_path),
        integrity: jax_pet::sidecar::IntegritySpec {
            expected_manifest_sha256: sha256_of(&manifest_path),
            manifest_path,
            runtime_dir,
        },
        args: vec![],
        graceful_timeout: Duration::from_secs(1),
        kill_timeout: Duration::from_secs(1),
    };
    let error = SidecarSupervisor::new(spec)
        .validate_binary()
        .expect_err("shrunken electron runtime file must be rejected");
    assert!(
        matches!(error, SidecarError::RuntimeUntrusted),
        "expected RuntimeUntrusted, got {error:?}"
    );
}
