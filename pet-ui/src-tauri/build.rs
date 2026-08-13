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

fn emit_rerun_rules(manifest_dir: &Path) {
    for relative in [
        "../../scripts/build-sidecar-external-bin.js",
        "../../scripts/lib/sidecar-package.js",
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
        "binaries/jax-rtc-sidecar-x86_64-pc-windows-msvc.exe",
        "binaries/jax-rtc-sidecar-runtime",
        "binaries/jax-rtc-sidecar-runtime/jax-rtc-sidecar.exe.sha256",
        "binaries/jax-rtc-sidecar-runtime/jax-rtc-sidecar.provenance.json",
        "binaries/jax-rtc-sidecar-runtime/jax-rtc-sidecar.provenance.sha256",
    ] {
        println!(
            "cargo:rerun-if-changed={}",
            manifest_dir.join(relative).display()
        );
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
    let manifest =
        manifest_dir.join("binaries/jax-rtc-sidecar-runtime/jax-rtc-sidecar.provenance.json");
    println!(
        "cargo:rustc-env=JAX_SIDECAR_MANIFEST_SHA256={}",
        manifest_digest(&manifest)
    );
}

fn main() {
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").expect("manifest dir"));
    emit_rerun_rules(&manifest_dir);
    verify_sidecar_for_release(&manifest_dir);
    tauri_build::build()
}
