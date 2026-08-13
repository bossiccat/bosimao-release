//! sidecar_runtime_trust.rs — 生产可信门（与 `scripts/lib/sidecar-trust.js` 同一策略）。
//!
//! hash 自洽不等于生产可信：即使整包替换为微型 runtime 并重算全部 hash，
//! 启动门也必须拒绝。阈值保守低于真实 Electron 31.7.7 / TRTC 13.4.802-beta.3
//! 产物，但远高于任何 fixture/占位 stub（externalBin 真实约 172MB、native 最小约 139KB）。

use std::path::Path;

use crate::sidecar::{SidecarError, SidecarSpec};

const MIN_EXTERNAL_BIN_BYTES: u64 = 4 * 1024 * 1024;
const MIN_NATIVE_BYTES: u64 = 32 * 1024;
const NATIVE_NAMES: [&str; 5] = [
    "trtc_electron_sdk.node",
    "liteav.dll",
    "txffmpeg.dll",
    "txsoundtouch.dll",
    "liteav_media_server.exe",
];
const MIN_ELECTRON_BYTES: [(&str, u64); 5] = [
    ("ffmpeg.dll", 512 * 1024),
    ("resources.pak", 512 * 1024),
    ("icudtl.dat", 512 * 1024),
    ("v8_context_snapshot.bin", 64 * 1024),
    ("locales/en-US.pak", 32 * 1024),
];

fn file_size(path: &Path) -> Option<u64> {
    std::fs::metadata(path).ok().map(|metadata| metadata.len())
}

fn is_pe_binary(path: &Path) -> bool {
    use std::io::Read;
    let Ok(mut file) = std::fs::File::open(path) else {
        return false;
    };
    let mut head = [0u8; 2];
    file.read_exact(&mut head).is_ok() && head == [0x4d, 0x5a]
}

/// externalBin >= 4MB 且 PE `MZ`；native 五个 >= 32KB 且 PE `MZ`；
/// Electron 六个关键文件达到各自最小体积。任何一项不满足即拒绝启动。
pub(crate) fn validate_runtime_trust(spec: &SidecarSpec) -> Result<(), SidecarError> {
    let trusted = |file: &Path, min_bytes: u64, require_pe: bool| -> bool {
        match file_size(file) {
            Some(size) if size >= min_bytes => !require_pe || is_pe_binary(file),
            _ => false,
        }
    };
    if !trusted(&spec.binary_path, MIN_EXTERNAL_BIN_BYTES, true) {
        return Err(SidecarError::RuntimeUntrusted);
    }
    let native_root = spec
        .integrity
        .runtime_dir
        .join("resources/app/node_modules/trtc-electron-sdk/build/Release");
    for name in NATIVE_NAMES {
        if !trusted(&native_root.join(name), MIN_NATIVE_BYTES, true) {
            return Err(SidecarError::RuntimeUntrusted);
        }
    }
    for (name, min_bytes) in MIN_ELECTRON_BYTES {
        if !trusted(&spec.integrity.runtime_dir.join(name), min_bytes, false) {
            return Err(SidecarError::RuntimeUntrusted);
        }
    }
    Ok(())
}
