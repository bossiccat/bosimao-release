// decompress critical advisory 修复链 —— download@8 ESM-interop 兼容补丁
// （postinstall 自动执行，幂等，fail-closed）
//
// 背景（Dependabot #31/#32, critical）：
//   decompress@4.2.1 存在 symlink 链路径穿越漏洞，官方无补丁版；
//   修复 = override 换维护续作 @xhmikosr/decompress@^11（package.json overrides）。
//   但 fork 自 9.0.0 起全线 ESM-only，而 trtc-electron-sdk → download@8
//   是 CJS：`const decompress = require('decompress')` 拿到 interop namespace
//   对象（{__esModule, default}）后直接当函数调用 → "decompress is not a function"。
//
// 本补丁：把 download/index.js 的裸 require 调用改为 interop 安全形态。
//   （npm overrides 对 file:/link: 规格支持有缺陷——已实测 reify 失败，
//    故不用 file: 垫片方案；别名 override 安装已实证成功，仅差调用形态。）
//
// 执行时机：npm install / npm ci 的 postinstall（本地开发、generation
//   构建、CI 一致生效）。SDK 自身 install 脚本（download.js）在本补丁前
//   运行但只在缓存缺失时才真正解压——若未来该路径开始执行且 SDK 归档
//   需要 .tgz/.zip 解压，需把本补丁提前（例如改用 pre-install 钩子或
//   直接 vendor download）。
'use strict';
const fs = require('fs');
const path = require('path');

const MARKER = 'decompress-compat-patch';
const downloadIndex = path.join(__dirname, '..', 'node_modules', 'download', 'index.js');

function fail(msg) {
  console.error('[patch-download-interop] FATAL:', msg);
  process.exit(1);
}

// ---- 定位 download@8 ----
if (!fs.existsSync(downloadIndex)) {
  // download 未安装 = trtc-electron-sdk 缺席（如 --omit=dev 场景），无需补丁
  console.log('[patch-download-interop] download@8 not installed, nothing to patch');
  process.exit(0);
}
const src = fs.readFileSync(downloadIndex, 'utf8');

if (src.includes(MARKER)) {
  console.log('[patch-download-interop] already applied, skip');
  verifyFork();
  process.exit(0);
}

// ---- 改写调用形态 ----
const NEEDLE = "const decompress = require('decompress');";
if (!src.includes(NEEDLE)) {
  fail(`needle not found in download/index.js（download 上游变化，必须人工复核补丁）`);
}
const REPLACEMENT = [
  `// ${MARKER}: @xhmikosr/decompress 9+ ESM-only，CJS require 得到 namespace 对象`,
  'const _decompress_ns = require(\'decompress\');',
  'const decompress = typeof _decompress_ns === \'function\' ? _decompress_ns : (_decompress_ns.default ?? _decompress_ns);',
].join('\n');
fs.writeFileSync(downloadIndex, src.replace(NEEDLE, REPLACEMENT));
console.log('[patch-download-interop] interop patch applied to download/index.js');

// ---- fail-closed：确认装进来的 decompress 确实是修复版 fork ----
verifyFork();

function verifyFork() {
  const candidates = [
    path.join(__dirname, '..', 'node_modules', 'download', 'node_modules', 'decompress', 'package.json'),
    path.join(__dirname, '..', 'node_modules', 'decompress', 'package.json'),
  ];
  for (const p of candidates) {
    if (!fs.existsSync(p)) continue;
    const pkg = JSON.parse(fs.readFileSync(p, 'utf8'));
    if (pkg.name === '@xhmikosr/decompress') {
      const major = parseInt(pkg.version.split('.')[0], 10);
      if (major < 9) fail(`unexpected fork version ${pkg.version} (< 9 未含修复)`);
      console.log(`[patch-download-interop] fork verified: ${pkg.name}@${pkg.version} at ${path.relative(process.cwd(), p)}`);
      return;
    }
    fail(`vulnerable decompress present: ${pkg.name}@${pkg.version} — override 未生效，禁止放行`);
  }
  fail('decompress not found at any expected location — install tree 异常');
}
