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
  expectedBundleResourceMap,
  verifyPackage,
} = require('../lib/sidecar-package');
const { assertProductionTrust } = require('../lib/sidecar-trust');
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
const NATIVE_NAMES = [
  'trtc_electron_sdk.node',
  'liteav.dll',
  'txffmpeg.dll',
  'txsoundtouch.dll',
  'liteav_media_server.exe',
];

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
  };
}

function expectTrustCode(config, generationDir, code) {
  assert.throws(
    () => assertProductionTrust(trustInput(config, generationDir)),
    (error) => error.code === code,
  );
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
  fs.mkdirSync(path.join(generationDir, 'resources', 'app', 'node_modules', 'electron'));
  expectCode(config, 'SIDECAR_PACKAGE_DEV_DEPENDENCY_EMBEDDED');
});

test('changing a selected generation payload fails verification', () => {
  const { config, generationDir } = fixture();
  fs.writeFileSync(path.join(generationDir, 'resources.pak'), 'tampered');
  assert.throws(() => verifyPackage(config), (error) => error instanceof PackageError);
});

test('rejects runtime closed-set additions and omissions', () => {
  const added = fixture();
  fs.writeFileSync(path.join(added.generationDir, 'unrecorded.dll'), 'unrecorded');
  assert.throws(() => verifyPackage(added.config), (error) => error instanceof PackageError);

  const omitted = fixture();
  const manifest = JSON.parse(fs.readFileSync(path.join(omitted.generationDir, PROVENANCE_FILE), 'utf8'));
  manifest.runtime_files = manifest.runtime_files.filter((item) => item.path !== 'resources.pak');
  fs.writeFileSync(path.join(omitted.generationDir, PROVENANCE_FILE), JSON.stringify(manifest));
  assert.throws(() => verifyPackage(omitted.config), (error) => error instanceof PackageError);
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

test('requires the exact five native paths to match runtime entries', () => {
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
  const installed = path.join(generationDir, INSTALLED_BIN);
  const substituted = path.join(generationDir, `jax-rtc-sidecar-${TARGET_TRIPLE}.exe`);
  fs.renameSync(installed, substituted);
  assert.throws(() => verifyPackage(config), (error) => error instanceof PackageError);

  // 反向：即便同时存在 target-triple 副本，合法 installed 身份仍必须存在；
  // 仅有三元组命名文件、缺失 jax-rtc-sidecar.exe 时必须失败。
  const partial = fixture();
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

test('resource mapping preserves the dedicated runtime directory contract end to end', () => {
  const destination = 'jax-rtc-sidecar-runtime/';
  assert.deepEqual(expectedBundleResourceMap(), {
    'binaries/jax-rtc-sidecar-runtime/': destination,
  });

  const tauri = JSON.parse(readProjectFile('pet-ui/src-tauri/tauri.conf.json'));
  // bundle.resources 保留 stable root 的完整 generation 树映射（不 flatten、不 symlink-follow）；
  // certs/ca.crt 是独立的 Tauri 资源，不属于 sidecar manifest 的 bundle_resources。
  assert.equal(tauri.bundle.resources['binaries/jax-rtc-sidecar-runtime/'], destination);

  const mainSource = readProjectFile('pet-ui/src-tauri/src/main.rs');
  assert.equal(rustConst(mainSource, 'SIDECAR_RUNTIME_DIR'), destination.replace(/\/$/, ''));

  // installed 身份是固定名 jax-rtc-sidecar.exe，resolver 绝不接受 triple 命名替代。
  const resolver = readProjectFile('pet-ui/src-tauri/src/sidecar_runtime_pointer.rs');
  assert.equal(resolver.includes('jax-rtc-sidecar.exe'), true);
  assert.equal(resolver.includes(`jax-rtc-sidecar-${TARGET_TRIPLE}.exe`), false);

  const { config, generation } = fixture();
  const manifest = verifyPackage(config);
  assert.deepEqual(manifest.bundle_resources, expectedBundleResourceMap());
  assert.equal(path.basename(config.runtimeDir), destination.replace(/\/$/, ''));
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
  fs.writeFileSync(
    path.join(generationDir, INSTALLED_BIN),
    Buffer.concat([Buffer.from([0x4d, 0x5a]), Buffer.alloc(5 * 1024 * 1024)]),
  );
  expectTrustCode(config, generationDir, 'SIDECAR_PACKAGE_TRUST_MIN_SIZE');
});

test('production trust rejects oversized non-PE binary without MZ header', () => {
  const { config, generationDir } = fixture();
  fs.writeFileSync(path.join(generationDir, INSTALLED_BIN), Buffer.alloc(5 * 1024 * 1024, 0x41));
  expectTrustCode(config, generationDir, 'SIDECAR_PACKAGE_TRUST_PE_HEADER');
});

test('production trust accepts real-size PE externalBin and native closed set', () => {
  const { config, generationDir } = fixture();
  fs.writeFileSync(
    path.join(generationDir, INSTALLED_BIN),
    Buffer.concat([Buffer.from([0x4d, 0x5a]), Buffer.alloc(5 * 1024 * 1024)]),
  );
  const nativeDir = trustInput(config, generationDir).nativeDir;
  for (const name of NATIVE_NAMES) {
    fs.writeFileSync(path.join(nativeDir, name), Buffer.concat([Buffer.from([0x4d, 0x5a]), Buffer.alloc(64 * 1024)]));
  }
  fs.writeFileSync(path.join(generationDir, 'ffmpeg.dll'), Buffer.alloc(512 * 1024));
  fs.writeFileSync(path.join(generationDir, 'resources.pak'), Buffer.alloc(512 * 1024));
  fs.writeFileSync(path.join(generationDir, 'icudtl.dat'), Buffer.alloc(512 * 1024));
  fs.writeFileSync(path.join(generationDir, 'v8_context_snapshot.bin'), Buffer.alloc(64 * 1024));
  fs.writeFileSync(path.join(generationDir, 'locales', 'en-US.pak'), Buffer.alloc(32 * 1024));
  assertProductionTrust(trustInput(config, generationDir));
});
