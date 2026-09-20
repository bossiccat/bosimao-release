'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  APP_SOURCES,
  TARGET_TRIPLE,
  PackageError,
  createProvenance,
  expectedBundleResourceMap,
  verifyPackage,
} = require('../lib/sidecar-package');
const {
  INTENTIONALLY_ABSENT_NATIVE,
  NATIVE_NAMES: TRUST_NATIVE_NAMES,
  PRE_VERSIONING_TRUST_VERSION,
  TRUST_VERSION,
  assertProductionTrust,
} = require('../lib/sidecar-trust');
const { NATIVE_REQUIRED } = require('../lib/sidecar-package-common');
const { peBytes } = require('./pe-fixture');
const { RUNTIME_ARTIFACT_FILES, restoreWritableGeneration } = require('../lib/sidecar-runtime-immutable');
const {
  createCurrentPointer,
  createRuntimeLayout,
  finalizeStagedGeneration,
  generationIdForProvenance,
  publishCurrentPointer,
} = require('../lib/sidecar-runtime-publish');
const {
  PROJECT_ROOT,
  balancedItem,
  fieldType,
  hasSequence,
  listJavaScriptFiles,
  readProjectFile,
  rustConst,
  rustTokens,
} = require('./source-contract-helper');

const INSTALLED_BIN = 'jax-rtc-sidecar.exe';
const SHA_FILE = 'jax-rtc-sidecar.exe.sha256';
const PROVENANCE_FILE = 'jax-rtc-sidecar.provenance.json';
const PROVENANCE_DIGEST_FILE = 'jax-rtc-sidecar.provenance.sha256';
const GENERATION_METADATA_FILE = 'generation.json';
// 冻结字面量（第 5 份副本，刻意独立于生产清单，见 ADR-027）。
// 「刻意缺席」的成员不在这里 —— 它缺席是记录在案的决定，见
// scripts/lib/sidecar-trust.js 的 INTENTIONALLY_ABSENT_NATIVE。
const NATIVE_NAMES = [
  'trtc_electron_sdk.node',
  'liteav.dll',
  'txffmpeg.dll',
  'txsoundtouch.dll',
];

// 刻意缺席的成员名。单一真相源在生产侧那份记录里，这里只取名字 ——
// 本文件不重复写这个字面量，避免"两处字面量各说各话"。
const PRUNED_NATIVE = INTENTIONALLY_ABSENT_NATIVE[0].name;

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function sha256File(file) {
  return sha256(fs.readFileSync(file));
}

function listFiles(root, current = root) {
  const result = [];
  for (const entry of fs.readdirSync(current, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const item = path.join(current, entry.name);
    if (entry.isDirectory()) result.push(...listFiles(root, item));
    else if (entry.isFile()) result.push(path.relative(root, item).split(path.sep).join('/'));
  }
  return result;
}

function closedFileMap(root) {
  const map = {};
  for (const relative of listFiles(root)) map[relative] = sha256File(path.join(root, relative));
  return map;
}

function rustStrConst(relative, constName) {
  const source = readProjectFile(relative);
  const anchor = new RegExp(
    `^[ \\t]*const\\s+${constName}\\s*:\\s*&str\\s*=\\s*"([^"]*)"\\s*;`,
    'gm',
  );
  const found = [...source.matchAll(anchor)];
  assert.equal(
    found.length,
    1,
    `${relative} 里 ${constName} 的 &str 常量必须恰好一处（锚在声明行首，不含注释）`,
  );
  return found[0][1];
}

// 取出 JS / Rust 两侧某个数值常量的字面量值（十进制 Number）。
// 锚在声明行行首：这些常量名在注释里也出现过，锚全文就是"有形状没牙齿"（o018 教训）。
// `PE_SIGNATURE` / `PE_SIGNATURE_BYTES` 这类同前缀常量不会互相误匹配 ——
// 正则要求名字之后紧跟 `=`（JS）或 `:`（Rust）。
function numericConst(relative, constName, isRust) {
  const source = readProjectFile(relative);
  const pattern = isRust
    ? `^[ \\t]*const\\s+${constName}\\s*:\\s*(?:u8|u16|u32|u64|usize)\\s*=\\s*(0x[0-9a-fA-F_]+|[0-9_]+)\\s*;`
    : `^[ \\t]*const\\s+${constName}\\s*=\\s*(0x[0-9a-fA-F_]+|[0-9_]+)\\s*;`;
  const found = [...source.matchAll(new RegExp(pattern, 'gm'))];
  assert.equal(
    found.length,
    1,
    `${relative} 里 ${constName} 的常量声明必须恰好一处（锚在声明行首，不含注释）`,
  );
  return Number(found[0][1].replace(/_/g, ''));
}

// 取出 Rust 源文件里某个 `const <NAME>: [&str; N] = [ ... ];` 的字符串项。
// 锚定规则（刻意为之）：声明必须出现在行首（允许缩进，允许 `pub(crate) ` 可见性前缀）。
// 注释行以 // 或 //! 开头，永远匹配不到 `^[ \t]*(pub…)? const` —— 而这些文件名在注释里
// 也出现过，锚全文就是"有形状没牙齿"（同 o018 静态锁那次教训）。
function rustStrArray(relative, constName) {
  const source = readProjectFile(relative);
  const anchor = new RegExp(
    `^[ \\t]*(?:pub(?:\\([^)]*\\))?\\s+)?const\\s+${constName}\\s*:\\s*\\[\\s*&str\\s*;\\s*(\\d+)\\s*\\]\\s*=\\s*\\[`,
    'gm',
  );
  const found = [...source.matchAll(anchor)];
  assert.equal(
    found.length,
    1,
    `${relative} 里 ${constName} 的数组声明必须恰好一处（锚在声明行首，不含注释）`,
  );
  const declared = Number(found[0][1]);
  const start = found[0].index + found[0][0].length;
  const end = source.indexOf(']', start);
  assert.ok(end > start, `${relative} 里 ${constName} 的数组没有闭合`);
  const body = source.slice(start, end);
  assert.equal(body.includes('['), false, `${relative} 里 ${constName} 的数组体意外嵌套了 [`);
  const values = [...body.matchAll(/"([^"]*)"/g)].map((match) => match[1]);
  assert.equal(
    values.length,
    declared,
    `${relative} 里 ${constName} 声明 [&str; ${declared}] 但列了 ${values.length} 项`,
  );
  return values;
}

// 取出 JS 源文件里某个 `const <NAME> = [ ... ];` 的字符串项（锚在声明行行首，同 rustStrArray：
// 这些名字在注释里也出现过，锚全文就是"有形状没牙齿"）。
function jsStrArray(relative, constName) {
  const source = readProjectFile(relative);
  const found = [...source.matchAll(new RegExp(`^[ \\t]*const\\s+${constName}\\s*=\\s*\\[`, 'gm'))];
  assert.equal(
    found.length,
    1,
    `${relative} 里 ${constName} 的数组声明必须恰好一处（锚在声明行首，不含注释）`,
  );
  const start = found[0].index + found[0][0].length;
  const end = source.indexOf(']', start);
  assert.ok(end > start, `${relative} 里 ${constName} 的数组没有闭合`);
  return [...source.slice(start, end).matchAll(/['"]([^'"]*)['"]/g)].map((match) => match[1]);
}

// 测试侧独立构造 provenance manifest：不复用生产 createProvenance，
// 避免"expected hash 与校验路径同源"（ADR-027 测试完整性要求）。
function fixtureManifest(config, contentRoot) {
  const nativeFiles = NATIVE_NAMES.map(
    (name) => `resources/app/node_modules/trtc-electron-sdk/build/Release/${name}`,
  );
  const excluded = new Set([SHA_FILE, PROVENANCE_FILE, PROVENANCE_DIGEST_FILE]);
  const runtimeFiles = listFiles(contentRoot).filter((item) => !excluded.has(item));
  return {
    schema_version: 1,
    build_script_version: '1.0.0',
    // 2026-09-20：策略版本 bump 到 1.1.0 之后，缺这个键的 fixture 会被可信门判成
    // "版本化之前的基线" ⇒ SIDECAR_PACKAGE_TRUST_VERSION_MISMATCH，于是下面四条
    // 可信门用例会从"验体积 / 验 PE 结构"退化成"验版本"（隔离树实测：4 条全红，
    // 全部指向这一处缺键）。fixture 必须与**按当前策略构建出来的世代**同形，所以这里
    // 显式盖上当前版本；仍然刻意不复用生产 createProvenance（本文件的独立构造是
    // ADR-027 测试完整性要求）。
    trust_version: TRUST_VERSION,
    target_triple: TARGET_TRIPLE,
    electron_version: config.electronVersion,
    trtc_sdk_version: config.sdkVersion,
    sidecar_package_lock_sha256: config.sourceLockHash,
    external_bin: {
      build_input_file: path.basename(config.executable),
      installed_file: config.installedFile,
      target_triple: TARGET_TRIPLE,
      sha256: sha256File(path.join(contentRoot, config.installedFile)),
    },
    native_files: nativeFiles.map((item) => ({ path: item, sha256: sha256File(path.join(contentRoot, item)) })),
    runtime_files: runtimeFiles.map((item) => ({ path: item, sha256: sha256File(path.join(contentRoot, item)) })),
    bundle_resources: expectedBundleResourceMap(),
  };
}

// 构造 stable root + immutable generation + current.json 布局的 package fixture。
// options.mutateManifest 在写入 provenance 前改动 manifest（构造自洽但 schema 违规的 generation）；
// options.mutateStaging 在 finalize 前改动 staging 内容（构造 payload/元数据被污染的 generation）。
function fixture(options = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'jax-sidecar-package-'));
  const binDir = path.join(root, 'binaries');
  const runtime = path.join(binDir, 'jax-rtc-sidecar-runtime');
  const sidecarDir = path.join(root, 'sidecar');

  createRuntimeLayout(runtime);
  const stagingDir = fs.mkdtempSync(path.join(runtime, 'staging', 'pending-'));

  // Electron dist 桩（generation 根目录平铺）。
  fs.mkdirSync(path.join(stagingDir, 'locales'), { recursive: true });
  fs.writeFileSync(path.join(stagingDir, 'ffmpeg.dll'), 'electron-ffmpeg');
  fs.writeFileSync(path.join(stagingDir, 'resources.pak'), 'pak');
  fs.writeFileSync(path.join(stagingDir, 'icudtl.dat'), 'icu');
  fs.writeFileSync(path.join(stagingDir, 'v8_context_snapshot.bin'), 'snapshot');
  fs.writeFileSync(path.join(stagingDir, 'locales', 'en-US.pak'), 'locale');

  // installed 身份：generation 内固定名 jax-rtc-sidecar.exe。
  fs.writeFileSync(path.join(stagingDir, INSTALLED_BIN), 'electron-runtime-executable');

  // resources/app 与 TRTC native payload。
  const app = path.join(stagingDir, 'resources', 'app');
  const sdk = path.join(app, 'node_modules', 'trtc-electron-sdk');
  fs.mkdirSync(path.join(sdk, 'build', 'Release'), { recursive: true });
  fs.mkdirSync(path.join(sdk, 'liteav'), { recursive: true });
  fs.writeFileSync(path.join(app, 'main.js'), 'require("trtc-electron-sdk")');
  fs.writeFileSync(path.join(app, 'package.json'), JSON.stringify({ name: 'jax-rtc-sidecar' }));
  fs.writeFileSync(path.join(app, 'package-lock.json'), '{"lockfileVersion":3}');
  fs.writeFileSync(path.join(sdk, 'package.json'), JSON.stringify({ version: '13.4.802-beta.3' }));
  fs.writeFileSync(path.join(sdk, 'liteav', 'index.js'), 'module.exports={}');
  for (const name of NATIVE_NAMES) {
    fs.writeFileSync(path.join(sdk, 'build', 'Release', name), name);
  }

  // Tauri externalBin 构建输入（与 installed 字节一致，但命名带 target triple）。
  const executable = path.join(binDir, `jax-rtc-sidecar-${TARGET_TRIPLE}.exe`);
  fs.mkdirSync(binDir, { recursive: true });
  fs.writeFileSync(executable, 'electron-runtime-executable');

  // sidecar 生产源码白名单 + 源码锁。
  fs.mkdirSync(sidecarDir, { recursive: true });
  for (const relative of APP_SOURCES) {
    fs.writeFileSync(path.join(sidecarDir, relative), relative);
  }

  const config = {
    binDir,
    runtimeDir: runtime,
    executable,
    installedFile: INSTALLED_BIN,
    sidecarDir,
    sourceLockFile: path.join(sidecarDir, 'package-lock.json'),
    sourceLockHash: sha256File(path.join(sidecarDir, 'package-lock.json')),
    electronVersion: '31.7.7',
    sdkVersion: '13.4.802-beta.3',
  };

  // 组装 staging → finalize → publish pointer。
  const manifest = fixtureManifest(config, stagingDir);
  if (options.mutateManifest) options.mutateManifest(manifest);
  fs.writeFileSync(path.join(stagingDir, SHA_FILE), `${manifest.external_bin.sha256}\n`);
  fs.writeFileSync(path.join(stagingDir, PROVENANCE_FILE), `${JSON.stringify(manifest, null, 2)}\n`);
  fs.writeFileSync(path.join(stagingDir, PROVENANCE_DIGEST_FILE), `${sha256File(path.join(stagingDir, PROVENANCE_FILE))}\n`);
  if (options.mutateStaging) options.mutateStaging(stagingDir);
  const provenanceBytes = fs.readFileSync(path.join(stagingDir, PROVENANCE_FILE));
  finalizeStagedGeneration({ runtimeDir: runtime, stagingDir, provenanceBytes, expectedFiles: closedFileMap(stagingDir) });
  publishCurrentPointer({
    runtimeDir: runtime,
    pointer: createCurrentPointer({
      generation: generationIdForProvenance(provenanceBytes),
      manifestSha256: sha256(provenanceBytes),
    }),
  });

  const generation = generationIdForProvenance(provenanceBytes);
  const generationDir = path.join(runtime, 'generations', generation);
  return { root, config, generation, generationDir };
}

function expectCode(config, code) {
  assert.throws(() => verifyPackage(config), (error) => error instanceof PackageError && error.code === code);
}

function trustInput(config, generationDir) {
  return {
    executable: path.join(generationDir, INSTALLED_BIN),
    nativeDir: path.join(
      generationDir,
      'resources', 'app', 'node_modules', 'trtc-electron-sdk', 'build', 'Release',
    ),
    runtimeDir: generationDir,
    // 可信门要拿 selected generation 的 provenance 比对策略版本。
    // 缺了必须 fail-closed（sidecar-trust-pe.test.js 有专门用例钉这一点）。
    provenance: JSON.parse(fs.readFileSync(path.join(generationDir, PROVENANCE_FILE), 'utf8')),
  };
}

function expectTrustCode(config, generationDir, code) {
  assert.throws(
    () => assertProductionTrust(trustInput(config, generationDir)),
    (error) => error.code === code,
  );
}

// 篡改型测试在故意改写已 finalize fixture 前显式恢复写权限；生产 freeze 行为不在此处放宽。
function restoreFixtureForTamper(generationDir) {
  restoreWritableGeneration(generationDir);
}

test('package output is stable root + pointer + immutable generation with no flat fallback', () => {
  const { config, generation, generationDir } = fixture();

  assert.deepEqual(
    fs.readdirSync(config.runtimeDir).sort(),
    ['current.json', 'generations', 'leases', 'publish.lock', 'reader-gc.lock', 'staging'],
  );
  assert.match(generation, /^g-[0-9a-f]{64}$/);
  assert.equal(fs.statSync(generationDir).isDirectory(), true);
  assert.equal(fs.existsSync(path.join(generationDir, GENERATION_METADATA_FILE)), true);
  assert.equal(fs.existsSync(path.join(config.runtimeDir, INSTALLED_BIN)), false);
  assert.equal(fs.existsSync(path.join(config.runtimeDir, 'jax-rtc-sidecar.provenance.json')), false);

  const pointer = JSON.parse(fs.readFileSync(path.join(config.runtimeDir, 'current.json'), 'utf8'));
  assert.deepEqual(Object.keys(pointer).sort(), ['generation', 'manifest_sha256', 'schema_version']);
  assert.equal(pointer.generation, generation);

  const metadata = JSON.parse(fs.readFileSync(path.join(generationDir, GENERATION_METADATA_FILE), 'utf8'));
  assert.equal(metadata.generation, generation);
  assert.equal(metadata.manifest_sha256, pointer.manifest_sha256);
  assert.equal(metadata.files[INSTALLED_BIN], sha256File(path.join(generationDir, INSTALLED_BIN)));
  assert.equal(fs.existsSync(path.join(generationDir, PROVENANCE_FILE)), true);

  const manifest = verifyPackage(config);
  assert.equal(manifest.external_bin.installed_file, INSTALLED_BIN);
});

test('rejects missing externalBin build input', () => {
  const { config } = fixture();
  fs.rmSync(config.executable);
  expectCode(config, 'SIDECAR_PACKAGE_EXTERNAL_BIN_MISSING');
});

test('rejects empty, non-lowercase and mismatched installed binary hash', () => {
  for (const [value, code] of [
    ['', 'SIDECAR_PACKAGE_HASH_INVALID'],
    ['A'.repeat(64), 'SIDECAR_PACKAGE_HASH_INVALID'],
    ['0'.repeat(64), 'SIDECAR_PACKAGE_HASH_MISMATCH'],
  ]) {
    const { config } = fixture({
      mutateStaging: (staging) => fs.writeFileSync(path.join(staging, SHA_FILE), value),
    });
    expectCode(config, code);
  }
});

test('rejects source lock hash drift', () => {
  const { config } = fixture();
  config.sourceLockHash = '0'.repeat(64);
  expectCode(config, 'SIDECAR_PACKAGE_LOCK_MISMATCH');
});

test('rejects SDK version drift', () => {
  const { config } = fixture();
  config.sdkVersion = '0.0.0';
  expectCode(config, 'SIDECAR_PACKAGE_SDK_VERSION_MISMATCH');
});

test('rejects missing TRTC native dependency', () => {
  const { config } = fixture({
    mutateStaging: (staging) => fs.rmSync(path.join(staging, 'resources', 'app', 'node_modules', 'trtc-electron-sdk', 'build', 'Release', 'txsoundtouch.dll')),
  });
  expectCode(config, 'SIDECAR_PACKAGE_NATIVE_MISSING');
});

test('rejects Electron devDependency embedded in resources/app', () => {
  const { config, generationDir } = fixture();
  restoreFixtureForTamper(generationDir);
  fs.mkdirSync(path.join(generationDir, 'resources', 'app', 'node_modules', 'electron'));
  expectCode(config, 'SIDECAR_PACKAGE_DEV_DEPENDENCY_EMBEDDED');
});

test('changing a selected generation payload fails verification', () => {
  const { config, generationDir } = fixture();
  restoreFixtureForTamper(generationDir);
  fs.writeFileSync(path.join(generationDir, 'resources.pak'), 'tampered');
  assert.throws(() => verifyPackage(config), (error) => error instanceof PackageError);
});

test('rejects runtime closed-set additions and omissions', () => {
  const added = fixture();
  restoreFixtureForTamper(added.generationDir);
  fs.writeFileSync(path.join(added.generationDir, 'unrecorded.dll'), 'unrecorded');
  assert.throws(() => verifyPackage(added.config), (error) => error instanceof PackageError);

  const omitted = fixture();
  restoreFixtureForTamper(omitted.generationDir);
  const manifest = JSON.parse(fs.readFileSync(path.join(omitted.generationDir, PROVENANCE_FILE), 'utf8'));
  manifest.runtime_files = manifest.runtime_files.filter((item) => item.path !== 'resources.pak');
  fs.writeFileSync(path.join(omitted.generationDir, PROVENANCE_FILE), JSON.stringify(manifest));
  assert.throws(() => verifyPackage(omitted.config), (error) => error instanceof PackageError);
});

test('a Chromium runtime artifact is not part of the payload closed set', () => {
  // Chromium 在 CWD（= generation 根）写顶层 debug.log（registration_protocol_win.cc 等
  // 内部诊断），**每次启动都在追加**，不受 JAX_SIDECAR_LOG_DIR 控制。Rust 侧自 RP-07
  // （2026-09-02，v4k/v4m 实测）起已豁免它，构建侧却一处都没有跟上 ⇒ 它被哈希进
  // runtime_files 与 generation.json，而运行期一追加就与声明的哈希必然分叉：
  //   · 构建期 --verify-only → SIDECAR_PACKAGE_RUNTIME_MISMATCH
  //   · 世代解析 → finalized payload hash mismatch
  // 本机现役世代实测就是这个形态（清单声明 70aa1d9b… / 实测 78cf15e5…）。
  const stagingTimeHash = sha256('staging-time-debug-log');
  const { config, generationDir } = fixture({
    mutateManifest: (manifest) => {
      manifest.runtime_files.push({ path: 'debug.log', sha256: stagingTimeHash });
    },
  });

  // 复刻"构建侧未豁免时产出的世代"：manifest 与 generation.json 两侧都声明它，
  // 而它的内容由运行期决定（此处即运行期追加后的形态）。
  restoreFixtureForTamper(generationDir);
  fs.writeFileSync(path.join(generationDir, 'debug.log'), 'runtime-appended');
  const metadata = JSON.parse(fs.readFileSync(path.join(generationDir, GENERATION_METADATA_FILE), 'utf8'));
  metadata.files['debug.log'] = stagingTimeHash;
  fs.writeFileSync(path.join(generationDir, GENERATION_METADATA_FILE), JSON.stringify(metadata));

  // 核心断言：内容变了也必须仍然通过 —— 这就是用户机器上每次启动之后的状态。
  assert.doesNotThrow(() => verifyPackage(config));

  // 反向对照：不能靠"把整个闭集判据放宽"来过 —— 其余未登记文件仍必须被判否。
  fs.writeFileSync(path.join(generationDir, 'runtime-noise.dll'), 'unrecorded');
  assert.throws(
    () => verifyPackage(config),
    (error) => error instanceof PackageError,
    '豁免只能窄到运行期产物这一个名字，闭集对其余路径必须照旧闭合',
  );

  // 新构建的 manifest 不该再声明它（否则每产出一个世代就重演一次上面的分叉）。
  assert.deepEqual(
    createProvenance(config, generationDir).runtime_files.filter((item) => item.path === 'debug.log'),
    [],
    'debug.log 是运行期可再生产物，不该进 provenance 的哈希覆盖集',
  );
});

test('the build prunes Chromium runtime artifacts out of staging instead of packing them', () => {
  // 补丁不止"不哈希"：构建机的 debug.log 里含构建机本地路径，把它装进客户包本身就不该
  // 发生（而且它每次运行都被改写，等于给载体发一份注定过期的哈希）。
  // 两向 fail-closed：源里有而 staging 里没有 ⇒ 拷贝不完整（本文件有过静默半拷贝的
  // 历史）；删完仍在 ⇒ 删除失败。两侧都不得静默继续打包。
  const { pruneRuntimeArtifacts } = require('../lib/sidecar-package-build');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'jax-sidecar-prune-'));
  const source = path.join(root, 'dist');
  const staging = path.join(root, 'staging');
  fs.mkdirSync(source, { recursive: true });
  fs.mkdirSync(staging, { recursive: true });
  const fail = (code) => { throw new PackageError(code); };

  // 源里没有（构建机从未跑过 electron）⇒ 什么都不删，也不报错。
  pruneRuntimeArtifacts(source, staging, fail);

  // 源里有、staging 里也有 ⇒ 必须删掉。
  fs.writeFileSync(path.join(source, 'debug.log'), 'build-machine-local-path');
  fs.writeFileSync(path.join(staging, 'debug.log'), 'build-machine-local-path');
  pruneRuntimeArtifacts(source, staging, fail);
  assert.equal(fs.existsSync(path.join(staging, 'debug.log')), false);

  // 源里有而 staging 里没有 ⇒ 拷贝不完整。
  assert.throws(
    () => pruneRuntimeArtifacts(source, staging, fail),
    (error) => error instanceof PackageError
      && error.code === 'SIDECAR_PACKAGE_RUNTIME_ARTIFACT_COPY_INCOMPLETE',
  );

  // 删不掉（此处用同名目录模拟）⇒ 不得带着它继续打包。
  fs.mkdirSync(path.join(staging, 'debug.log'));
  assert.throws(
    () => pruneRuntimeArtifacts(source, staging, fail),
    (error) => error instanceof PackageError
      && error.code === 'SIDECAR_PACKAGE_RUNTIME_ARTIFACT_PRUNE_FAILED',
  );
});

test('rejects duplicate, traversal and absolute manifest paths', () => {
  for (const mutate of [
    (manifest) => manifest.runtime_files.push({ ...manifest.runtime_files[0] }),
    (manifest) => { manifest.runtime_files[0].path = '../escape.dll'; },
    (manifest) => { manifest.runtime_files[0].path = 'C:/escape.dll'; },
  ]) {
    const { config } = fixture({ mutateManifest: mutate });
    expectCode(config, 'SIDECAR_PACKAGE_MANIFEST_PATH_INVALID');
  }
});

test('rejects strict manifest schema drift', () => {
  const { config } = fixture({ mutateManifest: (manifest) => { manifest.untrusted_extension = true; } });
  expectCode(config, 'SIDECAR_PACKAGE_MANIFEST_SCHEMA_INVALID');
});

test('requires the exact four native paths to match runtime entries', () => {
  const valid = fixture();
  const verified = verifyPackage(valid.config);
  const runtime = new Map(verified.runtime_files.map((item) => [item.path, item.sha256]));
  for (const native of verified.native_files) {
    assert.equal(runtime.get(native.path), native.sha256);
  }

  const drifted = fixture({ mutateManifest: (manifest) => { manifest.native_files[0].sha256 = '0'.repeat(64); } });
  expectCode(drifted.config, 'SIDECAR_PACKAGE_NATIVE_SUBSET_MISMATCH');

  const extra = fixture({ mutateManifest: (manifest) => { manifest.native_files.push(manifest.runtime_files.find((item) => item.path === 'resources.pak')); } });
  expectCode(extra.config, 'SIDECAR_PACKAGE_NATIVE_SET_MISMATCH');
});

test('distinguishes build input and installed externalBin names', () => {
  const { config } = fixture();
  const manifest = verifyPackage(config);
  assert.equal(manifest.external_bin.build_input_file, `jax-rtc-sidecar-${TARGET_TRIPLE}.exe`);
  assert.equal(manifest.external_bin.installed_file, INSTALLED_BIN);
});

test('a target-triple filename cannot substitute the installed jax-rtc-sidecar.exe identity', () => {
  const { config, generationDir } = fixture();
  restoreFixtureForTamper(generationDir);
  const installed = path.join(generationDir, INSTALLED_BIN);
  const substituted = path.join(generationDir, `jax-rtc-sidecar-${TARGET_TRIPLE}.exe`);
  fs.renameSync(installed, substituted);
  assert.throws(() => verifyPackage(config), (error) => error instanceof PackageError);

  // 反向：即便同时存在 target-triple 副本，合法 installed 身份仍必须存在；
  // 仅有三元组命名文件、缺失 jax-rtc-sidecar.exe 时必须失败。
  const partial = fixture();
  restoreFixtureForTamper(partial.generationDir);
  fs.rmSync(path.join(partial.generationDir, INSTALLED_BIN));
  assert.throws(() => verifyPackage(partial.config), (error) => error instanceof PackageError);
});

test('requires the dedicated runtime bundle resource destination', () => {
  const { config } = fixture({
    mutateManifest: (manifest) => { manifest.bundle_resources['binaries/jax-rtc-sidecar-runtime/'] = ''; },
  });
  expectCode(config, 'SIDECAR_PACKAGE_MANIFEST_SCHEMA_INVALID');
});

test('ignores unmanaged installer siblings but rejects managed runtime additions', () => {
  const { config, generationDir } = fixture();
  fs.writeFileSync(path.join(config.binDir, 'jax-pet.exe'), 'tauri-main');
  fs.writeFileSync(path.join(config.binDir, INSTALLED_BIN), 'installed-sidecar');
  verifyPackage(config);
  restoreFixtureForTamper(generationDir);
  fs.writeFileSync(path.join(generationDir, 'unrecorded-runtime.dll'), 'unrecorded');
  assert.throws(() => verifyPackage(config), (error) => error instanceof PackageError);
});

test('rejects sidecar production source whitelist drift', () => {
  const { root, config } = fixture();
  config.sidecarDir = path.join(root, 'sidecar-source');
  fs.mkdirSync(config.sidecarDir, { recursive: true });
  for (const relative of APP_SOURCES) {
    fs.writeFileSync(path.join(config.sidecarDir, relative), relative);
  }
  fs.writeFileSync(path.join(config.sidecarDir, 'unexpected.js'), 'module.exports={}');
  expectCode(config, 'SIDECAR_PACKAGE_APP_SOURCE_SET_MISMATCH');
});

test('APP_SOURCES matches the real sidecar/ top-level source set', () => {
  // 上面那条漂移用例是**自适应**的：它按 APP_SOURCES 造目录再塞一个多余文件，
  // 所以它永远发现不了 APP_SOURCES 与真实 sidecar/ 不一致。
  // 2026-09-19 实测代价：adev.js / downlink_pacer.js / resample.js 三个被随包模块
  // require 的文件（rtc.js:15 / rtc.js:28 / audio.js:19）漏登记了一周多，
  // 期间 `sidecar-verify` 锁在 HEAD 上一直是红的（SIDECAR_PACKAGE_APP_SOURCE_SET_MISMATCH），
  // 发布路径整体被阻断而无人察觉。这条锁就是那次漂移的直接产物。
  const sidecarDir = path.join(PROJECT_ROOT, 'sidecar');
  const actual = fs.readdirSync(sidecarDir, { withFileTypes: true })
    .filter((entry) => entry.isFile() && (entry.name.endsWith('.js') || entry.name === 'index.html'
      || entry.name === 'package.json' || entry.name === 'package-lock.json'))
    .map((entry) => entry.name)
    .sort();
  assert.deepEqual(
    [...APP_SOURCES].sort(),
    actual,
    'sidecar/ 顶层源码集与 APP_SOURCES 必须一致：漏登记会让随包 app 运行期 require 失败，'
    + '多登记不存在的会让 build 在 SIDECAR_PACKAGE_APP_SOURCE_MISSING 中止',
  );
});

test('the native closed set is one set across every production copy', () => {
  // 同一份"原生集 4 个名字"在本仓有 4 份**生产**副本 + 2 份**测试**副本：
  //   1. scripts/lib/sidecar-trust.js                NATIVE_NAMES     构建期可信门
  //   2. scripts/lib/sidecar-package-common.js       NATIVE_REQUIRED  provenance 哈希覆盖集
  //   3. pet-ui/src-tauri/src/sidecar_integrity.rs   REQUIRED         启动期，全路径 + 精确集合相等
  //   4. pet-ui/src-tauri/src/sidecar_runtime_trust.rs NATIVE_NAMES   启动期
  //   5. 本文件的 NATIVE_NAMES 字面量（fixture 构造，刻意独立，见 ADR-027）
  //   6. pet-ui/src-tauri/tests/support.rs           NATIVE_NAMES     集成测试 fixture
  // 2026-09-20 起原生集从 5 件变 4 件（媒体混流服务进程被剪除，见 INTENTIONALLY_ABSENT_NATIVE）。
  // 第 5、6 份不是生产清单，但同样必须同集合 —— 否则它们复现的是一个**已经不存在的包形态**：
  //   第 5 份由本文件下面的 deepEqual 直接钉住（它构造 verifyPackage 要读的包）；
  //   第 6 份此前**不在任何锁的视野内**：它构造的 manifest 会被喂给 validate_runtime
  //   （sidecar.rs 的 validate_for_launch → validate_native_subset 的**精确集合相等**），
  //   而 cargo 未被任何 workflow 跑过，所以它是在 CI 上拦住"改了生产清单忘了改 fixture"
  //   的唯一去处，故一并纳入本锁。
  // 派生副本（scripts/create-sidecar-runtime-fixture.js、sidecar_integrity.rs 的测试 fixture）
  // 另由 "the pruned native is a recorded decision…" 那条反向锁钉住。
  // 前 4 份全在生产路径上，此前**互无锁**，而且是跨语言（JS ↔ Rust）、跨进程
  // （构建期 ↔ 应用启动期）。这类漂移刚咬过一次（APP_SOURCES，见上一条）；
  // 这次更糟：没有任何测试同时看它们,所以"改了 JS 忘了改 Rust"只会表现为
  // 装机后 sidecar 拒绝 spawn（ManifestInvalid / RuntimeUntrusted），
  // 在 CI 上完全不可见。下面把 5 份钉成同一集合。
  const productionPrefix = 'resources/app/node_modules/trtc-electron-sdk/build/Release/';
  const frozen = [...NATIVE_NAMES].sort();
  assert.equal(frozen.length, 4, '冻结字面量必须是 4 项；增删原生集必须同步改这一条与全部副本');

  assert.deepEqual(
    [...TRUST_NATIVE_NAMES].sort(),
    frozen,
    'scripts/lib/sidecar-trust.js 的 NATIVE_NAMES 与冻结集合不一致（构建期可信门）',
  );
  assert.deepEqual(
    [...NATIVE_REQUIRED].sort(),
    frozen,
    'scripts/lib/sidecar-package-common.js 的 NATIVE_REQUIRED 与冻结集合不一致'
    + '（它是 provenance native_files 的哈希覆盖集：少一个名字等于少一处完整性覆盖）',
  );

  const integrityPaths = rustStrArray('pet-ui/src-tauri/src/sidecar_integrity.rs', 'REQUIRED');
  for (const item of integrityPaths) {
    assert.equal(
      item.startsWith(productionPrefix),
      true,
      `sidecar_integrity.rs 的 REQUIRED 项必须是 ${productionPrefix} 下的全路径，实为 ${item}`,
    );
  }
  assert.deepEqual(
    integrityPaths.map((item) => path.posix.basename(item)).sort(),
    frozen,
    'pet-ui/src-tauri/src/sidecar_integrity.rs 的 REQUIRED 与冻结集合不一致（启动期精确集合相等）',
  );

  assert.deepEqual(
    [...rustStrArray('pet-ui/src-tauri/src/sidecar_runtime_trust.rs', 'NATIVE_NAMES')].sort(),
    frozen,
    'pet-ui/src-tauri/src/sidecar_runtime_trust.rs 的 NATIVE_NAMES 与冻结集合不一致（启动期可信门）',
  );

  // 2026-09-20：第 6 份副本 —— `pet-ui/src-tauri/tests/support.rs` 的 fixture NATIVE_NAMES。
  // 它此前不在这条锁的视野内，而它构造的正是喂给 validate_runtime 的 manifest。
  // 不锁的后果是"生产清单剪了一件、fixture 还照旧声明 5 件"：validate_native_subset 是
  // **精确集合相等**，于是 fixture 直接被判 ManifestInvalid，而 cargo 没有任何 workflow 跑，
  // 这件事只会在有人手动 cargo test 时才暴露 —— 正是本锁存在的理由。
  assert.deepEqual(
    [...rustStrArray('pet-ui/src-tauri/tests/support.rs', 'NATIVE_NAMES')].sort(),
    frozen,
    'pet-ui/src-tauri/tests/support.rs 的 fixture NATIVE_NAMES 与冻结集合不一致'
    + '（它的 manifest 要过 validate_native_subset 的精确集合相等：多一件少一件都判否）',
  );

  // 策略版本常量同样是跨语言双份（构建期 JS ↔ 启动期 Rust）。不钉住的话，
  // "JS 接受、Rust 拒绝 spawn"（或反之）只会在装机后才暴露。
  assert.equal(
    rustStrConst('pet-ui/src-tauri/src/sidecar_integrity.rs', 'TRUST_VERSION'),
    TRUST_VERSION,
    'Rust 启动期门禁的 TRUST_VERSION 必须与 scripts/lib/sidecar-trust.js 的一致',
  );
  assert.equal(
    rustStrConst('pet-ui/src-tauri/src/sidecar_integrity.rs', 'PRE_VERSIONING_TRUST_VERSION'),
    PRE_VERSIONING_TRUST_VERSION,
    'Rust 的版本化之前基线必须与 scripts/lib/sidecar-trust.js 的一致',
  );
  // 同上，策略版本也有第 3 份副本：tests/support.rs 的 fixture manifest 要显式盖上它。
  // 不盖上就是"fixture 声明的是按旧策略构建的世代"—— bump 之后它会被可信门判成
  // 版本化之前的基线（SIDECAR_PACKAGE_TRUST_VERSION_MISMATCH），
  // 于是 support.rs 的 fixture 全家（supervisor / hash / credential / debug-log）
  // 都会从"验它们本来要验的东西"退化成"验版本"。
  assert.equal(
    rustStrConst('pet-ui/src-tauri/tests/support.rs', 'TRUST_VERSION'),
    TRUST_VERSION,
    'pet-ui/src-tauri/tests/support.rs 的 fixture 策略版本必须与 scripts/lib/sidecar-trust.js 的一致',
  );
});

test('the runtime artifact exemption is one set across both languages', () => {
  // 「运行期可再生产物不进闭集」这条规则此前**只存在于 Rust 侧**（RP-07 起 3 处裸字面量），
  // 构建侧一处都没有 ⇒ 两侧对"闭集"的定义分叉：启动期放行、构建期判否。这不是假想：
  // 本机现役世代正是"清单声明 70aa1d9b… / 实测 78cf15e5…"的形态，同一个目录上 Rust 与
  // 构建侧给出相反结论（这也是长年「反复弹窗」的结构性来源之一）。
  // JS 侧 3 处（provenance 闭集 / 校验器的 actual+declared 两侧 / 指针协议 walk+expected）
  // 与 Rust 侧 3 处（list_runtime_files / validate_runtime 的 expected 侧 /
  // walk_generation_payload 的 walk 与 resolve 的 expected）必须引用同一份名单。
  //
  // 覆盖边界：本锁钉**名字集合**与"每个豁免点都引用共享常量"，钉不住"是否又新增了第 4 处
  // 豁免点"——那是任何静态锁都够不到的。Rust 侧的顶层 logs/ 前缀规则不在本锁的名字集合内：
  // JS 侧当前**没有**对应规则，属既有不对称（已单列，不在本次改动范围）。
  const frozen = ['debug.log'];
  assert.deepEqual(
    [...RUNTIME_ARTIFACT_FILES].sort(),
    frozen,
    'JS 侧名单变了：改这里必须同时改 Rust 常量与两侧全部豁免点',
  );
  assert.deepEqual(
    jsStrArray('scripts/lib/sidecar-runtime-immutable.js', 'RUNTIME_ARTIFACT_FILES').sort(),
    frozen,
    'JS 源码里声明的名单与运行时导出的不一致（说明锚到别处去了）',
  );
  assert.deepEqual(
    [...rustStrArray('pet-ui/src-tauri/src/sidecar_integrity.rs', 'RUNTIME_ARTIFACT_FILES')].sort(),
    frozen,
    'Rust 侧 RUNTIME_ARTIFACT_FILES 与 JS 侧不一致 ⇒ 构建期放行、启动期拒绝（或反之）',
  );

  // 每个豁免点都必须**引用**该常量：裸字面量各自为政，改一处不会带动另一处。
  for (const [relative, symbol] of [
    ['pet-ui/src-tauri/src/sidecar_integrity.rs', 'list_runtime_files'],
    ['pet-ui/src-tauri/src/sidecar_integrity.rs', 'validate_runtime'],
    ['pet-ui/src-tauri/src/sidecar_runtime_pointer.rs', 'walk_generation_payload'],
    ['pet-ui/src-tauri/src/sidecar_runtime_pointer.rs', 'resolve_sidecar_runtime'],
  ]) {
    const body = balancedItem(rustTokens(readProjectFile(relative)), ['fn', symbol]);
    assert.equal(
      body.includes('RUNTIME_ARTIFACT_FILES'),
      true,
      `${relative} 的 ${symbol} 没有引用 RUNTIME_ARTIFACT_FILES：退回裸字面量就会单侧漂移`,
    );
  }

  // JS 侧同样钉住"引用而非硬编码"：豁免点必须来自共享名单。
  const verifySource = readProjectFile('scripts/lib/sidecar-package-verify.js');
  assert.equal(
    verifySource.includes('RUNTIME_ARTIFACT_FILES'),
    true,
    '校验器必须引用共享名单；把豁免写成字面量会让两侧再次分叉',
  );
});

test('the Rust startup PE judgement is the same structural judgement as the JS one', () => {
  // `sidecar_runtime_trust.rs` 的头注释声称「与 scripts/lib/sidecar-trust.js 同一策略」。
  // 2026-09-19 实测那句话一度是**假的**：Rust 侧 `is_pe_binary` 只判 2 字节 `MZ`，
  // 而 JS 侧已在 3e221ba 收紧为真结构校验。它跑在**每次启动、全部客户机**上，
  // 比构建期那条门更险 —— 而两侧是跨语言独立实现，各自的测试只钉自己，
  // "改了一侧忘了另一侧"没有任何测试能发现（与上一条 native 集锁同因）。
  //
  // 本条钉的是**判据形状 + 常量值**，不是行为等价：Rust 侧的行为牙齿由它自己的
  // `pe_structure_tests` 提供（`MZ` + 40,000 字节填充、`MZ` + 5MB 零填充必须判否）。
  // 写注释声称"同策略"是不算数的 —— 这条锁才是那句声明的证据。
  for (const [name, hex] of [
    ['PE_SIGNATURE', '00004550'],
    ['OPTIONAL_MAGIC_PE32', '10b'],
    ['OPTIONAL_MAGIC_PE32_PLUS', '20b'],
    ['E_LFANEW_OFFSET', '3c'],
  ]) {
    const expected = Number.parseInt(hex, 16);
    assert.equal(
      numericConst('scripts/lib/sidecar-trust.js', name, false),
      expected,
      `JS 侧 ${name} 的取值变了：两侧判据必须同口径（构建期门禁）`,
    );
    assert.equal(
      numericConst('pet-ui/src-tauri/src/sidecar_runtime_trust.rs', name, true),
      expected,
      `Rust 侧 ${name} 与 JS 侧不一致 ⇒ 构建期放行、启动期拒绝 spawn（或反之）`,
    );
  }

  // 结构四步必须都在 `is_pe_binary` **函数体内**被引用：
  // MZ → e_lfanew → "PE\0\0" → OptionalHeader.Magic。
  // 用 token 级（去注释）函数体并锚在函数声明上：这些符号在头注释里也出现过。
  const body = balancedItem(
    rustTokens(readProjectFile('pet-ui/src-tauri/src/sidecar_runtime_trust.rs')),
    ['fn', 'is_pe_binary', '(', 'path', ':', '&', 'Path', ')'],
  );
  for (const symbol of [
    'E_LFANEW_OFFSET',
    'PE_SIGNATURE',
    'OPTIONAL_MAGIC_PE32',
    'OPTIONAL_MAGIC_PE32_PLUS',
  ]) {
    assert.equal(
      body.includes(symbol),
      true,
      `is_pe_binary 里没有引用 ${symbol}：判据一旦退回"只验魔数"就不会再引用它`,
    );
  }
  // 反向：判据里不得出现 subsystem —— 该不该是 GUI 是**策略**，归 pe-subsystem-verify.py。
  assert.equal(
    body.includes('subsystem'),
    false,
    'subsystem 不属于"是不是 PE"的事实判据（本机 5 件原生产物里有 4 件是 CUI，'
    + '把它们判否等于用策略判错去关掉客户机启动）',
  );
});

test('resource mapping preserves the dedicated runtime directory contract end to end', () => {
  // RP-07 (2026-08-31): 安装目的地解耦缩短为 jrt/（NSIS 3.11 解压端 260 上限，
  // 完整名最长 269 字符会静默丢文件）；源目录保持规范名 jax-rtc-sidecar-runtime/。
  const destination = 'jrt/';
  assert.deepEqual(expectedBundleResourceMap(), {
    'binaries/jax-rtc-sidecar-runtime/': destination,
  });

  const tauri = JSON.parse(readProjectFile('pet-ui/src-tauri/tauri.conf.json'));
  // de08042 (2026-09-03): bundle.resources 窄化为 current.json + generations/
  // （排除 staging/pending-* 与 leases/locks，规避 tauri-build 全量拷贝在
  // node_modules 上的非确定性 os error 5 与 makensis 超长路径放大）；
  // 安装目的地仍为 jrt/。certs/ca.crt 是独立的 Tauri 资源，不属于
  // sidecar manifest 的 bundle_resources（下方 manifest 断言仍用旧 map，两层契约独立）。
  assert.deepEqual(tauri.bundle.resources, {
    'binaries/jax-rtc-sidecar-runtime/current.json': 'jrt/current.json',
    'binaries/jax-rtc-sidecar-runtime/generations/': 'jrt/generations/',
    'certs/ca.crt': 'certs/ca.crt',
  });

  const mainSource = readProjectFile('pet-ui/src-tauri/src/main.rs');
  assert.equal(rustConst(mainSource, 'SIDECAR_RUNTIME_DIR'), destination.replace(/\/$/, ''));

  // installed 身份是固定名 jax-rtc-sidecar.exe，resolver 绝不接受 triple 命名替代。
  const resolver = readProjectFile('pet-ui/src-tauri/src/sidecar_runtime_pointer.rs');
  assert.equal(resolver.includes('jax-rtc-sidecar.exe'), true);
  assert.equal(resolver.includes(`jax-rtc-sidecar-${TARGET_TRIPLE}.exe`), false);

  const { config, generation } = fixture();
  const manifest = verifyPackage(config);
  assert.deepEqual(manifest.bundle_resources, expectedBundleResourceMap());
  // 源目录名保持规范名（与安装目的地解耦）。
  assert.equal(path.basename(config.runtimeDir), 'jax-rtc-sidecar-runtime');
  assert.equal(fs.existsSync(path.join(config.runtimeDir, 'generations', generation)), true);
});

test('verify rejects tampered externalBin identity fields', async (t) => {
  for (const [field, value, code] of [
    ['build_input_file', 'attacker-x86_64-pc-windows-msvc.exe', 'SIDECAR_PACKAGE_EXTERNAL_BIN_IDENTITY_MISMATCH'],
    ['installed_file', 'attacker.exe', 'SIDECAR_PACKAGE_EXTERNAL_BIN_IDENTITY_MISMATCH'],
    ['target_triple', 'aarch64-pc-windows-msvc', 'SIDECAR_PACKAGE_EXTERNAL_BIN_IDENTITY_MISMATCH'],
  ]) {
    await t.test(field, () => {
      const { config } = fixture({ mutateManifest: (manifest) => { manifest.external_bin[field] = value; } });
      expectCode(config, code);
    });
  }
});

test('build.rs rerun rules watch current.json, selected generation and package inputs', () => {
  const buildRs = readProjectFile('pet-ui/src-tauri/build.rs');
  // 不再监视废弃的 flat root-level 清单路径。
  assert.equal(buildRs.includes('binaries/jax-rtc-sidecar-runtime/jax-rtc-sidecar.provenance.json'), false);
  assert.equal(buildRs.includes('binaries/jax-rtc-sidecar-runtime/jax-rtc-sidecar.exe.sha256'), false);
  // 监视 pointer 与 generation 输入。
  assert.equal(buildRs.includes('current.json'), true);
  assert.equal(buildRs.includes('generations'), true);
  assert.equal(buildRs.includes('rerun-if-changed'), true);
  // 监视 package 输入（含 Task 6 新增的 build 编排与协议层）。
  assert.equal(buildRs.includes('sidecar-package-build.js'), true);
  assert.equal(buildRs.includes('sidecar-runtime-publish.js'), true);
});

test('build.rs release manifest digest derives from pointer-selected provenance bytes', () => {
  const buildRs = readProjectFile('pet-ui/src-tauri/build.rs');
  // 不读废弃的 flat root-level 清单；必须经 current.json 解析 selected generation。
  assert.equal(buildRs.includes('binaries/jax-rtc-sidecar-runtime/jax-rtc-sidecar.provenance.json'), false);
  assert.equal(buildRs.includes('current.json'), true);
  assert.equal(buildRs.includes('jax-rtc-sidecar.provenance.json'), true);
  assert.equal(buildRs.includes('JAX_SIDECAR_MANIFEST_SHA256'), true);
});

test('production SidecarSpec cannot disable provenance integrity validation', () => {
  const sidecarTokens = rustTokens(readProjectFile('pet-ui/src-tauri/src/sidecar.rs'));
  const spec = balancedItem(sidecarTokens, ['pub', 'struct', 'SidecarSpec']);
  assert.equal(fieldType(spec, 'integrity'), 'IntegritySpec');

  const integrityTokens = rustTokens(readProjectFile('pet-ui/src-tauri/src/sidecar_integrity.rs'));
  const validateRuntime = balancedItem(integrityTokens, ['pub', '(', 'crate', ')', 'fn', 'validate_runtime']);
  assert.equal(
    hasSequence(validateRuntime, ['else', '{', 'return', 'Ok', '(', ')', ';', '}']),
    false,
    'validate_runtime must not accept missing integrity metadata',
  );
});

test('authored operational JavaScript modules stay within 300 lines', () => {
  const scriptsRoot = path.join(PROJECT_ROOT, 'scripts');
  const overLimit = listJavaScriptFiles(scriptsRoot)
    .filter((relative) => !relative.startsWith(`test${path.sep}`))
    .map((relative) => ({
      relative: relative.split(path.sep).join('/'),
      lines: fs.readFileSync(path.join(scriptsRoot, relative), 'utf8').split(/\r?\n/).length,
    }))
    .filter(({ lines }) => lines > 300);
  assert.deepEqual(overLimit, []);
});

test('production trust rejects tiny externalBin accepted by self-consistency verify', () => {
  const { config, generationDir } = fixture();
  verifyPackage(config);
  expectTrustCode(config, generationDir, 'SIDECAR_PACKAGE_TRUST_MIN_SIZE');
});

test('production trust rejects tiny native runtime files accepted by self-consistency verify', () => {
  const { config, generationDir } = fixture();
  verifyPackage(config);
  restoreFixtureForTamper(generationDir);
  // 2026-09-19: 桩必须是**结构合法**的 PE。此前这里是 "MZ + 零填充"，靠
  // isPeBinary 只读 2 字节魔数才过；收紧后那种桩会被正确地判为 PE_HEADER，
  // 从而把"native 太小应报 MIN_SIZE"这条断言挤掉（变成假红）。
  fs.writeFileSync(
    path.join(generationDir, INSTALLED_BIN),
    peBytes({ size: 5 * 1024 * 1024 }),
  );
  expectTrustCode(config, generationDir, 'SIDECAR_PACKAGE_TRUST_MIN_SIZE');
});

test('production trust rejects oversized non-PE binary without MZ header', () => {
  const { config, generationDir } = fixture();
  restoreFixtureForTamper(generationDir);
  fs.writeFileSync(path.join(generationDir, INSTALLED_BIN), Buffer.alloc(5 * 1024 * 1024, 0x41));
  expectTrustCode(config, generationDir, 'SIDECAR_PACKAGE_TRUST_PE_HEADER');
});

test('production trust accepts real-size PE externalBin and native closed set', () => {
  const { config, generationDir } = fixture();
  restoreFixtureForTamper(generationDir);
  // 2026-09-19: 阳性对照必须用真 PE 结构。此前用 "MZ + 零填充"，在本函数只读
  // 魔数的时代能过 —— 于是这条"接受真实体积 PE"的对照组实际什么都没验，
  // 正是它放过了 liteav_media_server.exe 这类只有魔数也照样通过的情形。
  fs.writeFileSync(
    path.join(generationDir, INSTALLED_BIN),
    peBytes({ size: 5 * 1024 * 1024 }),
  );
  const nativeDir = trustInput(config, generationDir).nativeDir;
  for (const name of NATIVE_NAMES) {
    fs.writeFileSync(path.join(nativeDir, name), peBytes({ size: 64 * 1024 }));
  }
  fs.writeFileSync(path.join(generationDir, 'ffmpeg.dll'), Buffer.alloc(512 * 1024));
  fs.writeFileSync(path.join(generationDir, 'resources.pak'), Buffer.alloc(512 * 1024));
  fs.writeFileSync(path.join(generationDir, 'icudtl.dat'), Buffer.alloc(512 * 1024));
  fs.writeFileSync(path.join(generationDir, 'v8_context_snapshot.bin'), Buffer.alloc(64 * 1024));
  fs.writeFileSync(path.join(generationDir, 'locales', 'en-US.pak'), Buffer.alloc(32 * 1024));
  assertProductionTrust(trustInput(config, generationDir));
});

test('createProvenance stamps the production trust policy version into the manifest', () => {
  // 缺了这个键，assertProductionTrust 只能退回"版本化之前的基线"，
  // 于是"策略变了"就只对新构建生效 —— 旧 generation 原样留在野。
  const { config, generationDir } = fixture();
  const produced = createProvenance(config, generationDir);
  assert.equal(produced.trust_version, TRUST_VERSION);
});

test('manifest schema tolerates an absent trust version but the trust gate now rejects it', () => {
  // 2026-09-20：这条用例要分成两层看，而在此之前两层恰好同向（都放行），bump 之后分叉：
  //   ① **schema 层**：`trust_version` 是可选键（MANIFEST_OPTIONAL_KEYS）。缺键的旧 manifest
  //      必须仍能解析、仍能过 verifyPackage —— 否则"解析不了"会盖掉"策略过期"这个真实原因，
  //      运维看到的报错就指错了地方。
  //   ② **可信门层**：策略版本在 2026-09-20 随随包原生集剪除一并 1.0.0 → 1.1.0，
  //      "缺键 = 版本化之前的基线 ⇒ 放行"这条**到期**。本机 current-installed 的两个
  //      2026-09-05 generation 现在必须判 SIDECAR_PACKAGE_TRUST_VERSION_MISMATCH ——
  //      这正是"把策略变更变成机械后果"、强制重建的那一步，不是回归。
  // ⚠️ 构造方式很关键：fixture 现在默认**盖上当前策略版本**（见 fixtureManifest），
  //    所以缺键形态必须显式删键来构造。此前这条用例指向 `fixture()` 的默认 manifest，
  //    bump 之后那个 manifest 已经带上键了 —— 若不补这一段，用例名还在讲"缺键"，
  //    断言却再也没碰过缺键形态（空断言），这是比变红更坏的失败模式。
  const absent = fixture({
    mutateManifest: (manifest) => { delete manifest.trust_version; },
  });
  verifyPackage(absent.config);
  expectTrustCode(absent.config, absent.generationDir, 'SIDECAR_PACKAGE_TRUST_VERSION_MISMATCH');

  const declared = fixture({
    mutateManifest: (manifest) => { manifest.trust_version = TRUST_VERSION; },
  });
  assert.equal(verifyPackage(declared.config).trust_version, TRUST_VERSION);

  for (const bad of [123, '', null]) {
    const broken = fixture({
      mutateManifest: (manifest) => { manifest.trust_version = bad; },
    });
    expectCode(broken.config, 'SIDECAR_PACKAGE_MANIFEST_SCHEMA_INVALID');
  }
});

test('production trust rejects a generation whose manifest declares a stale trust policy version', () => {
  const { config, generationDir } = fixture({
    mutateManifest: (manifest) => { manifest.trust_version = '0.9.0'; },
  });
  // schema 层面它是合法的（非空字符串），只有可信门的版本比对能拦住它 ——
  // 这正是"把策略变更变成机械后果"的那一步：旧策略的 generation 校验失败 ⇒ 强制重建。
  verifyPackage(config);
  expectTrustCode(config, generationDir, 'SIDECAR_PACKAGE_TRUST_VERSION_MISMATCH');
});

// --------------------------------------------------------------------------
// 2026-09-20：刻意剪除的原生成员（媒体混流服务进程，CUI）
// --------------------------------------------------------------------------
test('the pruned native is a recorded decision and cannot come back silently', () => {
  // 「它不在」必须是**记录在案的决定**，不是某天有人手滑删掉的效果。两个方向都要有牙：
  //   正向：4 份生产清单 + 本文件的冻结字面量 + 两处派生副本都不得含它（防"悄悄加回来"）；
  //   反向：INTENTIONALLY_ABSENT_NATIVE 必须含它，且带名字 + 理由 + 移除日期 + 实测量
  //        （防"把记录删掉" —— 那会让缺席变成无主状态，下一个读清单的人只会当它是漏登记）。
  assert.equal(
    INTENTIONALLY_ABSENT_NATIVE.length,
    1,
    '增删刻意缺席项必须同步改本用例、全部清单与构建期剪除，不能只改这里',
  );
  const entry = INTENTIONALLY_ABSENT_NATIVE[0];
  assert.equal(entry.name, PRUNED_NATIVE);
  assert.equal(typeof entry.reason, 'string');
  assert.ok(entry.reason.length > 0, '缺席必须有理由：否则下一个人只会当它是漏登记');
  assert.match(entry.removed_on, /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/, '缺席必须有移除日期');
  assert.equal(
    entry.measured.pe_subsystem,
    3,
    '记录的实测量必须与 PE 子系统门禁同口径（3 = WINDOWS_CUI）—— 这就是它必须被剪的理由',
  );
  assert.match(entry.measured.sha256, /^[0-9a-f]{64}$/);
  assert.ok(entry.measured.bytes > 0);

  const productionPrefix = 'resources/app/node_modules/trtc-electron-sdk/build/Release/';
  const lists = [
    ['scripts/lib/sidecar-trust.js 的 NATIVE_NAMES（构建期可信门）', [...TRUST_NATIVE_NAMES]],
    ['scripts/lib/sidecar-package-common.js 的 NATIVE_REQUIRED（provenance 哈希覆盖集）', [...NATIVE_REQUIRED]],
    [
      'pet-ui/src-tauri/src/sidecar_runtime_trust.rs 的 NATIVE_NAMES（启动期可信门）',
      rustStrArray('pet-ui/src-tauri/src/sidecar_runtime_trust.rs', 'NATIVE_NAMES'),
    ],
    [
      'pet-ui/src-tauri/src/sidecar_integrity.rs 的 REQUIRED（启动期精确集合相等）',
      rustStrArray('pet-ui/src-tauri/src/sidecar_integrity.rs', 'REQUIRED')
        .map((item) => path.posix.basename(item)),
    ],
    ['本文件的冻结字面量 NATIVE_NAMES', [...NATIVE_NAMES]],
  ];
  for (const [label, values] of lists) {
    assert.equal(
      values.includes(PRUNED_NATIVE),
      false,
      label + ' 又把它加回来了：剪除是产品决策（见 INTENTIONALLY_ABSENT_NATIVE），不是漏登记',
    );
  }
  // 冻结字面量必须仍是 4 项且全路径在 productionPrefix 下（与 REQUIRED 的形态约束一致）。
  assert.equal(lists[4][1].length, 4);
  assert.equal(
    rustStrArray('pet-ui/src-tauri/src/sidecar_integrity.rs', 'REQUIRED')
      .every((item) => item.startsWith(productionPrefix)),
    true,
  );

  // 派生副本：不是"清单"，但同样是把 CUI 带回包里的路径。它们必须**引用**那份记录，
  // 而不是各写一遍字面量（与 RUNTIME_ARTIFACT_FILES 那条锁同一理由）。
  for (const relative of [
    'scripts/create-sidecar-runtime-fixture.js',
    'pet-ui/src-tauri/src/sidecar_integrity.rs',
  ]) {
    assert.equal(
      readProjectFile(relative).includes(PRUNED_NATIVE),
      false,
      relative + ' 仍提到它。请引用 scripts/lib/sidecar-trust.js 的 INTENTIONALLY_ABSENT_NATIVE，'
      + '不要在派生副本里重写字面量',
    );
  }
});

test('the build prunes the intentionally absent native out of staging with two-way fail-closed', () => {
  // "不从清单里声明"与"不随包发货"是两件事：只删清单会留下一份谁也解释不清的载荷。
  // 这条钉住**发货侧**的机械保证，两个方向都不得静默继续：
  //   上游有而 staging 没有 ⇒ 拷贝不完整（剪除退化成空操作）；删完仍在 ⇒ 删除失败。
  const { pruneIntentionallyAbsentNatives } = require('../lib/sidecar-package-build');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'jax-sidecar-pruned-native-'));
  const sourceRelease = path.join(root, 'node_modules', 'trtc-electron-sdk', 'build', 'Release');
  const stagedRelease = path.join(root, 'staged', 'build', 'Release');
  fs.mkdirSync(sourceRelease, { recursive: true });
  fs.mkdirSync(stagedRelease, { recursive: true });
  const fail = (code) => { throw new PackageError(code); };

  // 上游 SDK 里没有它（例如换了个 SDK 版本）⇒ 什么都不删，也不报错。
  pruneIntentionallyAbsentNatives(sourceRelease, stagedRelease, fail);

  // 上游有、staging 也有 ⇒ 必须从 staging 消失。这是"它不再随包发货"的机械保证。
  fs.writeFileSync(path.join(sourceRelease, PRUNED_NATIVE), 'cui');
  fs.writeFileSync(path.join(stagedRelease, PRUNED_NATIVE), 'cui');
  pruneIntentionallyAbsentNatives(sourceRelease, stagedRelease, fail);
  assert.equal(fs.existsSync(path.join(stagedRelease, PRUNED_NATIVE)), false);

  // 上游有而 staging 里没有 ⇒ 拷贝不完整：剪除会静默退化成空操作，必须响亮报错。
  assert.throws(
    () => pruneIntentionallyAbsentNatives(sourceRelease, stagedRelease, fail),
    (error) => error instanceof PackageError
      && error.code === 'SIDECAR_PACKAGE_PRUNED_NATIVE_COPY_INCOMPLETE',
  );

  // 删不掉（此处用同名目录模拟）⇒ 不得带着它继续打包。
  fs.mkdirSync(path.join(stagedRelease, PRUNED_NATIVE));
  assert.throws(
    () => pruneIntentionallyAbsentNatives(sourceRelease, stagedRelease, fail),
    (error) => error instanceof PackageError
      && error.code === 'SIDECAR_PACKAGE_PRUNED_NATIVE_PRUNE_FAILED',
  );
});

test('the media family API reference guard stops the build with a named error', () => {
  const {
    assertNoMediaFamilyApiReferences,
    MEDIA_FAMILY_API_PATTERN,
  } = require('../lib/sidecar-package-build');
  assert.equal(typeof MEDIA_FAMILY_API_PATTERN, 'string');

  // 阴性：本仓真实 sidecar 源码必须通过（实测 0 命中）。
  assert.doesNotThrow(() => assertNoMediaFamilyApiReferences(path.join(PROJECT_ROOT, 'sidecar')));

  // **阳性对照**（不可省）：必须构造一份含真实调用形状的样本并断言它真的被拦下 ——
  // 否则正则写错（例如转义写歪）就是"空集通过"，门禁有形状没牙齿。
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jax-media-family-'));
  for (const relative of APP_SOURCES) {
    if (relative.endsWith('.js')) fs.writeFileSync(path.join(dir, relative), '// clean\n');
  }
  fs.writeFileSync(
    path.join(dir, 'main.js'),
    'const s = trtcCloud.getMediaMixingService();\nawait s.startMediaMixingServer(path);\n',
  );
  let caught;
  try {
    assertNoMediaFamilyApiReferences(dir);
  } catch (error) {
    caught = error;
  }
  assert.ok(caught, '含媒体家族调用的样本没有被拦下 —— preflight 是假门禁');
  assert.equal(caught.code, 'SIDECAR_MEDIA_MIXING_REQUIRES_PRUNED_NATIVE');
  assert.ok(caught.message.includes('main.js:'), '错误文案必须点名命中位置（文件:行）');
  assert.ok(
    caught.message.includes('INTENTIONALLY_ABSENT_NATIVE'),
    '错误文案必须直接给恢复步骤，而不是让 catch 的人自己去猜',
  );
});

test('the media family guard neither misfires on real calls nor misses the real names', () => {
  // 假红与假阴成对钉住：缺任一侧，这道门要么变成噪音要么变成装饰。
  const { MEDIA_FAMILY_API_PATTERN } = require('../lib/sidecar-package-build');
  const re = new RegExp(MEDIA_FAMILY_API_PATTERN);
  // 假红侧：产品**现在就在用**的调用面（样本取自 sidecar/ 的真实标识符统计），一个都不能被拦。
  for (const sample of [
    'cloud.enterRoom(params, TRTCAppScene.TRTCAppSceneAudioCall);',
    'cloud.setAudioFrameCallback({ onAudioFrame: (frame) => {} });',
    'cloud.stopLocalAudio();',
    'cloud.startLocalAudio();',
    'cloud.muteLocalAudio(true);',
    'function startSession(cred) {}',
    'startPollingRuntime();',
    'if (name.startsWith("jax")) {}',
    'const stop_ms = 20;',
  ]) {
    assert.equal(re.test(sample), false, '合法调用被误判为媒体家族引用：' + sample);
  }
  // 假阴侧：真名字必须被认出来（含被解构赋值、被写进字符串的形态）。
  for (const sample of [
    'await mediaMixingService.startMediaMixingServer(path);',
    'trtcCloud.getMediaMixingManager().addMediaSource(source);',
    'mediaMixingService.on(TRTCMediaMixingServiceEvent.onMediaMixingServerLost, cb);',
    'cloud.startScreenCapture(target);',
    'cloud.stopScreenCapture();',
    'cloud.startCloudRecording();',
    'cloud.startLocalPreview(view);',
    'const p = "resources/liteav_media_server.exe";',
  ]) {
    assert.equal(re.test(sample), true, '媒体家族引用被漏过：' + sample);
  }
});

test('the build path actually calls the pruned-native prune and the media family guard', () => {
  // 上面三条钉的是**函数行为**，它们钉不住"buildPackage 里不再调用" —— 删掉调用行会让
  // 行为用例依然全绿而剪除实际不再发生。这条静态锁补上那个缺口。
  // 边界（如实说明）：它钉"调用行存在"，钉不住改名/改参数（静态锁够不到，见 mutation-check 的覆盖说明）。
  const source = readProjectFile('scripts/lib/sidecar-package-build.js');
  for (const call of [
    'pruneIntentionallyAbsentNatives(sourceRelease, stagedRelease, fail);',
    'assertNoMediaFamilyApiReferences(config.sidecarDir);',
  ]) {
    assert.equal(
      source.includes(call),
      true,
      'buildPackage 不再调用 ' + call + ' ⇒ 剪除/preflight 实际不会发生',
    );
  }
});

