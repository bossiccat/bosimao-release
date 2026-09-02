//! RP-07 P0（2026-09-02）：provenance runtime_files 混入 debug.log 的完整性豁免。
//!
//! debug.log 是 Chromium 在 CWD（= generation 目录）写的再生产物
//!（registration_protocol_win.cc 等内部诊断，不受 JAX_SIDECAR_LOG_DIR 控制）。
//! staging 捕获时它可能被写进 provenance 的 runtime_files（v4m 安装实测
//! 3830 项含 debug.log），但安装侧既不保证其存在、也不保证内容一致。
//! actual 侧（list_runtime_files）已豁免；本组测试锁定 expected 侧
//!（validate_runtime_entries）同样豁免——否则首次 spawn 前完整性门即熔断
//!（RuntimeSetMismatch / RuntimeHashMismatch），watchdog 进入 fused。

mod support;

use std::path::PathBuf;
use std::time::Duration;

use jax_pet::sidecar::{SidecarSpec, SidecarSupervisor};
use sha2::{Digest, Sha256};

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

/// 向 fixture 的 provenance manifest 注入 debug.log 条目并重算 manifest digest
///（否则先熔断在 ManifestDigestMismatch，够不到目标路径）。
/// `on_disk = Some(content)` 同时在 runtime 目录写该文件；
/// `None` 表示 provenance 声明了它但磁盘缺失（v4m 安装后的实测状态）。
fn declare_debug_log(
    mut fixture: support::SidecarFixture,
    on_disk: Option<&[u8]>,
) -> support::SidecarFixture {
    let manifest_path = fixture.integrity.manifest_path.clone();
    let mut manifest: serde_json::Value = serde_json::from_slice(
        &std::fs::read(&manifest_path).expect("read manifest"),
    )
    .expect("parse manifest");
    let sha256 = match on_disk {
        Some(content) => {
            std::fs::write(fixture.integrity.runtime_dir.join("debug.log"), content)
                .expect("write debug.log");
            sha256_hex(content)
        }
        None => sha256_hex(&[]),
    };
    manifest["runtime_files"]
        .as_array_mut()
        .expect("runtime_files array")
        .push(serde_json::json!({ "path": "debug.log", "sha256": sha256 }));
    std::fs::write(
        &manifest_path,
        serde_json::to_vec(&manifest).expect("serialize manifest"),
    )
    .expect("rewrite manifest");
    fixture.integrity.expected_manifest_sha256 =
        sha256_hex(&std::fs::read(&manifest_path).expect("reread manifest"));
    fixture
}

fn spec_from(fixture: support::SidecarFixture) -> SidecarSpec {
    SidecarSpec {
        binary_path: fixture.binary_path.clone(),
        expected_sha256: sha256_hex(&std::fs::read(&fixture.binary_path).expect("read binary")),
        integrity: fixture.integrity,
        args: vec![],
        // TLS 信任锚路径（ADR-020 A1）：stub 测试不读该文件，用占位路径即可。
        ca_cert_path: PathBuf::from("certs/ca.crt"),
        graceful_timeout: Duration::from_secs(5),
        kill_timeout: Duration::from_secs(10),
    }
}

// ---- 场景 A（v4m 实测熔断路径）：声明 + 在盘 + hash 自洽 → RuntimeSetMismatch ----

#[test]
fn validate_passes_when_debug_log_declared_and_present_with_matching_hash() {
    let fixture = support::sidecar_fixture();
    let fixture = declare_debug_log(fixture, Some(b"chromium registration debug\n"));
    let mut sup = SidecarSupervisor::new(spec_from(fixture));
    sup.validate_binary()
        .expect("debug.log 声明且 hash 自洽必须通过完整性门（v4m 实测 RuntimeSetMismatch）");
}

// ---- 场景 B（安装后实测状态）：声明 + 磁盘缺失 → RuntimeHashMismatch ----

#[test]
fn validate_passes_when_debug_log_declared_but_missing_on_disk() {
    let fixture = support::sidecar_fixture();
    let fixture = declare_debug_log(fixture, None);
    let mut sup = SidecarSupervisor::new(spec_from(fixture));
    sup.validate_binary()
        .expect("debug.log 声明但磁盘缺失必须通过完整性门（可再生运行时产物）");
}

// ---- 场景 C（运行期重写）：声明 hash 与磁盘内容漂移 → RuntimeHashMismatch ----

#[test]
fn validate_ignores_debug_log_content_drift() {
    let fixture = support::sidecar_fixture();
    let fixture = declare_debug_log(fixture, Some(b"declared content\n"));
    // Chromium 每次启动重写 debug.log → 与声明 hash 不一致；actual 侧从不
    // 对其做 hash 校验，expected 侧同样不得校验（否则每次 spawn 都熔断）。
    std::fs::write(fixture.integrity.runtime_dir.join("debug.log"), b"rewritten\n")
        .expect("rewrite debug.log");
    let mut sup = SidecarSupervisor::new(spec_from(fixture));
    sup.validate_binary()
        .expect("debug.log 内容漂移不参与完整性闭集");
}
