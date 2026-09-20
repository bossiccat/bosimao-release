use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};

use jax_pet::sidecar::IntegritySpec;
use sha2::{Digest, Sha256};

static NEXT_FIXTURE: AtomicUsize = AtomicUsize::new(0);

/// Production trust policy 的**策略版本**，与 `scripts/lib/sidecar-trust.js` 的
/// `TRUST_VERSION` 一致。本文件与那边一样是**独立构造**（刻意不复用生产 createProvenance），
/// 所以这是该常量的第 3 份副本 —— 由 sidecar-package.test.js 的
/// "the native closed set is one set across every production copy" 同族锁钉住。
///
/// 为什么 fixture 必须盖上它：`validate_metadata` 对**缺键**的判定是"退回版本化之前的
/// 基线"（见 sidecar_integrity.rs 的 PRE_VERSIONING_TRUST_VERSION）。2026-09-20 随随包
/// 原生集剪除把策略版本 1.0.0 → 1.1.0，于是缺键的 fixture 会被可信门判成旧世代 ⇒
/// 下面这一整组集成测试会从"验它们本来要验的东西"退化成"验策略版本"，且错因被换掉。
const TRUST_VERSION: &str = "1.1.0";

/// Production trust policy 的保守下限，与 `scripts/lib/sidecar-trust.js` 保持一致。
const MIN_BINARY_BYTES: usize = 4 * 1024 * 1024;
const MIN_NATIVE_BYTES: usize = 32 * 1024;
// 4 件。刻意缺席的成员（TRTC 媒体混流服务进程，subsystem = 3 的 CUI）见
// scripts/lib/sidecar-trust.js 的 INTENTIONALLY_ABSENT_NATIVE。
// **必须与 validate_native_subset 的 REQUIRED 同一集合**：那条判据是精确集合相等，
// 而本 fixture 构造的 manifest 正是喂给 validate_runtime 的
// （sidecar.rs 的 validate_for_launch）—— 多一件会直接被判 ManifestInvalid。
const NATIVE_NAMES: [&str; 4] = [
    "trtc_electron_sdk.node",
    "liteav.dll",
    "txffmpeg.dll",
    "txsoundtouch.dll",
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

/// 真实尺寸 PE 闭集 fixture：binary 为可执行 PE 且 >= 4MB，native 四个为
/// >= 32KB 且带 MZ 头的 PE 风格文件，Electron 六个文件达到各自最小体积。
pub fn sidecar_fixture() -> SidecarFixture {
    build_fixture(false)
}

/// 微型 native 变体：native 四个被写为微型文本（10-22 bytes，即文件名本身的长度），
/// 用于证明生产可信门拒绝 hash 自洽的微型 runtime。
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
        "trust_version": TRUST_VERSION,
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
            "binaries/jax-rtc-sidecar-runtime/": "jrt/",
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

/// 最小 PE 的 `e_lfanew`（与 `scripts/test/pe-fixture.js` 的 `DEFAULT_E_LFANEW` 同口径）。
const DEFAULT_E_LFANEW: usize = 0x80;
/// OptionalHeader.Magic 相对 `e_lfanew` 的偏移（与 `pe-subsystem-verify.py` 同口径）。
const OPTIONAL_MAGIC_OFFSET: usize = 24;

/// 写一个**结构合法**的最小 PE，并 padding 到至少 `min_bytes`。
///
/// 2026-09-20 修正：此前这里写的是 `"MZ"` + 全零填充。那个桩只在 `is_pe_binary`
/// 「只读前 2 字节魔数」的旧判据下成立 —— 也就是说它一边被当作"可信件"喂进信任门，
/// 一边**什么都没验**。2026-09-19 `3cdb4b32` 把判据收紧成真结构校验之后，这个桩
/// 立刻变成假红（`Sidecar(RuntimeUntrusted)`），而 `cargo test` 不在任何 workflow 里，
/// 于是三条集成测试红了一整天没人看见。
///
/// JS 侧的同类桩（`scripts/test/sidecar-package.test.js` 里那个
/// `Buffer.concat([MZ, zeros])`）在收紧当天就换成了真结构；**Rust 侧漏了** ——
/// 本函数即那次不对称修复的对偶。判据是对的，坏的是 fixture。
///
/// 结构：`MZ` → `e_lfanew`(0x3C) → `"PE\0\0"` → OptionalHeader.Magic。
/// 刻意只构造到 Magic 偏移为止，不做成完整可加载映像 —— 本构造器只服务于
/// 「这是不是一个 PE 映像」这一事实判据，多构造无助于区分真假。
fn write_trusted_file(path: &std::path::Path, min_bytes: usize) {
    let minimum = DEFAULT_E_LFANEW + OPTIONAL_MAGIC_OFFSET + 2;
    let size = min_bytes.max(minimum);
    let mut content = vec![0u8; size];
    content[0..2].copy_from_slice(b"MZ");
    content[0x3c..0x40].copy_from_slice(&(DEFAULT_E_LFANEW as u32).to_le_bytes());
    content[DEFAULT_E_LFANEW..DEFAULT_E_LFANEW + 4].copy_from_slice(b"PE\0\0");
    content[DEFAULT_E_LFANEW + OPTIONAL_MAGIC_OFFSET
        ..DEFAULT_E_LFANEW + OPTIONAL_MAGIC_OFFSET + 2]
        .copy_from_slice(&0x20bu16.to_le_bytes());
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
