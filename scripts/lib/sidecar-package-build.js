'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { RUNTIME_ARTIFACT_FILES } = require('./sidecar-runtime-immutable');
// 单向依赖：build → common（common 不反向 require 本文件，无环，与既有 lib 分层一致）。
const { APP_SOURCES, PackageError } = require('./sidecar-package-common');
const { INTENTIONALLY_ABSENT_NATIVE } = require('./sidecar-trust');

// ADR-027 generation 内的稳定 metadata 文件名（与 sidecar-package.js 常量一致）。
const SHA_FILE = 'jax-rtc-sidecar.exe.sha256';
const PROVENANCE_FILE = 'jax-rtc-sidecar.provenance.json';
const PROVENANCE_DIGEST_FILE = 'jax-rtc-sidecar.provenance.sha256';

function runNpm(args, cwd, fail) {
  const npmCli = path.join(path.dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js');
  const result = fs.existsSync(npmCli)
    ? spawnSync(process.execPath, [npmCli, ...args], { cwd, stdio: 'inherit', shell: false, env: { ...process.env } })
    : spawnSync(process.platform === 'win32' ? 'npm.cmd' : 'npm', args, {
      cwd,
      stdio: 'inherit',
      shell: false,
      env: { ...process.env },
    });
  if (result.status !== 0) fail('SIDECAR_PACKAGE_NPM_CI_FAILED');
}

// 2026-09-02 cpSync 机器级故障规避：本机（2026-09-01 02:30 后）fs.cpSync 任何参数组合
// 均触发 0xC0000409 fail-fast 硬崩（node 22/24、bash/PowerShell、大小目录全复现，
// 疑似安全软件 hook 层问题），而 copyFileSync 全量验证通过。用逐文件递归拷贝
// 等价替换 cpSync(recursive, force, dereference:false)。electron dist 内无 symlink，
// dereference 语义差异不影响结果。
function copyTreeInto(sourceDir, targetDir) {
  fs.mkdirSync(targetDir, { recursive: true });
  for (const entry of fs.readdirSync(sourceDir, { withFileTypes: true })) {
    const sourcePath = path.join(sourceDir, entry.name);
    const targetPath = path.join(targetDir, entry.name);
    if (entry.isDirectory()) copyTreeInto(sourcePath, targetPath);
    else if (entry.isFile()) fs.copyFileSync(sourcePath, targetPath);
  }
}

// 2026-09-02 大文件外部进程拷贝规避：copyTreeInto（cpSync→copyFileSync）之后，
// build 进程内对 ~180MB electron.exe 的连续 copyFileSync/rmSync 在 npm ci 子进程
// 之后稳定静默硬死（runs 3/5/6/7 复现：staging 74 文件 + jax-rtc-sidecar.exe 完成、
// electron.exe 残留、无 JS 异常输出、租约 unlink 失败留锁）；同样的操作在独立
// node 进程（含沙箱内/外）均正常。与大文件 cpSync 0xC0000409 同属宿主 hook 层
// 进程内状态故障家族。规避：大文件操作改走 cmd.exe 外部进程，绕开 node fs 层。
function copyFileExternal(source, target, fail) {
  const result = spawnSync('cmd.exe', ['/d', '/c', 'copy', '/y', source, target], {
    stdio: 'ignore',
    shell: false,
  });
  if (result.status !== 0 || !fs.existsSync(target)) fail('SIDECAR_PACKAGE_COPY_FILE_EXTERNAL_FAILED');
}

function removeFileExternal(target, fail) {
  const result = spawnSync('cmd.exe', ['/d', '/c', 'del', '/f', '/q', target], {
    stdio: 'ignore',
    shell: false,
  });
  if (fs.existsSync(target)) fail('SIDECAR_PACKAGE_REMOVE_FILE_EXTERNAL_FAILED');
}

// 从 staging 剪除运行期可再生产物（Chromium 跑过之后留在 dist 顶层的 debug.log）。
// 为什么是"剪除"而不是"不哈希"：它含构建机本地路径，装进客户包本身就不该发生 ——
// 只把它从闭集里排除会留下一份谁也解释不清的载荷。名单与闭集豁免同源
// （sidecar-runtime-immutable.js 是唯一真相源，位置见 sidecar-package-common.js）。
// 两向 fail-closed：源里有而 staging 里没有 ⇒ 拷贝不完整；删完仍在 ⇒ 删除失败。
// 不用 cmd.exe 外部删除：那是为 ~180MB electron.exe 的宿主 hook 层故障（见上）准备的，
// 这里是小文件，走 node fs 层跨平台且可断言。
function pruneRuntimeArtifacts(sourceDist, targetDir, fail) {
  for (const name of RUNTIME_ARTIFACT_FILES) {
    const staged = path.join(targetDir, name);
    if (fs.existsSync(path.join(sourceDist, name)) && !fs.existsSync(staged)) {
      fail('SIDECAR_PACKAGE_RUNTIME_ARTIFACT_COPY_INCOMPLETE');
    }
    if (!fs.existsSync(staged)) continue;
    try {
      fs.rmSync(staged, { force: true });
    } catch (_) {
      // 由下方向的存在性断言统一判红，避免把原始 fs 错误当成结论。
    }
    if (fs.existsSync(staged)) fail('SIDECAR_PACKAGE_RUNTIME_ARTIFACT_PRUNE_FAILED');
  }
}

// 媒体混流 / 推流 / 截屏家族的 API 名字：**有界**枚举（显式名字），不做通配。
//
// 为什么要有这道门：这些能力依赖随包 SDK 里的 liteav_media_server.exe 或同等的外部媒体
// 进程，而它已被剪除（见 sidecar-trust.js 的 INTENTIONALLY_ABSENT_NATIVE）。
// 刻意**不依赖**上游 TRTC 在找不到该 exe 时的行为 —— 那是**未验证**的（没人测过）。
// 把"日后用到"变成**构建期**的命名错误，而不是上线后在客户机器上静默坏。
const MEDIA_FAMILY_API_PATTERN = [
  '\\b[A-Za-z_]*[Mm]ediaMixing[A-Za-z_]*\\b',
  '\\b(?:start|stop)(?:CloudRecording|LocalRecording|ScreenCapture|LocalPreview)\\b',
  '\\b(?:getScreenCaptureSources|selectScreenCaptureTarget|pauseScreenCapture|resumeScreenCapture)\\b',
  '\\bliteav_media_server\\b',
].join('|');

// 命名错误必须自带来恢复步骤：catch 的人可能只看得见 stderr 的最后一行 code。
const MEDIA_FAMILY_RECOVERY = [
  '恢复步骤（按顺序）：',
  '  1) 先做产品决策：随包下发 CUI 的媒体混流服务进程必须由 owner 签字接受',
  '     （scripts/pe-subsystem-verify.py 刻意不提供 allowlist）；',
  '  2) 把该名字从 scripts/lib/sidecar-trust.js 的 INTENTIONALLY_ABSENT_NATIVE 移除，',
  '     并加回全部生产清单（NATIVE_NAMES / NATIVE_REQUIRED / 两份 Rust 常量 / 冻结字面量）；',
  '  3) 删掉本文件里对应的 pruneIntentionallyAbsentNatives 调用与这道 preflight；',
  '  4) 重建 generation 并重装（见 outputs/prune-liteav-media-server-plan.md）。',
].join('\n');

// 扫描口径：只扫随包发货的 sidecar 应用源码闭集（APP_SOURCES 里的 *.js），
// 不扫 node_modules（那是不可变 generation 的一部分，由 prune 与哈希覆盖处理）。
// 边界（如实说明）：它钉得住显式名字；用字符串拼接/动态属性名绕过它是不可能的静态锁
// 之外的形态 —— 这类绕过只能靠 review，不假装能拦。
function assertNoMediaFamilyApiReferences(sidecarDir) {
  if (!sidecarDir) return;
  const pattern = new RegExp(MEDIA_FAMILY_API_PATTERN);
  const hits = [];
  for (const relative of APP_SOURCES) {
    if (!relative.endsWith('.js')) continue;
    const file = path.join(sidecarDir, relative);
    if (!fs.existsSync(file)) continue; // 缺文件由 verifyAppSourceSet 判红，此处不重复判
    fs.readFileSync(file, 'utf8').split('\n').forEach((line, index) => {
      if (pattern.test(line)) hits.push(relative + ':' + (index + 1) + ': ' + line.trim());
    });
  }
  if (hits.length === 0) return;
  const error = new PackageError('SIDECAR_MEDIA_MIXING_REQUIRES_PRUNED_NATIVE');
  error.message = [
    'SIDECAR_MEDIA_MIXING_REQUIRES_PRUNED_NATIVE',
    '随包源码引用了依赖已剪除原生（INTENTIONALLY_ABSENT_NATIVE）的媒体家族 API：',
    ...hits.map((hit) => '  ' + hit),
    MEDIA_FAMILY_RECOVERY,
  ].join('\n');
  throw error;
}

// 从 staging 剪除**刻意缺席**的原生集成员（名单见 sidecar-trust.js 的 INTENTIONALLY_ABSENT_NATIVE）。
// 为什么是"剪除"而不是"不声明"：只把它从清单里删掉会留下一份谁也解释不清的载荷 ——
// 它仍随包发货、仍占体积、仍让客户机多一个 CUI，而哈希覆盖集里却没有它。
// 形态刻意照抄上面的 pruneRuntimeArtifacts（不另发明一套），两向 fail-closed：
//   上游 SDK 有而 staging 里没有 ⇒ 拷贝不完整（剪除退化成空操作，必须响亮报错）；
//   删完仍在 ⇒ 删除失败，不得带着它继续打包。
function pruneIntentionallyAbsentNatives(sourceRelease, targetRelease, fail) {
  for (const entry of INTENTIONALLY_ABSENT_NATIVE) {
    const staged = path.join(targetRelease, entry.name);
    if (fs.existsSync(path.join(sourceRelease, entry.name)) && !fs.existsSync(staged)) {
      fail('SIDECAR_PACKAGE_PRUNED_NATIVE_COPY_INCOMPLETE');
    }
    if (!fs.existsSync(staged)) continue;
    try {
      fs.rmSync(staged, { force: true });
    } catch (_) {
      // 由下方向的存在性断言统一判红，避免把原始 fs 错误当成结论。
    }
    if (fs.existsSync(staged)) fail('SIDECAR_PACKAGE_PRUNED_NATIVE_PRUNE_FAILED');
  }
}

function buildPackage(config, api) {  const {
    APP_SOURCES,
    createProvenance,
    fail,
    sha256File,
    verifyAppSourceSet,
    verifyPackage,
    createCurrentPointer,
    createRuntimeLayout,
    finalizeStagedGeneration,
    generationIdForProvenance,
    publishCurrentPointer,
    closedFileMap,
  } = api;
  verifyAppSourceSet(config.sidecarDir);
  // 媒体混流/推流/截屏家族：构建期硬错误。刻意放在**任何 npm ci / 拷贝之前** ——
  // 这道门是便宜的纯读扫描，没有理由让它等到把包组装完才响。
  assertNoMediaFamilyApiReferences(config.sidecarDir);
  // SIDECAR_SKIP_NPM_CI=1：跳过 npm ci（前提：调用方已手动在 sidecar/ 装好全新
  // node_modules）。背景（2026-09-02）：宿主 shell 会对长命令发起重试，两个并发
  // npm ci 在同一 node_modules 上竞态死锁（600s 无输出后 SIGTERM，连续 3 次实证）。
  // electron 版本一致性仍由下方 MISMATCH 校验兜底，skip 只省重装、不放松校验。
  if (process.env.SIDECAR_SKIP_NPM_CI !== '1') {
    runNpm(['ci'], config.sidecarDir, fail);
  }
  const electronPackage = JSON.parse(fs.readFileSync(path.join(config.sidecarDir, 'node_modules', 'electron', 'package.json'), 'utf8'));
  if (electronPackage.version !== config.electronVersion) fail('SIDECAR_PACKAGE_ELECTRON_VERSION_MISMATCH');

  // stable root + staging（不再删除/重建 runtime 根，也不写 flat 文件）。
  createRuntimeLayout(config.runtimeDir);
  const stagingDir = path.join(config.runtimeDir, 'staging', `pending-${crypto.randomUUID()}`);
  fs.mkdirSync(stagingDir, { recursive: true });

  // Electron dist → staging；electron.exe 作为 installed 身份与 externalBin 构建输入。
  const electronDist = path.join(config.sidecarDir, 'node_modules', 'electron', 'dist');
  copyTreeInto(electronDist, stagingDir);
  // 在写任何 manifest / 计算任何闭集之前剪除运行期产物：否则它会被哈希进
  // runtime_files 与 generation.json，而运行期一追加就与声明分叉（详见本函数注释）。
  pruneRuntimeArtifacts(electronDist, stagingDir, fail);
  const electronExe = path.join(stagingDir, 'electron.exe');
  if (!fs.existsSync(electronExe)) fail('SIDECAR_PACKAGE_ELECTRON_RUNTIME_MISSING');
  copyFileExternal(electronExe, path.join(stagingDir, config.installedFile), fail);
  copyFileExternal(electronExe, config.executable, fail);
  removeFileExternal(electronExe, fail);

  // resources/app 组装。
  fs.rmSync(path.join(stagingDir, 'resources', 'default_app.asar'), { force: true });
  const appDir = path.join(stagingDir, 'resources', 'app');
  fs.mkdirSync(appDir, { recursive: true });
  for (const relative of APP_SOURCES) {
    const source = path.join(config.sidecarDir, relative);
    if (!fs.existsSync(source)) fail('SIDECAR_PACKAGE_APP_SOURCE_MISSING');
    fs.copyFileSync(source, path.join(appDir, relative));
  }
  runNpm(['ci', '--omit=dev'], appDir, fail);
  const installedSdk = JSON.parse(fs.readFileSync(path.join(appDir, 'node_modules', 'trtc-electron-sdk', 'package.json'), 'utf8')).version;
  if (installedSdk !== config.sdkVersion) fail('SIDECAR_PACKAGE_SDK_VERSION_MISMATCH');

  // 在写任何 manifest / 计算任何闭集之前剪除刻意缺席的原生成员：否则它会既随包发货、
  // 又被哈希进 native_files/runtime_files（"剪除"就只剩下文档意义）。
  // 上游侧取 sidecar/node_modules 里那份 SDK dist：它是 npm ci 结果的同源对照，
  // 也是"上游还在发这个文件"的唯一判据（见 pruneIntentionallyAbsentNatives 的注释）。
  const sourceRelease = path.join(config.sidecarDir, 'node_modules', 'trtc-electron-sdk', 'build', 'Release');
  const stagedRelease = path.join(appDir, 'node_modules', 'trtc-electron-sdk', 'build', 'Release');
  pruneIntentionallyAbsentNatives(sourceRelease, stagedRelease, fail);

  // provenance 与 metadata 写入 staging（生成 generation.json 之前）。
  fs.writeFileSync(path.join(stagingDir, SHA_FILE), `${sha256File(path.join(stagingDir, config.installedFile))}\n`, { encoding: 'ascii' });
  const manifest = createProvenance(config, stagingDir);
  fs.writeFileSync(path.join(stagingDir, PROVENANCE_FILE), `${JSON.stringify(manifest, null, 2)}\n`, { encoding: 'utf8' });
  fs.writeFileSync(path.join(stagingDir, PROVENANCE_DIGEST_FILE), `${sha256File(path.join(stagingDir, PROVENANCE_FILE))}\n`, { encoding: 'ascii' });

  // finalize immutable generation → publish current pointer。
  const provenanceBytes = fs.readFileSync(path.join(stagingDir, PROVENANCE_FILE));
  const generation = generationIdForProvenance(provenanceBytes);
  finalizeStagedGeneration({
    runtimeDir: config.runtimeDir,
    stagingDir,
    provenanceBytes,
    expectedFiles: closedFileMap(stagingDir),
  });
  publishCurrentPointer({
    runtimeDir: config.runtimeDir,
    pointer: createCurrentPointer({ generation, manifestSha256: generation.slice(2) }),
  });

  return verifyPackage(config);
}

module.exports = {
  MEDIA_FAMILY_API_PATTERN,
  assertNoMediaFamilyApiReferences,
  buildPackage,
  pruneIntentionallyAbsentNatives,
  pruneRuntimeArtifacts,
};
