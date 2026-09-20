'use strict';

// 契约：`isPeBinary` / `assertProductionTrust` 的 PE 判据必须是真的。
//
// 背景（2026-09-19，claim windows-popup-free）
// ------------------------------------------
// `scripts/lib/sidecar-trust.js` 的 `isPeBinary` 初版只读前 2 个字节：
//
//     const head = Buffer.alloc(2);
//     fs.readSync(fd, head, 0, 2, 0);
//     return head[0] === 0x4d && head[1] === 0x5a;   // 仅 "MZ" 魔数
//
// 团队实测：一个 40,000 字节、全部填充 0x41、只在开头放 `MZ` 的文件被判为 PE。
// 而该函数顶部注释声称负责 "real binary size and PE provenance" ——
// **尺寸是真的，PE 来源只验了魔数**。它被 `assertProductionTrust` 用来校验
// externalBin 与整个 `NATIVE_NAMES` 原生集，而后者是
// `scripts/build-sidecar-external-bin.js` 发布路径上的生产可信门。
//
// 所以本文件同时钉两件事：
//   1. "这是不是 PE" 必须是**结构**判断（MZ → e_lfanew → "PE\0\0" → OptionalHeader.magic）；
//   2. subsystem **不**属于这个判断。subsystem 是策略，归
//      `scripts/pe-subsystem-verify.py` 管辖。CUI 是合法 PE —— 把策略塞进事实判断
//      会让 NATIVE_NAMES 的校验语义不可读。下面有专门一条用例把这个边界钉死，
//      防止后人往里加 subsystem 检查（样本名字 2026-09-20 起换成一个仍在集合内的成员：
//      原来那个 CUI 产物已被剪除，见 sidecar-trust.js 的 INTENTIONALLY_ABSENT_NATIVE）。

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  PRE_VERSIONING_TRUST_VERSION,
  TRUST_VERSION,
  assertProductionTrust,
  isPeBinary,
  MIN_EXTERNAL_BIN_BYTES,
  MIN_NATIVE_BYTES,
  NATIVE_NAMES,
} = require('../lib/sidecar-trust');
const {
  CUI_SUBSYSTEM,
  DEFAULT_E_LFANEW,
  GUI_SUBSYSTEM,
  OPTIONAL_MAGIC_PE32,
  OPTIONAL_MAGIC_PE32_PLUS,
  OPTIONAL_SUBSYSTEM_OFFSET,
  mzOnlyBytes,
  peBytes,
} = require('./pe-fixture');

const REPO_ROOT = path.resolve(__dirname, '..', '..');

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'jax-pe-trust-'));
}

function writePe(file, options) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, peBytes(options));
  return file;
}

// 组装一个"体积达标 + 结构合法 + 策略版本匹配"的 assertProductionTrust 输入。
// `provenance` 是必需的：可信门要拿它比对策略版本（缺失即 fail-closed）。
function trustFixture(mutate = {}) {
  const root = tempDir();
  const nativeDir = path.join(root, 'native');
  const runtimeDir = path.join(root, 'runtime');
  fs.mkdirSync(nativeDir, { recursive: true });
  fs.mkdirSync(path.join(runtimeDir, 'locales'), { recursive: true });

  const executable = mutate.executable
    ? mutate.executable(path.join(root, 'electron.exe'))
    : writePe(path.join(root, 'electron.exe'), { size: MIN_EXTERNAL_BIN_BYTES + 1024 });

  for (const name of NATIVE_NAMES) {
    const file = path.join(nativeDir, name);
    const kind = mutate.native && mutate.native[name];
    if (kind === 'mz-only') fs.writeFileSync(file, mzOnlyBytes(MIN_NATIVE_BYTES + 1024));
    else if (kind === 'cui') writePe(file, { subsystem: CUI_SUBSYSTEM, size: MIN_NATIVE_BYTES + 1024 });
    else writePe(file, { size: MIN_NATIVE_BYTES + 1024 });
  }

  const electronFiles = {
    'ffmpeg.dll': 512 * 1024,
    'resources.pak': 512 * 1024,
    'icudtl.dat': 512 * 1024,
    'v8_context_snapshot.bin': 64 * 1024,
    'locales/en-US.pak': 32 * 1024,
  };
  for (const [name, size] of Object.entries(electronFiles)) {
    fs.writeFileSync(path.join(runtimeDir, name), Buffer.alloc(size));
  }

  const provenance = mutate.provenance === undefined
    ? { trust_version: TRUST_VERSION }
    : mutate.provenance;
  const input = { executable, nativeDir, runtimeDir };
  if (provenance !== 'OMIT') input.provenance = provenance;
  return { root, input };
}

function trustCode(input) {
  try {
    assertProductionTrust(input);
    return 'OK';
  } catch (error) {
    return error.code || `UNEXPECTED:${error.message}`;
  }
}

// --------------------------------------------------------------------------
// 事实判据：结构正确才算是 PE
// --------------------------------------------------------------------------
test('accepts a structurally valid PE32+ image', () => {
  const file = writePe(path.join(tempDir(), 'a.exe'), { magic: OPTIONAL_MAGIC_PE32_PLUS });
  assert.equal(isPeBinary(file), true);
});

test('accepts a structurally valid PE32 image', () => {
  const file = writePe(path.join(tempDir(), 'a.exe'), { magic: OPTIONAL_MAGIC_PE32 });
  assert.equal(isPeBinary(file), true);
});

test('rejects the measured false positive: MZ plus 40,000 bytes of filler', () => {
  // 团队实测过的确切形状：40,000 字节全 0x41，只在开头放 MZ。
  const file = path.join(tempDir(), 'fake.exe');
  fs.writeFileSync(file, mzOnlyBytes(40000));
  assert.equal(fs.statSync(file).size, 40000);
  assert.equal(isPeBinary(file), false, 'MZ 魔数不足以证明是 PE');
});

test('rejects MZ plus zero filler at shipped-binary size', () => {
  const file = path.join(tempDir(), 'fake.exe');
  fs.writeFileSync(file, mzOnlyBytes(5 * 1024 * 1024, 0x00));
  assert.equal(isPeBinary(file), false);
});

test('rejects a file whose e_lfanew does not point at the PE signature', () => {
  const file = path.join(tempDir(), 'a.exe');
  const buffer = peBytes({});
  buffer.writeUInt32LE(0x00000000, 0x3c); // e_lfanew → 0，那里是 "MZ\0\0"
  fs.writeFileSync(file, buffer);
  assert.equal(isPeBinary(file), false);
});

test('rejects a file whose e_lfanew points past the end of the file', () => {
  const file = path.join(tempDir(), 'a.exe');
  const buffer = peBytes({});
  buffer.writeUInt32LE(0x7fffffff, 0x3c);
  fs.writeFileSync(file, buffer);
  assert.equal(isPeBinary(file), false);
});

test('rejects a valid signature followed by an unknown OptionalHeader magic', () => {
  const file = path.join(tempDir(), 'a.exe');
  fs.writeFileSync(file, peBytes({ magic: 0x0107 })); // ROM 映像，不是 GUI/CUI 映像
  assert.equal(isPeBinary(file), false);
});

test('rejects a truncated file that ends before the OptionalHeader magic', () => {
  const file = path.join(tempDir(), 'a.exe');
  const full = peBytes({});
  fs.writeFileSync(file, full.subarray(0, 0x40 + 4 + 20 + 1));
  assert.equal(isPeBinary(file), false);
});

test('rejects a two-byte MZ stub', () => {
  const file = path.join(tempDir(), 'a.exe');
  fs.writeFileSync(file, Buffer.from([0x4d, 0x5a]));
  assert.equal(isPeBinary(file), false);
});

test('returns false for a missing file instead of throwing', () => {
  assert.equal(isPeBinary(path.join(tempDir(), 'nope.exe')), false);
});

test('returns false for a directory instead of throwing', () => {
  assert.equal(isPeBinary(tempDir()), false);
});

// --------------------------------------------------------------------------
// 边界：subsystem 是策略，不是"是不是 PE"的事实
// --------------------------------------------------------------------------
test('subsystem is not part of the PE-ness judgement (CUI is still a PE)', () => {
  // liteav_media_server.exe 是 CUI，但它确实是合法 PE。策略（该不该是 GUI）
  // 归 scripts/pe-subsystem-verify.py；这里只能回答事实问题。
  const dir = tempDir();
  const gui = writePe(path.join(dir, 'gui.exe'), { subsystem: GUI_SUBSYSTEM });
  const cui = writePe(path.join(dir, 'cui.exe'), { subsystem: CUI_SUBSYSTEM });
  const bytes = fs.readFileSync(gui);
  const cuiBytes = fs.readFileSync(cui);
  assert.equal(isPeBinary(gui), true);
  assert.equal(isPeBinary(cui), true, 'CUI 是合法 PE，不能在这里判否');
  // 两个文件除 subsystem 字段的低字节外必须完全一致 —— 证明差异只来自策略字段，
  // 且该字段**不参与** PE 判定（若有人把 subsystem 检查塞进 isPeBinary，这条会红）。
  const differing = [...bytes.keys()].filter((i) => bytes[i] !== cuiBytes[i]);
  assert.deepEqual(differing, [DEFAULT_E_LFANEW + OPTIONAL_SUBSYSTEM_OFFSET]);
});

// --------------------------------------------------------------------------
// 生产可信门端到端：收紧后既不能假绿，也不能对真结构假红
// --------------------------------------------------------------------------
test('production trust rejects an oversized MZ-only externalBin', () => {
  const { input } = trustFixture({
    executable: (file) => {
      fs.writeFileSync(file, mzOnlyBytes(MIN_EXTERNAL_BIN_BYTES + 1024));
      return file;
    },
  });
  assert.equal(trustCode(input), 'SIDECAR_PACKAGE_TRUST_PE_HEADER');
});

test('production trust rejects an MZ-only native file even when oversized', () => {
  const { input } = trustFixture({ native: { 'liteav.dll': 'mz-only' } });
  assert.equal(trustCode(input), 'SIDECAR_PACKAGE_TRUST_PE_HEADER');
});

test('production trust accepts structurally valid externalBin and native closed set', () => {
  const { input } = trustFixture();
  assert.equal(trustCode(input), 'OK');
});

test('production trust accepts a CUI native file (subsystem is not its business)', () => {
  // 被剪除的那个媒体混流服务进程实为 CUI（subsystem = 3，见 sidecar-trust.js 的
  // INTENTIONALLY_ABSENT_NATIVE.measured），信任门必须放行它 —— 弹窗属性由 PE 门禁判。
  // 2026-09-20：它已不在 NATIVE_NAMES 里，所以这里必须换一个**仍在集合内**的名字来合
  // CUI 样本；否则 trustFixture 的 mutate 键会落空，这条用例会变成永远通过的空断言。
  const { input } = trustFixture({ native: { 'liteav.dll': 'cui' } });
  assert.equal(trustCode(input), 'OK');
});

// --------------------------------------------------------------------------
// 策略版本：让"策略变了"成为机械后果，而不是只对新构建生效
// --------------------------------------------------------------------------
test('production trust requires the provenance manifest (no silent skip when unwired)', () => {
  // 缺失 provenance 时必须**响亮失败**。若这里改成"没给就跳过比对"，
  // 那么任何一处忘了传 manifest 的调用点都会静默通过 —— 正是本文件要消灭的假绿。
  const { input } = trustFixture({ provenance: 'OMIT' });
  assert.equal(input.provenance, undefined);
  assert.equal(trustCode(input), 'SIDECAR_PACKAGE_TRUST_PROVENANCE_MISSING');
});

test('production trust rejects a generation whose declared trust policy version is stale', () => {
  const { input } = trustFixture({ provenance: { trust_version: '0.9.0' } });
  assert.equal(trustCode(input), 'SIDECAR_PACKAGE_TRUST_VERSION_MISMATCH');
});

test('production trust treats an absent trust_version as the pre-versioning baseline', () => {
  // 本机 current-installed 的 generation（2026-09-05 构建）manifest 里没有这个键。
  // 在"基线 == 当前策略版本"期间它们必须仍然通过；一旦策略版本被 bump
  // （例如 prune 落地那天），缺键的旧 generation 必须立刻失配 ⇒ 强制重建。
  // 下面是自适配断言：两种形态都被钉住，改版本的人没法不小心跳过这一步。
  const { input } = trustFixture({ provenance: {} });
  if (PRE_VERSIONING_TRUST_VERSION === TRUST_VERSION) {
    assert.equal(
      trustCode(input),
      'OK',
      '基线与当前策略版本相同期间，缺键的旧 generation 应当通过',
    );
    return;
  }
  assert.equal(
    trustCode(input),
    'SIDECAR_PACKAGE_TRUST_VERSION_MISMATCH',
    '策略版本已 bump：缺 trust_version 的旧 generation 必须失配，强制重建',
  );
});

// --------------------------------------------------------------------------
// 真机证据（house pattern：非 Windows 显式 skip，不静默通过）
// --------------------------------------------------------------------------
test('real shipped artifacts on this machine are structurally valid PEs', {
  skip: process.platform !== 'win32' ? 'windows-only PE evidence' : false,
}, () => {
  const candidates = [
    path.join(REPO_ROOT, 'pet-ui', 'src-tauri', 'target', 'release', 'jax-pet.exe'),
    path.join(REPO_ROOT, 'pet-ui', 'src-tauri', 'binaries', 'jax-rtc-sidecar.exe'),
  ].filter((file) => fs.existsSync(file));
  if (candidates.length === 0) return;
  for (const file of candidates) {
    assert.equal(isPeBinary(file), true, `${file} 是真 PE，不得被判否（假红）`);
  }
});
