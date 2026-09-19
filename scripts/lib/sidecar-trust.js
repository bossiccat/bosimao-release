'use strict';

const fs = require('node:fs');
const path = require('node:path');

// Production trust policy for the Windows Electron/TRTC sidecar runtime.
// Distinguishes "test fixture self-consistency" (hash/set/version checks in
// sidecar-package.js) from "production runtime trust" (real binary size and
// PE provenance). Hash self-consistency alone never proves trustworthiness:
// a 28-byte externalBin and 10-23-byte native stubs can be internally
// consistent and still be useless as a commercial runtime.

const TRUST_VERSION = '1.0.0';

// 「版本化之前」的基线版本号。
//
// 本机 current-installed 的两个 generation（g-096f42… / g-82d140…）是 2026-09-05 构建的，
// 实测它们的 provenance manifest 只有那 10 个必需键、**没有** `trust_version`。
// 它们是在 TRUST_VERSION 恰为 '1.0.0' 时构建的，所以这里显式把它标注为"版本化之前的
// 基线"，而不是静默跳过比对：
//   - 具名常量 + 注释写明历史含义与**失效条件**；
//   - 一旦 TRUST_VERSION 被 bump（例如 prune 落地那天提到 1.1.0），缺键的旧 generation
//     会被判 1.0.0 ≠ 新版本 ⇒ 校验失败 ⇒ 强制重建。
// 这是刻意保留、且有到期条件的**历史基线**，不是 allowlist：
// 它没有"加一行就放行任意文件"的口子，唯一的容忍对象是"缺少版本键"这一种形态。
const PRE_VERSIONING_TRUST_VERSION = '1.0.0';

const NATIVE_NAMES = [
  'trtc_electron_sdk.node',
  'liteav.dll',
  'txffmpeg.dll',
  'txsoundtouch.dll',
  'liteav_media_server.exe',
];

// Conservative lower bounds far above any fixture stub and below any real
// Electron 31.7.7 / TRTC 13.4.802-beta.3 artifact observed on this machine.
const MIN_EXTERNAL_BIN_BYTES = 4 * 1024 * 1024; // real electron.exe ~172 MB
const MIN_NATIVE_BYTES = 32 * 1024;             // real native set >= 139 KB
const MIN_ELECTRON_BYTES = {
  'ffmpeg.dll': 512 * 1024,
  'resources.pak': 512 * 1024,
  'icudtl.dat': 512 * 1024,
  'v8_context_snapshot.bin': 64 * 1024,
  'locales/en-US.pak': 32 * 1024,
};

class TrustError extends Error {
  constructor(code) {
    super(code);
    this.name = 'TrustError';
    this.code = code;
  }
}

function fail(code) {
  throw new TrustError(code);
}

// PE 结构判据 —— 只回答"这是不是一个 PE 映像"这一**事实**问题。
//
// 边界（刻意为之，别往这里塞 subsystem 检查）
// ------------------------------------------
// 「这个 PE 该不该是 GUI 子系统」是**策略**问题，归 `scripts/pe-subsystem-verify.py`
// 管辖（它读 OptionalHeader.Subsystem，2=GUI / 3=CUI）。`liteav_media_server.exe`
// 是 CUI，但它确实是合法 PE；把 subsystem 判定塞进本函数会让它同时承担两种职责，
// 并让 NATIVE_NAMES 的校验语义变得不可读。两侧的分工：
//     本函数      → 是不是 PE（事实，可判真假）
//     PE 子系统门禁 → 是不是 GUI（策略，需要产品决策）
// `scripts/test/sidecar-trust-pe.test.js` 有专门用例把这条边界钉死。
//
// 历史（2026-09-19，别改回去）
// ---------------------------
// 初版只读了前 2 个字节判 "MZ"：
//     const head = Buffer.alloc(2);
//     fs.readSync(fd, head, 0, 2, 0);
//     return head[0] === 0x4d && head[1] === 0x5a;
// 实测把一个 40,000 字节、全部填充 0x41、只在开头放 MZ 的文件判成 PE ——
// 而本函数被 assertProductionTrust 用来校验 externalBin 与整个 NATIVE_NAMES
// 原生集，后者是 `scripts/build-sidecar-external-bin.js` 发布路径上的生产可信门。
// 也就是说注释里声称的 "PE provenance" 当时只验了魔数，4MB 的任意 blob 都能过。
//
// 现在的判据（与 `scripts/pe-subsystem-verify.py` 同口径）：
//     MZ → e_lfanew(0x3C, 4B) → 该处必须 "PE\0\0" → OptionalHeader.Magic ∈ {0x10b, 0x20b}
const PE_SIGNATURE = 0x00004550; // "PE\0\0" 的小端读法
const OPTIONAL_MAGIC_PE32 = 0x10b;
const OPTIONAL_MAGIC_PE32_PLUS = 0x20b;
const E_LFANEW_OFFSET = 0x3c;
const E_LFANEW_FIELD_BYTES = 4;
const PE_SIGNATURE_BYTES = 4;
const COFF_HEADER_BYTES = 20;
const OPTIONAL_MAGIC_BYTES = 2;
// PE 签名 + COFF 头 + OptionalHeader.Magic：判定所需的最小尾部窗口。
const PE_HEAD_WINDOW_BYTES = PE_SIGNATURE_BYTES + COFF_HEADER_BYTES + OPTIONAL_MAGIC_BYTES;

function isPeBinary(file) {
  let fd;
  try {
    fd = fs.openSync(file, 'r');
  } catch (_) {
    return false; // 不存在 / 是目录 / 无权限：不是 PE
  }
  try {
    // 分两次按偏移读，而不是"一次读入固定前缀"：e_lfanew 由文件自身决定
    // （实测真实产物 0x80~0x108 不等，理论上可更大），固定前缀会把合法 PE 判否
    // 成假红；也避免把 180MB 的 electron.exe 整个读进内存。
    const dos = Buffer.alloc(E_LFANEW_OFFSET + E_LFANEW_FIELD_BYTES);
    if (fs.readSync(fd, dos, 0, dos.length, 0) < dos.length) return false;
    if (dos[0] !== 0x4d || dos[1] !== 0x5a) return false; // "MZ"
    const peOffset = dos.readUInt32LE(E_LFANEW_OFFSET);
    const head = Buffer.alloc(PE_HEAD_WINDOW_BYTES);
    // peOffset 越界（指向文件尾之后）时 readSync 返回 0 或短读，一律判否。
    if (fs.readSync(fd, head, 0, head.length, peOffset) < head.length) return false;
    if (head.readUInt32LE(0) !== PE_SIGNATURE) return false;
    const magic = head.readUInt16LE(PE_SIGNATURE_BYTES + COFF_HEADER_BYTES);
    return magic === OPTIONAL_MAGIC_PE32 || magic === OPTIONAL_MAGIC_PE32_PLUS;
  } catch (_) {
    return false;
  } finally {
    fs.closeSync(fd);
  }
}

// 生产可信门：策略版本 + 体积 + PE 结构。
//
// 策略版本（`trust_version`）为什么必须比对
// ----------------------------------------
// 可信门的**策略本身会变**（阈值、PE 判据、原生集成员）。变了之后，按旧策略构建的
// generation 仍然躺在机器上，只靠"新构建走新策略"是拦不住它们的 —— 旧的原样留在野。
// 把策略版本写进 provenance manifest、并让本函数比对，等于把"策略变了"变成**机械后果**：
// 缺版本键或版本不匹配的 generation 直接校验失败，强制重建。
//
// 输入约定：调用方必须把 **selected generation 的 provenance manifest** 交进来
// （`input.provenance`）。缺失即 `SIDECAR_PACKAGE_TRUST_PROVENANCE_MISSING` ——
// 刻意 fail-closed，让"忘了接线"表现为响亮错误，而不是静默跳过比对。
function assertProductionTrust(input) {
  if (!input || !input.executable || !input.nativeDir || !input.runtimeDir) {
    fail('SIDECAR_PACKAGE_TRUST_INPUT_INVALID');
  }
  const { executable, nativeDir, runtimeDir } = input;

  const provenance = input.provenance;
  if (!provenance || typeof provenance !== 'object' || Array.isArray(provenance)) {
    fail('SIDECAR_PACKAGE_TRUST_PROVENANCE_MISSING');
  }
  const declared = provenance.trust_version === undefined
    ? PRE_VERSIONING_TRUST_VERSION
    : provenance.trust_version;
  if (declared !== TRUST_VERSION) fail('SIDECAR_PACKAGE_TRUST_VERSION_MISMATCH');

  if (!fs.existsSync(executable)) fail('SIDECAR_PACKAGE_TRUST_MISSING');
  if (fs.statSync(executable).size < MIN_EXTERNAL_BIN_BYTES) fail('SIDECAR_PACKAGE_TRUST_MIN_SIZE');
  if (!isPeBinary(executable)) fail('SIDECAR_PACKAGE_TRUST_PE_HEADER');

  for (const name of NATIVE_NAMES) {
    const file = path.join(nativeDir, name);
    if (!fs.existsSync(file)) fail('SIDECAR_PACKAGE_TRUST_MISSING');
    if (fs.statSync(file).size < MIN_NATIVE_BYTES) fail('SIDECAR_PACKAGE_TRUST_MIN_SIZE');
    if (!isPeBinary(file)) fail('SIDECAR_PACKAGE_TRUST_PE_HEADER');
  }

  for (const [name, minBytes] of Object.entries(MIN_ELECTRON_BYTES)) {
    const file = path.join(runtimeDir, name);
    if (!fs.existsSync(file)) fail('SIDECAR_PACKAGE_TRUST_MISSING');
    if (fs.statSync(file).size < minBytes) fail('SIDECAR_PACKAGE_TRUST_MIN_SIZE');
  }
}

module.exports = {
  PRE_VERSIONING_TRUST_VERSION,
  TRUST_VERSION,
  assertProductionTrust,
  isPeBinary,
  MIN_EXTERNAL_BIN_BYTES,
  MIN_NATIVE_BYTES,
  NATIVE_NAMES,
};
