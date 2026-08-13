use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};

use jax_pet::sidecar::IntegritySpec;
use sha2::{Digest, Sha256};

static NEXT_FIXTURE: AtomicUsize = AtomicUsize::new(0);

/// Production trust policy 的保守下限，与 `scripts/lib/sidecar-trust.js` 保持一致。
const MIN_BINARY_BYTES: usize = 4 * 1024 * 1024;
const MIN_NATIVE_BYTES: usize = 32 * 1024;
const NATIVE_NAMES: [&str; 5] = [
    "trtc_electron_sdk.node",
    "liteav.dll",
    "txffmpeg.dll",
    "txsoundtouch.dll",
    "liteav_media_server.exe",
];
const ELECTRON_FILES: [(&str, usize); 5] = [
    ("ffmpeg.dll", 512 * 1024),
    ("resources.pak", 512 * 1024),
    ("icudtl.dat", 512 * 1024),
    ("v8_context_snapshot.bin", 64 * 1024),
    ("locales/en-US.pak", 32 * 1024),
];

pub struct SidecarFixture {
    pub binary_path: PathBuf,
    pub integrity: IntegritySpec,
}

/// 真实尺寸 PE 闭集 fixture：binary 为可执行 PE 且 >= 4MB，native 五个为
/// >= 32KB 且带 MZ 头的 PE 风格文件，Electron 六个文件达到各自最小体积。
pub fn sidecar_fixture() -> SidecarFixture {
    build_fixture(false)
}

/// 微型 native 变体：native 五个被写为微型文本（10-23 bytes），用于证明
/// 生产可信门拒绝 hash 自洽的微型 runtime。
pub fn sidecar_fixture_tiny_native() -> SidecarFixture {
    build_fixture(true)
}

fn build_fixture(tiny_native: bool) -> SidecarFixture {
    let root = std::env::temp_dir().join(format!(
        "jax-sidecar-integrity-{}-{}",
        std::process::id(),
        NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
    ));
    let runtime_dir = root.join("jax-rtc-sidecar-runtime");
    let binary_path = root.join("jax-rtc-sidecar.exe");
    std::fs::create_dir_all(runtime_dir.join("locales")).expect("create runtime locales");
    std::fs::create_dir_all(
        runtime_dir.join("resources/app/node_modules/trtc-electron-sdk/build/Release"),
    )
    .expect("create native runtime");
    std::fs::copy(std::env::current_exe().expect("current exe"), &binary_path)
        .expect("copy test executable");
    let binary = std::fs::read(&binary_path).expect("read copied binary");
    if binary.len() < MIN_BINARY_BYTES {
        // PE overlay 追加合法：保持可执行，同时满足真实尺寸下限。
        let mut padded = binary;
        padded.resize(MIN_BINARY_BYTES + 1024 * 1024, 0);
        std::fs::write(&binary_path, padded).expect("pad binary to trusted size");
    }
    for (name, min_bytes) in ELECTRON_FILES {
        write_trusted_file(&runtime_dir.join(name), min_bytes);
    }
    let native_root = "resources/app/node_modules/trtc-electron-sdk/build/Release";
    for name in NATIVE_NAMES {
        let path = runtime_dir.join(native_root).join(name);
        if tiny_native {
            std::fs::write(&path, name).expect("write tiny native file");
        } else {
            write_trusted_file(&path, MIN_NATIVE_BYTES);
        }
    }
    let runtime_files = runtime_entries(&runtime_dir);
    let native_files = NATIVE_NAMES
        .iter()
        .map(|name| {
            let path = format!("{native_root}/{name}");
            serde_json::json!({ "path": path, "sha256": file_hash(&runtime_dir.join(&path)) })
        })
        .collect::<Vec<_>>();
    let manifest = serde_json::json!({
        "schema_version": 1,
        "build_script_version": "test",
        "target_triple": "x86_64-pc-windows-msvc",
        "electron_version": "test",
        "trtc_sdk_version": "test",
        "sidecar_package_lock_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "external_bin": {
            "build_input_file": "jax-rtc-sidecar-x86_64-pc-windows-msvc.exe",
            "installed_file": "jax-rtc-sidecar.exe",
            "target_triple": "x86_64-pc-windows-msvc",
            "sha256": file_hash(&binary_path),
        },
        "native_files": native_files,
        "runtime_files": runtime_files,
        "bundle_resources": {
            "binaries/jax-rtc-sidecar-runtime/": "jax-rtc-sidecar-runtime/",
        },
    });
    let manifest_path = runtime_dir.join("jax-rtc-sidecar.provenance.json");
    std::fs::write(
        &manifest_path,
        serde_json::to_vec(&manifest).expect("serialize manifest"),
    )
    .expect("write manifest");
    SidecarFixture {
        binary_path,
        integrity: IntegritySpec {
            expected_manifest_sha256: file_hash(&manifest_path),
            manifest_path,
            runtime_dir,
        },
    }
}

fn write_trusted_file(path: &std::path::Path, min_bytes: usize) {
    let mut content = Vec::with_capacity(min_bytes + 2);
    content.extend_from_slice(b"MZ");
    content.resize(min_bytes, 0);
    std::fs::write(path, content).expect("write trusted-size file");
}

fn runtime_entries(runtime_dir: &std::path::Path) -> Vec<serde_json::Value> {
    let mut paths = Vec::new();
    visit(runtime_dir, runtime_dir, &mut paths);
    paths.sort();
    paths
        .into_iter()
        .map(|path| {
            let sha256 = file_hash(&runtime_dir.join(&path));
            serde_json::json!({ "path": path, "sha256": sha256 })
        })
        .collect()
}

fn visit(root: &std::path::Path, current: &std::path::Path, paths: &mut Vec<String>) {
    for entry in std::fs::read_dir(current).expect("read runtime dir") {
        let entry = entry.expect("read runtime entry");
        if entry.file_type().expect("runtime entry type").is_dir() {
            visit(root, &entry.path(), paths);
        } else {
            paths.push(
                entry
                    .path()
                    .strip_prefix(root)
                    .expect("relative runtime path")
                    .to_string_lossy()
                    .replace('\\', "/"),
            );
        }
    }
}

fn file_hash(path: &std::path::Path) -> String {
    format!(
        "{:x}",
        Sha256::digest(std::fs::read(path).expect("read file"))
    )
}
