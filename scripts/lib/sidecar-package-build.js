'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { RUNTIME_ARTIFACT_FILES } = require('./sidecar-runtime-immutable');

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

module.exports = { buildPackage, pruneRuntimeArtifacts };
