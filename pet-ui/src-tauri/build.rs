use std::path::{Path, PathBuf};
use std::process::Command;

use sha2::{Digest, Sha256};

const WATCHED_ENV: [&str; 8] = [
    "NODE",
    "NPM_CONFIG_REGISTRY",
    "NPM_CONFIG_CAFILE",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
];

const RUNTIME_DIR: &str = "binaries/jax-rtc-sidecar-runtime";

fn runtime_root(manifest_dir: &Path) -> PathBuf {
    manifest_dir.join(RUNTIME_DIR)
}

/// 从 current.json 提取 generation id（`g-<64 lowercase hex>`）；缺失或格式非法返回 `None`。
/// build.rs 不能依赖 serde_json（build-dependencies 仅 sha2），故用最小字符串字段提取，
/// 并对结果做严格 lower-case hex 校验后 fail closed。
fn json_string_field(text: &str, field: &str) -> Option<String> {
    let needle = format!("\"{field}\"");
    let start = text.find(&needle)?;
    let rest = &text[start + needle.len()..];
    let colon = rest.find(':')?;
    let rest = rest[colon + 1..].trim_start();
    let rest = rest.strip_prefix('"')?;
    let end = rest.find('"')?;
    Some(rest[..end].to_string())
}

fn current_generation(manifest_dir: &Path) -> Option<String> {
    let pointer_path = runtime_root(manifest_dir).join("current.json");
    let bytes = std::fs::read(pointer_path).ok()?;
    let text = String::from_utf8_lossy(&bytes);
    let generation = json_string_field(&text, "generation")?;
    if generation.len() == 66
        && generation.starts_with("g-")
        && generation[2..]
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        Some(generation)
    } else {
        None
    }
}

fn emit_rerun_rules(manifest_dir: &Path) {
    for relative in [
        "../../scripts/build-sidecar-external-bin.js",
        "../../scripts/lib/sidecar-package.js",
        "../../scripts/lib/sidecar-package-build.js",
        "../../scripts/lib/sidecar-runtime-publish.js",
        "../../scripts/lib/sidecar-trust.js",
        "../../sidecar/package.json",
        "../../sidecar/package-lock.json",
        "../../sidecar/audio.js",
        "../../sidecar/bridge.js",
        "../../sidecar/config.js",
        "../../sidecar/exit-protocol.js",
        "../../sidecar/index.html",
        "../../sidecar/logger.js",
        "../../sidecar/main.js",
        "../../sidecar/phone.js",
        "../../sidecar/rtc-startup.js",
        "../../sidecar/rtc.js",
        "../../sidecar/security.js",
        // TLS 信任锚（ADR-020 A1/A2）：打包进 resource_dir/certs/ca.crt 的
        // 自签 CA 公钥，是从项目根 certs/ca.crt 拷贝的分发副本（换 CA 时同步替换）。
        "certs/ca.crt",
        // externalBin 构建输入（Tauri bundling 需要）。
        "binaries/jax-rtc-sidecar-x86_64-pc-windows-msvc.exe",
        // ADR-027：pointer 与 generations 根目录（新 generation 加入时触发重跑）。
        "binaries/jax-rtc-sidecar-runtime/current.json",
        "binaries/jax-rtc-sidecar-runtime/generations",
    ] {
        println!(
            "cargo:rerun-if-changed={}",
            manifest_dir.join(relative).display()
        );
    }
    // selected generation 的 manifest/hash/payload 输入（generation id 由 pointer 动态决定）。
    if let Some(generation) = current_generation(manifest_dir) {
        let gen_dir = runtime_root(manifest_dir).join("generations").join(&generation);
        println!("cargo:rerun-if-changed={}", gen_dir.display());
        for file in [
            "generation.json",
            "jax-rtc-sidecar.exe",
            "jax-rtc-sidecar.exe.sha256",
            "jax-rtc-sidecar.provenance.json",
            "jax-rtc-sidecar.provenance.sha256",
        ] {
            println!("cargo:rerun-if-changed={}", gen_dir.join(file).display());
        }
    }
    for name in WATCHED_ENV {
        println!("cargo:rerun-if-env-changed={name}");
    }
}

fn manifest_digest(manifest: &Path) -> String {
    let bytes = std::fs::read(manifest).expect("read verified sidecar provenance manifest");
    format!("{:x}", Sha256::digest(bytes))
}

fn verify_sidecar_for_release(manifest_dir: &Path) {
    if std::env::var("PROFILE").as_deref() != Ok("release") {
        println!(
            "cargo:rustc-env=JAX_SIDECAR_MANIFEST_SHA256={}",
            "0".repeat(64)
        );
        return;
    }

    let verifier = manifest_dir.join("../../scripts/build-sidecar-external-bin.js");
    let node = std::env::var_os("NODE").unwrap_or_else(|| "node".into());
    let status = Command::new(node)
        .arg(verifier)
        .arg("--verify-only")
        .status()
        .expect("failed to launch sidecar package verifier");
    if !status.success() {
        panic!("sidecar package verification failed");
    }
    // ADR-027 §7：编译进二进制的 manifest SHA 来自 current.json 所选 generation
    // 的实际 provenance bytes，而非废弃的 flat root-level 清单路径。
    let generation =
        current_generation(manifest_dir).expect("sidecar current pointer missing or invalid");
    let provenance = runtime_root(manifest_dir)
        .join("generations")
        .join(generation)
        .join("jax-rtc-sidecar.provenance.json");
    println!(
        "cargo:rustc-env=JAX_SIDECAR_MANIFEST_SHA256={}",
        manifest_digest(&provenance)
    );
}

fn main() {
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").expect("manifest dir"));
    emit_rerun_rules(&manifest_dir);
    verify_sidecar_for_release(&manifest_dir);
    tauri_build::build()
}
