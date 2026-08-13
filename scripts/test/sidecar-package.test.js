'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  APP_SOURCES,
  TARGET_TRIPLE,
  createProvenance,
  expectedBundleResourceMap,
  verifyPackage,
} = require('../lib/sidecar-package');
const { assertProductionTrust } = require('../lib/sidecar-trust');
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

function sha256(value) {
  return require('node:crypto').createHash('sha256').update(value).digest('hex');
}

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'jax-sidecar-package-'));
  const binDir = path.join(root, 'binaries');
  const runtime = path.join(binDir, 'jax-rtc-sidecar-runtime');
  const app = path.join(runtime, 'resources', 'app');
  const sdk = path.join(app, 'node_modules', 'trtc-electron-sdk');
  fs.mkdirSync(path.join(runtime, 'locales'), { recursive: true });
  fs.mkdirSync(path.join(sdk, 'build', 'Release'), { recursive: true });
  fs.mkdirSync(path.join(sdk, 'liteav'), { recursive: true });
  fs.writeFileSync(path.join(runtime, 'ffmpeg.dll'), 'electron-ffmpeg');
  fs.writeFileSync(path.join(runtime, 'resources.pak'), 'pak');
  fs.writeFileSync(path.join(runtime, 'icudtl.dat'), 'icu');
  fs.writeFileSync(path.join(runtime, 'v8_context_snapshot.bin'), 'snapshot');
  fs.writeFileSync(path.join(runtime, 'locales', 'en-US.pak'), 'locale');
  fs.writeFileSync(path.join(app, 'main.js'), 'require("trtc-electron-sdk")');
  fs.writeFileSync(path.join(app, 'package.json'), JSON.stringify({ name: 'jax-rtc-sidecar' }));
  fs.writeFileSync(path.join(app, 'package-lock.json'), '{"lockfileVersion":3}');
  fs.writeFileSync(path.join(sdk, 'package.json'), JSON.stringify({ version: '13.4.802-beta.3' }));
  fs.writeFileSync(path.join(sdk, 'liteav', 'index.js'), 'module.exports={}');
  for (const name of ['trtc_electron_sdk.node', 'liteav.dll', 'txffmpeg.dll', 'txsoundtouch.dll', 'liteav_media_server.exe']) {
    fs.writeFileSync(path.join(sdk, 'build', 'Release', name), name);
  }
  const exe = path.join(binDir, `jax-rtc-sidecar-${TARGET_TRIPLE}.exe`);
  fs.mkdirSync(binDir, { recursive: true });
  fs.writeFileSync(exe, 'electron-runtime-executable');
  const config = {
    binDir,
    runtimeDir: runtime,
    executable: exe,
    hashFile: path.join(runtime, 'jax-rtc-sidecar.exe.sha256'),
    manifestFile: path.join(runtime, 'jax-rtc-sidecar.provenance.json'),
    installedFile: 'jax-rtc-sidecar.exe',
    sourceLockFile: path.join(app, 'package-lock.json'),
    sourceLockHash: sha256(fs.readFileSync(path.join(app, 'package-lock.json'))),
    electronVersion: '31.7.7',
    sdkVersion: '13.4.802-beta.3',
  };
  const manifest = createProvenance(config);
  fs.writeFileSync(config.hashFile, `${manifest.external_bin.sha256}\n`);
  fs.writeFileSync(config.manifestFile, `${JSON.stringify(manifest, null, 2)}\n`);
  return { root, config };
}

function expectCode(config, code) {
  assert.throws(() => verifyPackage(config), (error) => error.code === code);
}

test('rejects missing externalBin', () => {
  const { config } = fixture();
  fs.rmSync(config.executable);
  expectCode(config, 'SIDECAR_PACKAGE_EXTERNAL_BIN_MISSING');
});

test('rejects empty, non-lowercase and mismatched expected hash', () => {
  for (const [value, code] of [
    ['', 'SIDECAR_PACKAGE_HASH_INVALID'],
    ['A'.repeat(64), 'SIDECAR_PACKAGE_HASH_INVALID'],
    ['0'.repeat(64), 'SIDECAR_PACKAGE_HASH_MISMATCH'],
  ]) {
    const { config } = fixture();
    fs.writeFileSync(config.hashFile, value);
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
  const { config } = fixture();
  fs.rmSync(path.join(config.runtimeDir, 'resources', 'app', 'node_modules', 'trtc-electron-sdk', 'build', 'Release', 'txsoundtouch.dll'));
  expectCode(config, 'SIDECAR_PACKAGE_NATIVE_MISSING');
});

test('rejects Electron devDependency embedded in resources/app', () => {
  const { config } = fixture();
  fs.mkdirSync(path.join(config.runtimeDir, 'resources', 'app', 'node_modules', 'electron'));
  expectCode(config, 'SIDECAR_PACKAGE_DEV_DEPENDENCY_EMBEDDED');
});

test('rejects runtime tampering after provenance generation', () => {
  const { config } = fixture();
  fs.writeFileSync(path.join(config.runtimeDir, 'resources.pak'), 'tampered');
  expectCode(config, 'SIDECAR_PACKAGE_RUNTIME_MISMATCH');
});

test('rejects runtime closed-set additions and omissions', () => {
  const added = fixture();
  fs.writeFileSync(path.join(added.config.runtimeDir, 'unrecorded.dll'), 'unrecorded');
  expectCode(added.config, 'SIDECAR_PACKAGE_RUNTIME_SET_MISMATCH');

  const omitted = fixture();
  const manifest = JSON.parse(fs.readFileSync(omitted.config.manifestFile, 'utf8'));
  manifest.runtime_files = manifest.runtime_files.filter((item) => item.path !== 'resources.pak');
  fs.writeFileSync(omitted.config.manifestFile, JSON.stringify(manifest));
  expectCode(omitted.config, 'SIDECAR_PACKAGE_RUNTIME_SET_MISMATCH');
});

test('rejects duplicate, traversal and absolute manifest paths', () => {
  for (const mutate of [
    (manifest) => manifest.runtime_files.push({ ...manifest.runtime_files[0] }),
    (manifest) => { manifest.runtime_files[0].path = '../escape.dll'; },
    (manifest) => { manifest.runtime_files[0].path = 'C:/escape.dll'; },
  ]) {
    const { config } = fixture();
    const manifest = JSON.parse(fs.readFileSync(config.manifestFile, 'utf8'));
    mutate(manifest);
    fs.writeFileSync(config.manifestFile, JSON.stringify(manifest));
    expectCode(config, 'SIDECAR_PACKAGE_MANIFEST_PATH_INVALID');
  }
});

test('rejects strict manifest schema drift', () => {
  const { config } = fixture();
  const manifest = JSON.parse(fs.readFileSync(config.manifestFile, 'utf8'));
  manifest.untrusted_extension = true;
  fs.writeFileSync(config.manifestFile, JSON.stringify(manifest));
  expectCode(config, 'SIDECAR_PACKAGE_MANIFEST_SCHEMA_INVALID');
});

test('requires the exact five native paths to match runtime entries', () => {
  const valid = fixture();
  const verified = verifyPackage(valid.config);
  const runtime = new Map(verified.runtime_files.map((item) => [item.path, item.sha256]));
  for (const native of verified.native_files) {
    assert.equal(runtime.get(native.path), native.sha256);
  }

  const drifted = fixture();
  const manifest = JSON.parse(fs.readFileSync(drifted.config.manifestFile, 'utf8'));
  manifest.native_files[0].sha256 = '0'.repeat(64);
  fs.writeFileSync(drifted.config.manifestFile, JSON.stringify(manifest));
  expectCode(drifted.config, 'SIDECAR_PACKAGE_NATIVE_SUBSET_MISMATCH');

  const extra = fixture();
  const extraManifest = JSON.parse(fs.readFileSync(extra.config.manifestFile, 'utf8'));
  extraManifest.native_files.push(extraManifest.runtime_files.find((item) => item.path === 'resources.pak'));
  fs.writeFileSync(extra.config.manifestFile, JSON.stringify(extraManifest));
  expectCode(extra.config, 'SIDECAR_PACKAGE_NATIVE_SET_MISMATCH');
});

test('distinguishes build input and installed externalBin names', () => {
  const { config } = fixture();
  const manifest = verifyPackage(config);
  assert.equal(manifest.external_bin.build_input_file, `jax-rtc-sidecar-${TARGET_TRIPLE}.exe`);
  assert.equal(manifest.external_bin.installed_file, 'jax-rtc-sidecar.exe');
});

test('requires the dedicated runtime bundle resource destination', () => {
  const { config } = fixture();
  const manifest = JSON.parse(fs.readFileSync(config.manifestFile, 'utf8'));
  manifest.bundle_resources['binaries/jax-rtc-sidecar-runtime/'] = '';
  fs.writeFileSync(config.manifestFile, JSON.stringify(manifest));
  expectCode(config, 'SIDECAR_PACKAGE_MANIFEST_SCHEMA_INVALID');
});

test('ignores unmanaged installer siblings but rejects managed runtime additions', () => {
  const { config } = fixture();
  fs.writeFileSync(path.join(config.binDir, 'jax-pet.exe'), 'tauri-main');
  fs.writeFileSync(path.join(config.binDir, 'jax-rtc-sidecar.exe'), 'installed-sidecar');
  verifyPackage(config);
  fs.writeFileSync(path.join(config.runtimeDir, 'unrecorded-runtime.dll'), 'unrecorded');
  expectCode(config, 'SIDECAR_PACKAGE_RUNTIME_SET_MISMATCH');
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
  assert.deepEqual(tauri.bundle.resources, expectedBundleResourceMap());

  const mainSource = readProjectFile('pet-ui/src-tauri/src/main.rs');
  assert.equal(rustConst(mainSource, 'SIDECAR_RUNTIME_DIR'), destination.replace(/\/$/, ''));
  assert.equal(rustConst(mainSource, 'SIDECAR_BIN'), 'jax-rtc-sidecar.exe');
  assert.equal(rustConst(mainSource, 'SIDECAR_SHA_FILE'), 'jax-rtc-sidecar.exe.sha256');
  assert.equal(rustConst(mainSource, 'SIDECAR_MANIFEST_FILE'), 'jax-rtc-sidecar.provenance.json');

  const { config } = fixture();
  const manifest = verifyPackage(config);
  assert.deepEqual(manifest.bundle_resources, tauri.bundle.resources);
  assert.equal(path.basename(config.runtimeDir), destination.replace(/\/$/, ''));
  assert.equal(path.dirname(config.hashFile), config.runtimeDir);
  assert.equal(path.dirname(config.manifestFile), config.runtimeDir);
});

test('verify rejects tampered externalBin identity fields', async (t) => {
  for (const [field, value, code] of [
    ['build_input_file', 'attacker-x86_64-pc-windows-msvc.exe', 'SIDECAR_PACKAGE_EXTERNAL_BIN_IDENTITY_MISMATCH'],
    ['installed_file', 'attacker.exe', 'SIDECAR_PACKAGE_EXTERNAL_BIN_IDENTITY_MISMATCH'],
    ['target_triple', 'aarch64-pc-windows-msvc', 'SIDECAR_PACKAGE_EXTERNAL_BIN_IDENTITY_MISMATCH'],
  ]) {
    await t.test(field, () => {
      const { config } = fixture();
      const manifest = JSON.parse(fs.readFileSync(config.manifestFile, 'utf8'));
      manifest.external_bin[field] = value;
      fs.writeFileSync(config.manifestFile, JSON.stringify(manifest));
      expectCode(config, code);
    });
  }
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

function trustInput(config) {
  return {
    executable: config.executable,
    nativeDir: path.join(
      config.runtimeDir,
      'resources', 'app', 'node_modules', 'trtc-electron-sdk', 'build', 'Release',
    ),
    runtimeDir: config.runtimeDir,
  };
}

function expectTrustCode(config, code) {
  assert.throws(() => assertProductionTrust(trustInput(config)), (error) => error.code === code);
}

test('production trust rejects tiny externalBin accepted by self-consistency verify', () => {
  const { config } = fixture();
  verifyPackage(config);
  expectTrustCode(config, 'SIDECAR_PACKAGE_TRUST_MIN_SIZE');
});

test('production trust rejects tiny native runtime files accepted by self-consistency verify', () => {
  const { config } = fixture();
  verifyPackage(config);
  fs.writeFileSync(
    config.executable,
    Buffer.concat([Buffer.from([0x4d, 0x5a]), Buffer.alloc(5 * 1024 * 1024)]),
  );
  expectTrustCode(config, 'SIDECAR_PACKAGE_TRUST_MIN_SIZE');
});

test('production trust rejects oversized non-PE binary without MZ header', () => {
  const { config } = fixture();
  fs.writeFileSync(config.executable, Buffer.alloc(5 * 1024 * 1024, 0x41));
  expectTrustCode(config, 'SIDECAR_PACKAGE_TRUST_PE_HEADER');
});

test('production trust accepts real-size PE externalBin and native closed set', () => {
  const { config } = fixture();
  fs.writeFileSync(config.executable, Buffer.concat([Buffer.from([0x4d, 0x5a]), Buffer.alloc(5 * 1024 * 1024)]));
  const nativeDir = trustInput(config).nativeDir;
  for (const name of ['trtc_electron_sdk.node', 'liteav.dll', 'txffmpeg.dll', 'txsoundtouch.dll', 'liteav_media_server.exe']) {
    fs.writeFileSync(path.join(nativeDir, name), Buffer.concat([Buffer.from([0x4d, 0x5a]), Buffer.alloc(64 * 1024)]));
  }
  fs.writeFileSync(path.join(config.runtimeDir, 'ffmpeg.dll'), Buffer.alloc(512 * 1024));
  fs.writeFileSync(path.join(config.runtimeDir, 'resources.pak'), Buffer.alloc(512 * 1024));
  fs.writeFileSync(path.join(config.runtimeDir, 'icudtl.dat'), Buffer.alloc(512 * 1024));
  fs.writeFileSync(path.join(config.runtimeDir, 'v8_context_snapshot.bin'), Buffer.alloc(64 * 1024));
  fs.writeFileSync(path.join(config.runtimeDir, 'locales', 'en-US.pak'), Buffer.alloc(32 * 1024));
  assertProductionTrust(trustInput(config));
});
