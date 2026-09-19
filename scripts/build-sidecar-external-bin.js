'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const {
  TARGET_TRIPLE,
  PackageError,
  buildPackage,
  parseGenerationManifest,
  resolveCurrentGeneration,
  verifyPackage,
} = require('./lib/sidecar-package');
const { assertProductionTrust } = require('./lib/sidecar-trust');
const { inspectConsumers } = require('./lib/sidecar-runtime-consumer-probe');
const { acquireRuntimeLease } = require('./lib/sidecar-runtime-coordination');
const { migrateLegacyRuntime } = require('./lib/sidecar-runtime-migration');
const {
  inspectLegacyPublishLock,
  parseMigrationOptions,
} = require('./lib/sidecar-runtime-migration-command');

const root = path.resolve(__dirname, '..');
const sidecarDir = path.join(root, 'sidecar');
const binDir = path.join(root, 'pet-ui', 'src-tauri', 'binaries');
const runtimeDir = path.join(binDir, 'jax-rtc-sidecar-runtime');
const sourceLockFile = path.join(sidecarDir, 'package-lock.json');

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function lockedVersions() {
  const packageJson = JSON.parse(fs.readFileSync(path.join(sidecarDir, 'package.json'), 'utf8'));
  return {
    electronVersion: packageJson.devDependencies.electron,
    sdkVersion: packageJson.dependencies['trtc-electron-sdk'],
  };
}

function nativePointerHelperPath(input = {}) {
  return input.helperPath || path.join(root, 'tools', 'sidecar-pointer-replace', 'target', 'release', 'sidecar-pointer-replace.exe');
}

function requireNativePointerHelper(input = {}) {
  if (process.platform !== 'win32') return null;
  const helperPath = nativePointerHelperPath(input);
  if (!fs.existsSync(helperPath)) {
    const error = new Error('SIDECAR_POINTER_REPLACE_HELPER_MISSING');
    error.code = 'SIDECAR_POINTER_REPLACE_HELPER_MISSING';
    throw error;
  }
  return helperPath;
}

function defaultCargoWhich(platform) {
  return (binary) => {
    const command = platform === 'win32' ? 'where' : 'which';
    let result;
    try {
      result = spawnSync(command, [binary], { encoding: 'utf8', windowsHide: true, timeout: 5000 });
    } catch {
      return null;
    }
    if (!result || result.status !== 0 || typeof result.stdout !== 'string') return null;
    return result.stdout.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)[0] || null;
  };
}

// 发布环境必须确定使用同一把 Cargo：PATH 解析会随 shell/session 漂移，
// 本机实测该 shell 的 PATH 里根本没有 cargo，只能靠显式路径或 cargo home。
function resolveCargoPath(options = {}) {
  const platform = options.platform || process.platform;
  const env = options.env || process.env;
  const existsSync = options.existsSync || ((candidate) => fs.existsSync(candidate));
  const binary = platform === 'win32' ? 'cargo.exe' : 'cargo';
  const home = options.homeDir || env.USERPROFILE || env.HOME || '';
  const candidates = [];
  if (options.cargoPath) candidates.push(options.cargoPath);
  if (env.JAX_CARGO_BIN) candidates.push(env.JAX_CARGO_BIN);
  if (home) candidates.push(path.join(home, '.cargo', 'bin', binary));
  for (const candidate of candidates) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  const discovered = (options.which || defaultCargoWhich(platform))(binary);
  if (discovered && existsSync(discovered)) return discovered;
  const error = new Error('SIDECAR_CARGO_TOOLCHAIN_UNAVAILABLE');
  error.code = 'SIDECAR_CARGO_TOOLCHAIN_UNAVAILABLE';
  throw error;
}

function ensureNativePointerHelper(input = {}) {
  if (process.platform !== 'win32') return null;
  const manifestPath = input.manifestPath || path.join(root, 'tools', 'sidecar-pointer-replace', 'Cargo.toml');
  const helperPath = nativePointerHelperPath(input);
  const cargoPath = input.cargoPath || resolveCargoPath(input);
  const execute = input.execute || ((command, args) => spawnSync(command, args, { stdio: 'inherit', windowsHide: true }));
  const result = execute(cargoPath, ['build', '--release', '--manifest-path', manifestPath]);
  if (!result || result.error || result.status !== 0) {
    const error = new Error('SIDECAR_POINTER_REPLACE_HELPER_BUILD_FAILED');
    error.code = 'SIDECAR_POINTER_REPLACE_HELPER_BUILD_FAILED';
    throw error;
  }
  return requireNativePointerHelper({ helperPath });
}

function packageConfig() {
  const versions = lockedVersions();
  return {
    sidecarDir,
    binDir,
    runtimeDir,
    executable: path.join(binDir, `jax-rtc-sidecar-${TARGET_TRIPLE}.exe`),
    installedFile: 'jax-rtc-sidecar.exe',
    sourceLockFile,
    sourceLockHash: sha256(sourceLockFile),
    ...versions,
  };
}

function verifyAndTrust(config) {
  const manifest = verifyPackage(config);
  const generationDir = resolveCurrentGeneration(config).generationDir;
  assertProductionTrust({
    executable: path.join(generationDir, config.installedFile),
    nativeDir: path.join(generationDir, 'resources', 'app', 'node_modules', 'trtc-electron-sdk', 'build', 'Release'),
    runtimeDir: generationDir,
    provenance: manifest,
  });
  return manifest;
}

function main(input = {}) {
  const args = input.args || process.argv.slice(2);
  const verifyOnly = args.includes('--verify-only');
  const migration = parseMigrationOptions(args);
  if (verifyOnly) requireNativePointerHelper(input);
  else ensureNativePointerHelper(input);
  const config = (input.packageConfig || packageConfig)();
  // 普通发布与 legacy 迁移共用同一把跨进程租约，避免迁移的 final probe
  // 到 rename/publish/verify 期间被并发 build 写入。
  const lease = input.acquireRuntimeLease || acquireRuntimeLease;
  const coordination = input.coordination || {};
  const build = input.buildPackage || buildPackage;
  const verify = input.verifyPackage || verifyPackage;
  const trust = input.verifyAndTrust || verifyAndTrust;

  if (migration.migrate) {
    const manifest = migrateLegacyRuntime({
      runtimeDir: config.runtimeDir,
      backupDir: migration.backupDir,
      acquireMigrationLock: (runtimeParent) => lease(
        path.join(runtimeParent, path.basename(config.runtimeDir)),
        { ...coordination, operation: 'migration' },
      ),
      inspectConsumers,
      inspectLockOwner: inspectLegacyPublishLock,
      publish: () => build(config),
      verify: () => trust(config),
    });
    process.stdout.write(`migration-ready ${manifest.backupDir}\n`);
    return;
  }

  const release = lease(config.runtimeDir, { ...coordination, operation: 'publish' });
  let manifest;
  try {
    manifest = verifyOnly ? verify(config) : build(config);
    // 生产可信门（策略版本 + 体积 + PE 头）落在 selected immutable generation 内，
    // 而非 flat runtime 或 externalBin 构建输入。必须在释放租约前完成：
    // 否则并发 publisher 可在 release 之后替换 current pointer，使被校验的
    // generation 与实际选中的 generation 不一致（TOCTOU）。
    (input.trustGate || ((activeConfig) => {
      const generationDir = resolveCurrentGeneration(activeConfig).generationDir;
      assertProductionTrust({
        executable: path.join(generationDir, activeConfig.installedFile),
        nativeDir: path.join(generationDir, 'resources', 'app', 'node_modules', 'trtc-electron-sdk', 'build', 'Release'),
        runtimeDir: generationDir,
        // 从（重新解析后的）generation 目录读 manifest，而不是复用上面的 `manifest`
        // 局部量：pointer 若在校验与该门之间被并发 publisher 换掉，闭包里的旧
        // manifest 会掩盖这次替换（与上面的 TOCTOU 注释同因）。
        provenance: (input.parseGenerationManifest || parseGenerationManifest)(generationDir),
      });
    }))(config);
    process.stdout.write(
      `${verifyOnly ? 'verified' : 'built'} ${manifest.external_bin.build_input_file} ${manifest.external_bin.sha256}\n`
    );
  } finally {
    release();
  }
}

function diagnosticCode(error) {
  const migrationCode = /^SIDECAR_RUNTIME_MIGRATION_[A-Z_]+/.exec(error.code || error.message || '');
  const helperCode = /^SIDECAR_POINTER_REPLACE_HELPER_[A-Z_]+/.exec(error.code || error.message || '');
  const coordinationCode = /^SIDECAR_RUNTIME_COORDINATION_[A-Z_]+/.exec(error.code || error.message || '');
  const toolchainCode = /^SIDECAR_CARGO_TOOLCHAIN_[A-Z_]+/.exec(error.code || error.message || '');
  if (error instanceof PackageError) return error.code;
  if (toolchainCode) return toolchainCode[0];
  if (coordinationCode) return coordinationCode[0];
  if (helperCode) return helperCode[0];
  return migrationCode ? migrationCode[0] : 'SIDECAR_PACKAGE_UNEXPECTED_FAILURE';
}

if (require.main === module) {
  try {
    main();
  } catch (error) {
    process.stderr.write(`${diagnosticCode(error)}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  diagnosticCode,
  ensureNativePointerHelper,
  main,
  packageConfig,
  requireNativePointerHelper,
  resolveCargoPath,
};
