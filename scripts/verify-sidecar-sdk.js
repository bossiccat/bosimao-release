// verify-sidecar-sdk.js —— sidecar TRTC SDK 基线验证（Task 9 / SPEC §4.3）
//
// 机械检查（全部可判定，缺任一即退出非 0）：
//   1. package.json 声明 trtc-electron-sdk 精确版本（无 ^/~）
//   2. lockfile 与 manifest 版本一致
//   3. node_modules/trtc-electron-sdk 正式包存在且可解析
//   4. SDK 包含原生 .node 二进制
//   5. 包版本 = lock 版本（真实安装证据）
//   6. 只有 rtc.js 创建 TRTCCloud（getTRTCShareInstance）
//
// 用法：node scripts/verify-sidecar-sdk.js
'use strict';

const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..');
const SIDECAR = path.join(ROOT, 'sidecar');
const MANIFEST = JSON.parse(fs.readFileSync(path.join(SIDECAR, 'package.json'), 'utf8'));
const LOCK = JSON.parse(fs.readFileSync(path.join(SIDECAR, 'package-lock.json'), 'utf8'));
const SDK_DIR = path.join(SIDECAR, 'node_modules', 'trtc-electron-sdk');

const failures = [];

function fail(msg) {
  failures.push(msg);
  console.error(`FAIL: ${msg}`);
}

// 1. manifest 精确版本
const declared = MANIFEST.dependencies && MANIFEST.dependencies['trtc-electron-sdk'];
if (!declared) fail('manifest 未声明 trtc-electron-sdk');
else if (/^[~^]/.test(declared)) fail(`trtc-electron-sdk 必须精确版本，实际: ${declared}`);
else console.log(`OK manifest: trtc-electron-sdk@${declared}`);

// 2/5. lockfile 一致
const locked = LOCK.packages && LOCK.packages['node_modules/trtc-electron-sdk'];
if (!locked) fail('lockfile 缺少 node_modules/trtc-electron-sdk');
else {
  console.log(`OK lock: trtc-electron-sdk@${locked.version} (${locked.resolved})`);
  if (declared && locked.version !== declared.replace(/^[~^]/, '')) {
    fail(`lock 版本 ${locked.version} 与 manifest ${declared} 不一致`);
  }
}

// 3. 正式包存在且可解析
if (!fs.existsSync(SDK_DIR)) {
  fail(`node_modules/trtc-electron-sdk 缺失（UNMET DEPENDENCY）：${SDK_DIR}`);
} else {
  try {
    const entry = require.resolve('trtc-electron-sdk', { paths: [SIDECAR] });
    console.log(`OK require.resolve: ${entry}`);
  } catch (e) {
    fail(`trtc-electron-sdk 无法解析: ${e.message}`);
  }
  const pkgPath = path.join(SDK_DIR, 'package.json');
  if (fs.existsSync(pkgPath)) {
    const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
    console.log(`OK installed: trtc-electron-sdk@${pkg.version}`);
    if (locked && pkg.version !== locked.version) fail(`安装版本 ${pkg.version} 与 lock ${locked.version} 不一致`);
  } else {
    fail('SDK 目录缺 package.json（疑似损坏安装）');
  }
}

// 4. 原生二进制
if (fs.existsSync(SDK_DIR)) {
  const natives = [];
  const walk = (dir) => {
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, ent.name);
      if (ent.isDirectory()) walk(p);
      else if (ent.name.endsWith('.node')) natives.push(p);
    }
  };
  walk(SDK_DIR);
  if (natives.length === 0) fail('SDK 缺少原生 .node 二进制（包损坏或平台不匹配）');
  else console.log(`OK natives: ${natives.length} 个 .node 二进制`);
}

// 6. 创建点唯一
const rtc = fs.readFileSync(path.join(SIDECAR, 'rtc.js'), 'utf8');
if (!/getTRTCShareInstance/.test(rtc)) fail('rtc.js 必须创建 TRTCCloud（getTRTCShareInstance）');
for (const f of ['main.js', 'audio.js', 'bridge.js', 'config.js', 'logger.js']) {
  const p = path.join(SIDECAR, f);
  if (fs.existsSync(p) && /getTRTCShareInstance|new TRTCCloud\b/.test(fs.readFileSync(p, 'utf8'))) {
    fail(`TRTCCloud 创建只允许在 rtc.js，发现 ${f}`);
  }
}

if (failures.length > 0) {
  console.error(`\nverify-sidecar-sdk FAIL（${failures.length} 项）`);
  process.exit(1);
}
console.log('\nverify-sidecar-sdk PASS');
