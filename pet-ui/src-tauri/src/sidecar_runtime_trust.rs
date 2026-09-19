//! sidecar_runtime_trust.rs — 生产可信门（与 `scripts/lib/sidecar-trust.js` 同一策略）。
//!
//! hash 自洽不等于生产可信：即使整包替换为微型 runtime 并重算全部 hash，
//! 启动门也必须拒绝。阈值保守低于真实 Electron 31.7.7 / TRTC 13.4.802-beta.3
//! 产物，但远高于任何 fixture/占位 stub（externalBin 真实约 172MB、native 最小约 139KB）。

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
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

// PE 结构判据 —— 只回答"这是不是一个 PE 映像"这一**事实**问题。
//
// 边界（刻意为之，别往这里塞 subsystem 检查）
// ------------------------------------------
// 「这个 PE 该不该是 GUI 子系统」是**策略**问题，归 `scripts/pe-subsystem-verify.py`
// 管辖（它读 OptionalHeader.Subsystem，2=GUI / 3=CUI）。本机实测：5 件原生产物里的
// 4 件（`liteav.dll` / `txffmpeg.dll` / `txsoundtouch.dll` / `liteav_media_server.exe`）
// 的 .Subsystem 都是 3，但它们全都是合法 PE；把 subsystem 判定塞进本函数会让
// NATIVE_NAMES 的校验语义变得不可读，并且**用策略判错去关掉客户机的启动**。
// 两侧分工：
//     本函数      → 是不是 PE（事实，可判真假）
//     PE 子系统门禁 → 是不是 GUI（策略，需要产品决策）
// 下面的 `subsystem_is_not_part_of_the_pe_ness_judgement` 用"差异字节只有一处"把它钉死。
//
// 历史（2026-09-19，别改回去）
// ---------------------------
// 本函数初版与 JS 侧同族，只读了前 2 个字节判 "MZ"：
//     let mut head = [0u8; 2];
//     file.read_exact(&mut head).is_ok() && head == [0x4d, 0x5a]
// 而本文件头注释当时就在宣称「与 `scripts/lib/sidecar-trust.js` 同一策略」——
// JS 侧在 3e221ba 收紧为真 PE 结构校验之后，这句等价性声明在 Rust 侧一度是**假的**。
// 本函数跑在**每次启动、全部客户机**上（比 JS 侧那条构建期门更险），
// 所以两侧必须同口径：MZ → e_lfanew → "PE\0\0" → OptionalHeader.Magic ∈ {0x10b, 0x20b}。
const PE_SIGNATURE: u32 = 0x0000_4550; // "PE\0\0" 的小端读法
const OPTIONAL_MAGIC_PE32: u16 = 0x010b;
const OPTIONAL_MAGIC_PE32_PLUS: u16 = 0x020b;
const E_LFANEW_OFFSET: u64 = 0x3c;
const E_LFANEW_FIELD_BYTES: usize = 4;
const PE_SIGNATURE_BYTES: usize = 4;
const COFF_HEADER_BYTES: usize = 20;
const OPTIONAL_MAGIC_BYTES: usize = 2;
// PE 签名 + COFF 头 + OptionalHeader.Magic：判定所需的最小尾部窗口。
const PE_HEAD_WINDOW_BYTES: usize = PE_SIGNATURE_BYTES + COFF_HEADER_BYTES + OPTIONAL_MAGIC_BYTES;

/// 按偏移读定长切片。短读（含 seek 到文件尾之后）一律 `None`。
///
/// 不整文件读入：externalBin 真实 172MB，而且 `e_lfanew` 由文件自身决定
/// （本机实测 0x78 / 0x110 不等），"一次读入固定前缀"会把 e_lfanew >= 前缀长度的
/// 合法 PE 判否 —— 那是**假红**，方向比假绿更严重。
fn read_at(file: &mut File, offset: u64, length: usize) -> Option<Vec<u8>> {
    let mut buffer = vec![0u8; length];
    file.seek(SeekFrom::Start(offset)).ok()?;
    file.read_exact(&mut buffer).ok()?;
    Some(buffer)
}

fn is_pe_binary(path: &Path) -> bool {
    let Ok(mut file) = File::open(path) else {
        return false; // 不存在 / 是目录 / 无权限：不是 PE
    };
    // dos 窗口 0x00..0x40，其中 (0x3C, 4B) 是 e_lfanew。
    let dos_len = E_LFANEW_OFFSET as usize + E_LFANEW_FIELD_BYTES;
    let Some(dos) = read_at(&mut file, 0, dos_len) else {
        return false;
    };
    if dos[0] != 0x4d || dos[1] != 0x5a {
        return false; // "MZ"
    }
    let lfanew = E_LFANEW_OFFSET as usize;
    let pe_offset = u32::from_le_bytes([
        dos[lfanew],
        dos[lfanew + 1],
        dos[lfanew + 2],
        dos[lfanew + 3],
    ]) as u64;
    let Some(head) = read_at(&mut file, pe_offset, PE_HEAD_WINDOW_BYTES) else {
        return false; // pe_offset 越界
    };
    let signature = u32::from_le_bytes([head[0], head[1], head[2], head[3]]);
    if signature != PE_SIGNATURE {
        return false;
    }
    let magic_offset = PE_SIGNATURE_BYTES + COFF_HEADER_BYTES;
    let magic = u16::from_le_bytes([head[magic_offset], head[magic_offset + 1]]);
    magic == OPTIONAL_MAGIC_PE32 || magic == OPTIONAL_MAGIC_PE32_PLUS
}

/// externalBin >= 4MB 且结构上是 PE；native 五个 >= 32KB 且结构上是 PE；
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

// 2026-09-19：`is_pe_binary` 此前只判 2 字节 `MZ`，却承担着"PE provenance"的职责。
// 下面这组用例把三件事钉住：
//   1. **假红侧优先** —— 真实 172MB externalBin + 真实 5 件原生集在收紧后必须**全部通过**，
//      且走的是生产函数 `validate_runtime_trust` 本身（本门的失效方向是"判错 = 客户机拒绝
//      启动"，比假绿严重，所以这一侧排在最前面）；
//   2. 假绿侧 —— `MZ` + 垃圾/零填充、e_lfanew 指向 0 或文件尾之后、未知 magic、截断文件
//      一律判否；
//   3. `subsystem` **不**参与该判定（差异字节只有一处）。
#[cfg(test)]
mod pe_structure_tests {
    use super::*;
    use crate::sidecar::IntegritySpec;
    use std::path::PathBuf;
    use std::time::Duration;

    const FILL: u8 = 0x41;
    const DEFAULT_E_LFANEW: u32 = 0x80;
    // 偏移口径与 `scripts/test/pe-fixture.js` 的 `peBytes()` 一致（pe+24=magic，
    // pe+24+68=subsystem），便于 JS / Rust 两侧互相印证。
    const OPTIONAL_MAGIC_OFFSET: usize = 24;
    const OPTIONAL_SUBSYSTEM_OFFSET: usize = 24 + 68;
    const GUI_SUBSYSTEM: u16 = 2;
    const CUI_SUBSYSTEM: u16 = 3;

    /// 结构合法的最小 PE 映像：MZ / e_lfanew / "PE\0\0" / COFF 头(20B) /
    /// OptionalHeader.Magic 与 .Subsystem 各在正确偏移。刻意不做成"完整可加载映像"：
    /// 本构造器只服务于"这是不是 PE"这一事实判据，多构造无助于区分真假。
    fn pe_bytes(magic: u16, subsystem: u16, size: usize) -> Vec<u8> {
        let minimum = DEFAULT_E_LFANEW as usize + OPTIONAL_SUBSYSTEM_OFFSET + 2;
        let mut buffer = vec![FILL; minimum.max(size)];
        let pe = DEFAULT_E_LFANEW as usize;
        buffer[0..2].copy_from_slice(b"MZ");
        buffer[0x3c..0x40].copy_from_slice(&DEFAULT_E_LFANEW.to_le_bytes());
        buffer[pe..pe + PE_SIGNATURE_BYTES].copy_from_slice(&PE_SIGNATURE.to_le_bytes());
        buffer[pe + OPTIONAL_MAGIC_OFFSET..pe + OPTIONAL_MAGIC_OFFSET + OPTIONAL_MAGIC_BYTES]
            .copy_from_slice(&magic.to_le_bytes());
        buffer[pe + OPTIONAL_SUBSYSTEM_OFFSET..pe + OPTIONAL_SUBSYSTEM_OFFSET + 2]
            .copy_from_slice(&subsystem.to_le_bytes());
        buffer
    }

    /// "只有 MZ 魔数"的伪 PE —— 团队 2026-09-19 实测过能被旧判据放行的确切形状。
    fn mz_only_bytes(size: usize, byte: u8) -> Vec<u8> {
        let mut buffer = vec![byte; size];
        buffer[0..2].copy_from_slice(b"MZ");
        buffer
    }

    /// 每个用例一个具名独立目录（house pattern：`std::env::temp_dir()` + 具名子目录）。
    fn fixture(name: &str, bytes: &[u8]) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("jaxpet_pe_structure_{name}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("temp fixture dir");
        let file = dir.join("sample.exe");
        std::fs::write(&file, bytes).expect("temp fixture file");
        file
    }

    /// 本机已发布（current.json 指向）的 generation 目录；未发布时为 `None`。
    fn installed_generation_dir() -> Option<PathBuf> {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("binaries")
            .join("jax-rtc-sidecar-runtime");
        let pointer = std::fs::read_to_string(root.join("current.json")).ok()?;
        let parsed: serde_json::Value = serde_json::from_str(&pointer).ok()?;
        let dir = root
            .join("generations")
            .join(parsed.get("generation")?.as_str()?);
        dir.is_dir().then_some(dir)
    }

    // ----------------------------------------------------------------------
    // ① 假红侧（优先）：真产物必须全部通过，且走生产函数本身
    // ----------------------------------------------------------------------
    #[test]
    fn the_real_installed_generation_still_passes_the_production_gate() {
        let Some(generation) = installed_generation_dir() else {
            // 构建产物缺失（本机没跑过 publish），不是平台差异，所以把原因**打印出来**
            // 而不是静默通过。走到这里说明本次"假红侧"验收未执行 —— 报告里必须如实说明。
            eprintln!(
                "[pe-structure] 本机没有已发布 generation，真实产物假红验收未执行；\
                 结构判据本身仍由本模块其余用例覆盖"
            );
            return;
        };
        let executable = generation.join("jax-rtc-sidecar.exe");
        let native_root = generation.join("resources/app/node_modules/trtc-electron-sdk/build/Release");

        // 先证明"验的是真东西"：否则这条用例可能对着一个 stub 宣称假红已覆盖。
        let size = file_size(&executable).expect("real externalBin must exist");
        assert!(
            size > 100 * 1024 * 1024,
            "本条用例的对象必须是真实 ~172MB 产物，实测 {size} 字节 —— 对 stub 做假红验收没有意义"
        );
        println!("[pe-structure] externalBin: {size} bytes");

        assert!(
            is_pe_binary(&executable),
            "假红：真实 externalBin 被判否 ⇒ 客户机每次启动拒绝 spawn"
        );
        for name in NATIVE_NAMES {
            let file = native_root.join(name);
            let native_size = file_size(&file)
                .unwrap_or_else(|| panic!("真实原生集缺文件：{name}"));
            println!("[pe-structure] native {name}: {native_size} bytes");
            assert!(is_pe_binary(&file), "假红：真实原生文件被判否：{name}");
        }

        // 端到端：走**生产函数本身**，而不是旁路调用判据函数。
        let spec = SidecarSpec {
            binary_path: executable,
            expected_sha256: String::new(), // validate_runtime_trust 不用它
            integrity: IntegritySpec {
                manifest_path: generation.join("jax-rtc-sidecar.provenance.json"),
                expected_manifest_sha256: String::new(),
                runtime_dir: generation.clone(),
            },
            args: Vec::new(),
            ca_cert_path: PathBuf::new(),
            graceful_timeout: Duration::from_secs(1),
            kill_timeout: Duration::from_secs(1),
        };
        assert!(
            validate_runtime_trust(&spec).is_ok(),
            "假红：生产门拒绝本机真实 generation"
        );

        // 真实 CUI 产物：.Subsystem 实测是 3，但它依然是合法 PE。
        // 这条把"策略 vs 事实"的边界钉在**真实产物**上，而不是只钉在构造的 fixture 上。
        let media_server = native_root.join("liteav_media_server.exe");
        let bytes = std::fs::read(&media_server).expect("read liteav_media_server.exe");
        let lfanew = E_LFANEW_OFFSET as usize;
        let pe_offset = u32::from_le_bytes([
            bytes[lfanew],
            bytes[lfanew + 1],
            bytes[lfanew + 2],
            bytes[lfanew + 3],
        ]) as usize;
        let subsystem = u16::from_le_bytes([
            bytes[pe_offset + OPTIONAL_SUBSYSTEM_OFFSET],
            bytes[pe_offset + OPTIONAL_SUBSYSTEM_OFFSET + 1],
        ]);
        println!("[pe-structure] liteav_media_server.exe subsystem: {subsystem}");
        assert_eq!(
            subsystem, CUI_SUBSYSTEM,
            "本机 liteav_media_server.exe 实测应为 CUI(3)；若产物换了这条要跟着复核"
        );
        assert!(
            is_pe_binary(&media_server),
            "CUI 是合法 PE —— 策略判定不得关掉客户机启动"
        );
    }

    // ----------------------------------------------------------------------
    // ② 假绿侧：魔数/半结构一律判否
    // ----------------------------------------------------------------------
    #[test]
    fn accepts_a_structurally_valid_pe32_plus_image() {
        let file = fixture(
            "pe32plus",
            &pe_bytes(OPTIONAL_MAGIC_PE32_PLUS, GUI_SUBSYSTEM, 0),
        );
        assert!(is_pe_binary(&file));
    }

    #[test]
    fn accepts_a_structurally_valid_pe32_image() {
        let file = fixture("pe32", &pe_bytes(OPTIONAL_MAGIC_PE32, GUI_SUBSYSTEM, 0));
        assert!(is_pe_binary(&file));
    }

    #[test]
    fn rejects_the_measured_false_positive_mz_plus_forty_thousand_bytes_of_filler() {
        let bytes = mz_only_bytes(40_000, FILL);
        assert_eq!(bytes.len(), 40_000);
        let file = fixture("mz_filler", &bytes);
        assert!(!is_pe_binary(&file), "MZ 魔数不足以证明是 PE");
    }

    #[test]
    fn rejects_mz_plus_zero_filler_at_shipped_binary_size() {
        let file = fixture("mz_zero", &mz_only_bytes(5 * 1024 * 1024, 0x00));
        assert!(!is_pe_binary(&file));
    }

    #[test]
    fn rejects_a_two_byte_mz_stub() {
        let file = fixture("mz_two_bytes", &[0x4d, 0x5a]);
        assert!(!is_pe_binary(&file));
    }

    #[test]
    fn rejects_a_file_whose_e_lfanew_does_not_point_at_the_pe_signature() {
        let mut bytes = pe_bytes(OPTIONAL_MAGIC_PE32_PLUS, GUI_SUBSYSTEM, 0);
        bytes[0x3c..0x40].copy_from_slice(&0u32.to_le_bytes()); // 那里是 "MZ\0\0"
        let file = fixture("lfanew_zero", &bytes);
        assert!(!is_pe_binary(&file));
    }

    #[test]
    fn rejects_a_file_whose_e_lfanew_points_past_the_end_of_the_file() {
        let mut bytes = pe_bytes(OPTIONAL_MAGIC_PE32_PLUS, GUI_SUBSYSTEM, 0);
        bytes[0x3c..0x40].copy_from_slice(&0x7fff_ffffu32.to_le_bytes());
        let file = fixture("lfanew_out_of_range", &bytes);
        assert!(!is_pe_binary(&file));
    }

    #[test]
    fn rejects_a_valid_signature_followed_by_an_unknown_optional_header_magic() {
        let file = fixture("magic_rom", &pe_bytes(0x0107, GUI_SUBSYSTEM, 0)); // ROM 映像
        assert!(!is_pe_binary(&file));
    }

    #[test]
    fn rejects_a_truncated_file_that_ends_before_the_optional_header_magic() {
        let full = pe_bytes(OPTIONAL_MAGIC_PE32_PLUS, GUI_SUBSYSTEM, 0);
        let end = DEFAULT_E_LFANEW as usize + PE_SIGNATURE_BYTES + COFF_HEADER_BYTES + 1;
        let file = fixture("truncated", &full[..end]);
        assert!(!is_pe_binary(&file));
    }

    #[test]
    fn returns_false_for_a_missing_file_instead_of_panicking() {
        let missing = std::env::temp_dir()
            .join("jaxpet_pe_structure_absent")
            .join("nope.exe");
        assert!(!is_pe_binary(&missing));
    }

    #[test]
    fn returns_false_for_a_directory_instead_of_panicking() {
        let dir = std::env::temp_dir().join("jaxpet_pe_structure_is_a_dir");
        let _ = std::fs::create_dir_all(&dir);
        assert!(!is_pe_binary(&dir));
    }

    // ----------------------------------------------------------------------
    // ③ 边界：subsystem 是策略，不是"是不是 PE"的事实
    // ----------------------------------------------------------------------
    #[test]
    fn subsystem_is_not_part_of_the_pe_ness_judgement() {
        let gui = fixture(
            "boundary_gui",
            &pe_bytes(OPTIONAL_MAGIC_PE32_PLUS, GUI_SUBSYSTEM, 0),
        );
        let cui = fixture(
            "boundary_cui",
            &pe_bytes(OPTIONAL_MAGIC_PE32_PLUS, CUI_SUBSYSTEM, 0),
        );
        let gui_bytes = std::fs::read(&gui).expect("gui fixture");
        let cui_bytes = std::fs::read(&cui).expect("cui fixture");
        assert!(is_pe_binary(&gui));
        assert!(is_pe_binary(&cui), "CUI 是合法 PE，不能在这里判否");
        assert_eq!(gui_bytes.len(), cui_bytes.len());
        let differing: Vec<usize> = (0..gui_bytes.len())
            .filter(|index| gui_bytes[*index] != cui_bytes[*index])
            .collect();
        // GUI(2) 与 CUI(3) 的差异只有 .Subsystem 的**低字节这一处**。
        // 若有人把 subsystem 检查塞进 is_pe_binary，上面两条断言或这条会红。
        assert_eq!(
            differing,
            vec![DEFAULT_E_LFANEW as usize + OPTIONAL_SUBSYSTEM_OFFSET]
        );
    }
}
